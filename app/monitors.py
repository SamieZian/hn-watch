"""Monitor scheduler: one asyncio task per enabled monitor.

Tick pipeline: fetch since cursor → drop already-judged ids → ONE claude call
judging the whole batch → persist matches → advance cursor → notify.
A failed judge call does NOT mark items seen, so they are re-judged next tick.
"""
import asyncio
import json
import logging
import random
import time

from . import claude_runner, config, db, hn, notify
from .events import bus

log = logging.getLogger("hnwatch.monitors")

JUDGE_PROMPT = """You are a strict relevance filter for a Hacker News monitor.

MONITOR PROMPT (what the user cares about):
\"\"\"{monitor_prompt}\"\"\"

CANDIDATE ITEMS (JSON array):
{items_json}

For EACH item decide if it clearly matches the monitor prompt. Be selective:
when in doubt, mark it not relevant.

Respond with ONLY a JSON array, no prose, no markdown fences:
[{{"id": <id>, "relevant": true|false, "summary": "<if relevant: 1-2 sentences on what it is and why it matches, max 220 chars; else empty string>"}}]
Every input id must appear exactly once in the output."""


class MonitorScheduler:
    def __init__(self) -> None:
        self._tasks: dict[int, asyncio.Task] = {}

    def start_all(self) -> None:
        for m in db.list_monitors():
            if m["enabled"]:
                self.start(m["id"])

    def start(self, monitor_id: int) -> None:
        self.stop(monitor_id)
        self._tasks[monitor_id] = asyncio.create_task(self._loop(monitor_id))

    def stop(self, monitor_id: int) -> None:
        task = self._tasks.pop(monitor_id, None)
        if task is not None:
            task.cancel()

    def stop_all(self) -> None:
        for mid in list(self._tasks):
            self.stop(mid)

    async def run_now(self, monitor_id: int) -> None:
        m = db.get_monitor(monitor_id)
        if m is not None:
            await self.tick(m)

    async def _loop(self, monitor_id: int) -> None:
        # jitter so several monitors starting together don't pile onto the semaphore
        await asyncio.sleep(random.random() * 10)
        while True:
            m = db.get_monitor(monitor_id)
            if m is None or not m["enabled"]:
                return
            try:
                await self.tick(m)
            except Exception:
                log.exception("tick failed for monitor %s", monitor_id)
            m = db.get_monitor(monitor_id)
            if m is None:
                return
            await asyncio.sleep(m["interval_minutes"] * 60)

    async def tick(self, m: dict) -> None:
        mid = m["id"]
        self._publish_status(mid, "running")
        try:
            since = m["last_cursor"] or int(time.time()) - config.FIRST_TICK_LOOKBACK_S
            items = await hn.fetch_recent(since, limit=50)
        except Exception as exc:
            self._record_error(mid, f"HN fetch failed: {exc}")
            return

        fresh_ids = db.unseen(mid, [i.hn_id for i in items])
        fresh = [i for i in items if i.hn_id in fresh_ids]
        fresh = fresh[: config.MAX_ITEMS_PER_TICK]  # newest first from Algolia

        if not fresh:
            self._finish_ok(m, items)
            return

        items_json = json.dumps(
            [
                {"id": i.hn_id, "title": i.title, "url": i.url,
                 "points": i.points, "num_comments": i.num_comments}
                for i in fresh
            ]
        )
        res = await claude_runner.run_json(
            JUDGE_PROMPT.format(monitor_prompt=m["prompt"], items_json=items_json)
        )
        if not res.ok or not isinstance(res.data, list):
            self._record_error(mid, res.error or "judge returned unexpected shape")
            return

        db.add_monitor_cost(mid, res.cost_usd)
        verdicts = {
            v["id"]: v for v in res.data
            if isinstance(v, dict) and "id" in v
        }
        by_id = {i.hn_id: i for i in fresh}
        new_items = []
        for hn_id, verdict in verdicts.items():
            item = by_id.get(hn_id)
            if item is not None and verdict.get("relevant"):
                row = db.insert_feed_item(
                    mid, item, verdict.get("summary") or item.title
                )
                if row is not None:
                    new_items.append(row)

        db.mark_seen(mid, list(by_id))  # all judged ids, match or not
        self._finish_ok(m, items)

        if new_items:
            bus.publish({"type": "feed.new", "monitor_id": mid, "items": new_items})
            await notify.send(
                "HN Watch",
                f"{len(new_items)} new item{'s' if len(new_items) > 1 else ''}",
                subtitle=m["name"],
            )

    def _finish_ok(self, m: dict, fetched: list) -> None:
        cursor = max([i.created_at_i for i in fetched], default=m["last_cursor"] or 0)
        db.update_monitor(
            m["id"],
            last_run_at=db.now_iso(),
            last_cursor=cursor or None,
            last_status="ok",
            last_error=None,
        )
        self._publish_status(m["id"], "idle")

    def _record_error(self, monitor_id: int, error: str) -> None:
        log.warning("monitor %s error: %s", monitor_id, error)
        db.update_monitor(
            monitor_id,
            last_run_at=db.now_iso(),
            last_status="error",
            last_error=error[:500],
        )
        self._publish_status(monitor_id, "error")

    def _publish_status(self, monitor_id: int, status: str) -> None:
        m = db.get_monitor(monitor_id)
        if m is not None:
            bus.publish({"type": "monitor.status", "status": status, "monitor": m})


scheduler = MonitorScheduler()
