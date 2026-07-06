# How I drove the coding agent

The assignment asks for the prompts used with a coding agent, so here's the
honest log. I used **Claude Code** interactively. My approach: don't let the
agent write code until a plan I believed in existed — then let it implement
phase by phase, verifying each phase against the real app (real HN data, real
`claude -p` calls, driving the UI in a browser) before moving on.

## 1. Kickoff

I gave it a screenshot of the assignment email and typed `/plan`.

Before designing anything it checked my machine (found Rust missing, found
`claude` CLI 2.1.201) and benchmarked a trivial `claude -p` call: ~6s and
~$0.13. That single measurement drove the most important design decision —
one batched judge call per tick, never one call per item.

## 2. My clarifying answers (these shaped everything)

Stack — the assignment allowed replacing Rust:

> **use python and fastapi if required .. make python around ecosystem**

Location:

> **~/Desktop/hn-watch**

## 3. The plan

It came back with the architecture that's now in the README — the
pywebview/pystray single-Cocoa-loop shell, the shared-semaphore claude
runner, Algolia over Firebase, the SQLite schema, four fixed swarm angles,
and a build order that put throwaway de-risk spikes *before* any app code.
I approved it as-is; the plan doc became the working spec for every phase.

## 4. Implementation phases

- **Phase 0 — spikes.** Prove the scary parts first: pywebview + pystray
  `run_detached()` + osascript in one process; a haiku judge call on fake
  items (this caught haiku wrapping "JSON only please" output in markdown
  fences → fallback parser); capturing raw `stream-json` lines to a file
  before writing the parser against guessed schemas.
- **Phase 1 — core pipeline.** db → hn client → claude runner → tick → API/
  SSE → feed UI. Verified live: a real tick judged 25 stories for $0.03 and
  matched 2; the second tick cost $0.00 and produced zero duplicates; a
  restart preserved monitors and feed.
- **Phase 2 — shell.** The spike's threading model moved into `app/main.py`.
- **Phase 3 — swarm.** Verified live: 4 agents streamed tool-use events over
  SSE, finished at different times, synthesis produced the combined brief.
  Total for the run: $1.17.
- **Phase 4 — README, this file, repo.**

## 5. Corrections that came up (and who caught them)

- `asyncio.QueueError` doesn't exist → `QueueEmpty`/`QueueFull` (runtime).
- pywebview has no `__version__` attribute (spike crash).
- Status dots on a finished swarm run rendered as "running" — caught by
  actually driving the UI in a browser after building it, not by reading
  the code.

## 6. Post-build review

After everything worked I asked for an audit against the assignment plus
research into current best practice:

> **make whatever menstioned in assignment have we fixed all of them ? and
> what are design decision took ? have you did web search what are good
> practices and design and better one exist and all ?**

That produced two real upgrades (spiked before adopting): judge verdicts
moved from prompt-begged JSON to CLI-enforced `--json-schema` structured
output (needs `--max-turns 3` for its internal StructuredOutput tool call),
and hard `--max-budget-usd` caps on every invocation. It also evaluated and
rejected `--bare` (requires API-key auth; my machine uses subscription
OAuth) and the Agent SDK (better embedding story, but the assignment
mandates `claude -p`).
