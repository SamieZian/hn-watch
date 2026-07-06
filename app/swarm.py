"""Research swarm: N parallel `claude -p` agents + one synthesis call.

Parallelism is real (asyncio.gather) but throttled by the SAME semaphore the
monitor ticks use (claude_runner.CLAUDE_SEM), so the two workloads share one
concurrency governor. Failed agents don't sink the run: synthesis proceeds
with the survivors.
"""
import asyncio
import logging

from . import claude_runner, config, db
from .events import bus

log = logging.getLogger("hnwatch.swarm")

AGENT_PROMPT = """You are one research agent in a team investigating a Hacker News story.
Investigate ONLY your assigned angle. Be fast and concrete.

STORY: {title}
LINK: {url}
HN DISCUSSION: {hn_url}

YOUR ANGLE: {angle_name}: {angle_instructions}

Use WebSearch/WebFetch as needed (a handful of lookups, not an exhaustive crawl).
Then output a markdown section: start with "## {angle_name}", max ~300 words,
bullet-heavy, end with a "Sources:" list of URLs you actually used."""

SYNTH_PROMPT = """You are compiling a research brief from parallel agents' findings about a Hacker News story.

STORY: {title} ({url})

AGENT REPORTS:
{reports}

Write one cohesive markdown brief:
# <punchy headline>
**TL;DR**: 2-3 sentences.
## Key takeaways   (3-5 bullets, the non-obvious stuff)
<then the strongest material reorganized by theme, not by agent>
## Open questions  (2-3 bullets)
Keep it under 600 words. Do not invent facts not present in the reports."""


def start_run(feed_item_id: int) -> int | None:
    item = db.get_feed_item(feed_item_id)
    if item is None:
        return None
    run_id, agent_ids = db.create_swarm_run(
        feed_item_id, [name for name, _ in config.SWARM_ANGLES]
    )
    asyncio.get_running_loop().create_task(_run(run_id, agent_ids, item))
    return run_id


async def _run(run_id: int, agent_ids: list[int], item: dict) -> None:
    self_desc = {"title": item["title"], "url": item["url"] or item["hn_url"],
                 "hn_url": item["hn_url"]}
    _publish_status(run_id)

    results = await asyncio.gather(
        *[
            _agent(run_id, agent_id, angle, instructions, self_desc)
            for agent_id, (angle, instructions) in zip(agent_ids, config.SWARM_ANGLES)
        ],
        return_exceptions=True,
    )

    sections = []
    for (angle, _), r in zip(config.SWARM_ANGLES, results):
        if isinstance(r, str) and r.strip():
            sections.append(r)
        else:
            sections.append(f"## {angle}\n\n(agent failed)")

    db.update_swarm_run(run_id, status="synthesizing")
    _publish_status(run_id)

    res = await claude_runner.run_json(
        SYNTH_PROMPT.format(
            title=item["title"], url=self_desc["url"], reports="\n\n".join(sections)
        ),
        model=config.RESEARCH_MODEL,
        parse=False,  # the brief is markdown, not JSON
        max_budget_usd=config.AGENT_MAX_BUDGET_USD,
    )
    if res.ok:
        db.add_swarm_cost(run_id, res.cost_usd)
        db.update_swarm_run(
            run_id, status="done", brief_md=res.text, finished_at=db.now_iso()
        )
    else:
        db.update_swarm_run(
            run_id, status="error",
            error=res.error or "synthesis failed", finished_at=db.now_iso(),
        )
    _publish_status(run_id)


async def _agent(
    run_id: int, agent_id: int, angle: str, instructions: str, item: dict
) -> str:
    async def on_event(event: dict) -> None:
        bus.publish({
            "type": "swarm.agent", "run_id": run_id, "agent_id": agent_id,
            "angle": angle, **event,
        })

    try:
        res = await claude_runner.run_stream(
            AGENT_PROMPT.format(
                title=item["title"], url=item["url"], hn_url=item["hn_url"],
                angle_name=angle, angle_instructions=instructions,
            ),
            on_event=on_event,
        )
    except Exception as exc:
        log.exception("swarm agent %s crashed", agent_id)
        res = claude_runner.ClaudeResult(ok=False, error=str(exc))

    db.add_swarm_cost(run_id, res.cost_usd)
    if res.ok:
        db.update_swarm_agent(
            agent_id, status="done", output_md=res.text, finished_at=db.now_iso()
        )
        await on_event({"kind": "done"})
        return res.text
    db.update_swarm_agent(agent_id, status="error", finished_at=db.now_iso())
    await on_event({"kind": "error", "text": res.error or "agent failed"})
    return ""


def _publish_status(run_id: int) -> None:
    run = db.get_swarm_run(run_id)
    if run is not None:
        bus.publish({"type": "swarm.status", "run": run})
