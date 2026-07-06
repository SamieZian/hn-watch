# Prompts used to drive the coding agent

Per the assignment, this logs how the project was driven with a coding agent.
Tooling: **Claude Code** (interactive session) built the app; the app itself
uses `claude -p` as its runtime. The session followed a plan-first workflow:
the agent was given the assignment, asked clarifying questions, produced an
architecture plan for approval, then implemented phase by phase with
verification between phases.

## 1. Kickoff (planning mode)

> *(screenshot of the assignment email)* — `/plan`

Claude Code entered plan mode, checked the machine's toolchain (found Rust
missing, `claude` CLI 2.1.201 present, measured a trivial `claude -p` call at
~6s / ~$0.13 — which drove the batched-judging design), and asked two
questions before designing anything.

## 2. Clarifying answers (these shaped the architecture)

Stack choice — the assignment allowed replacing Rust:

> **use python and fastapi if required .. make python around ecosystem**

Project location:

> **~/Desktop/hn-watch**

## 3. Plan approval

Claude Code produced the full plan (the tables/diagrams now living in
README.md: pywebview+pystray threading model, shared-semaphore claude runner,
Algolia source, SQLite schema, 4 fixed swarm angles, phased build order with
Phase-0 de-risk spikes) and it was approved as-is.

## 4. Implementation phases (agent-driven, no further human prompts)

The approved plan then served as the prompt for each phase:

- **Phase 0 — spikes first.** "Prove the risky parts before app code":
  pywebview + pystray `run_detached()` + osascript coexistence; a haiku judge
  call on 5 fake items (which caught haiku wrapping JSON in markdown fences →
  the 3-stage parser fallback); capturing raw `stream-json` output to a file
  before writing the parser.
- **Phase 1 — core pipeline.** db → hn → claude_runner → tick → API/SSE →
  feed UI. Verified live: real tick judged 25 HN items for $0.03, matched 2;
  second tick $0.00 and 0 duplicates; restart preserved everything.
- **Phase 2 — shell.** The spike's threading model moved into `app/main.py`.
- **Phase 3 — swarm.** Verified live: 4 agents streamed 13+ tool events over
  SSE, finished at different times, synthesis produced the combined brief,
  total $1.17.
- **Phase 4 — this file, README, repo.**

## 5. Notable mid-course corrections

- `asyncio.QueueError` doesn't exist → `QueueEmpty/QueueFull` (events.py).
- pywebview has no `__version__` attribute (shell spike).
- Frontend bug found by the agent driving the UI in a browser: replaying a
  finished swarm run reset agent status dots to "running" because `getPane()`
  clobbered the dot class when called without a status argument.

## 6. Post-build review prompt

> **make whatever menstioned in assignment have we fixed all of them ? and
> what are design decision took ? have you did web search what are good
> practices and design and better one exist and all ?**

This triggered an audit against the assignment plus research into official
Claude Code headless docs, which produced two upgrades: judge verdicts moved
from prompt-begged JSON to CLI-enforced `--json-schema` structured output
(spiked first: schema mode needs `--max-turns 3` for its internal
StructuredOutput tool call), and hard `--max-budget-usd` caps on every
invocation. `--bare` was evaluated and rejected (requires API-key auth;
target machines use subscription OAuth).
