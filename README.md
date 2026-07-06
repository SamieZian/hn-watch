# HN Watch

A small native macOS app that watches Hacker News for me. I create **monitors** (a natural-language prompt like *"AI-agent startup launches"* plus an interval) and each one runs as a background worker that pulls recent HN stories, asks **Claude Code in headless mode (`claude -p`)** which ones actually match, and drops the hits into a single Twitter-style feed. Every feed item has a **Dig deeper** button that fans out four `claude -p` research agents in parallel (each on a different angle), streams their progress live, and compiles the results into one brief.

The assignment spec'd Tauri + Rust but explicitly allowed swapping the stack. I went with Python end-to-end. It's where I have the most depth, and I decided the interesting parts of this assignment (the agent runtime layer, the concurrency story) live in how you orchestrate `claude -p`, not in the shell language. More on that trade-off below.

## Running it

You need:
- macOS with Homebrew `python@3.12` (`brew install python@3.12`)
- [Claude Code](https://claude.com/claude-code) installed and logged in, check with `claude -p "say hi"`

```bash
git clone https://github.com/SamieZian/hn-watch.git
cd hn-watch
./run.sh
```

First run bootstraps a venv (six dependencies). You get a window, an orange **Y** in the menu bar, and the UI at `http://127.0.0.1:8321`. Closing the window just hides it; monitors keep ticking in the background; reopen or quit from the tray.

To try it quickly: create a monitor, hit **▶ Run now** (the first tick looks back 24h so you're not staring at an empty feed), then **🔎 Dig deeper** on anything that lands.

## How it's put together

```
macOS process
│
├── MAIN THREAD (Cocoa/NSApplication, owned by pywebview)
│     window (close intercepted → hide, app keeps running)
│     pystray tray icon via run_detached()  ← attaches to the SAME NSApp
│
└── SERVER THREAD (its own asyncio loop)
      uvicorn + FastAPI  (REST + one SSE stream + static frontend)
      ├── one asyncio task per enabled monitor (tick loop)
      ├── swarm orchestrator tasks (asyncio.gather of 4 agents)
      ├── ALL `claude -p` subprocesses          ┐ shared
      │       asyncio.Semaphore(4) ─────────────┘ governor
      └── notifications via osascript
```

Data flow: Algolia HN API → dedup filter → `claude -p` judge (haiku) → SQLite → SSE → vanilla-JS feed. No build step, no ORM, no message broker. The whole thing is ~1,300 lines.

### The claude runtime layer

This is the part of the assignment I spent the most thought on. The scheduled monitors and the on-demand swarm hit the same runtime in opposite shapes (one call per tick vs. many at once) and my answer is to force both through a single module, [`app/claude_runner.py`](app/claude_runner.py), with two entry points:

| | `run_json()` | `run_stream()` |
|---|---|---|
| used by | monitor ticks, swarm synthesis | swarm research agents |
| model | haiku (judging) / sonnet (synthesis) | sonnet |
| tools | none external (tight `--max-turns`) | `--allowedTools WebSearch WebFetch` |
| output | `--output-format json` (+ `--json-schema` for judging) | `stream-json` lines → normalized events → SSE |
| cost rail | `--max-budget-usd 0.25` | `--max-budget-usd 1.00` per agent |

Both paths acquire the **same global `asyncio.Semaphore(4)`**. That one primitive is the whole concurrency story: a monitor tick that fires while a swarm is running just queues until a slot frees. Every CLI envelope reports `total_cost_usd`, which I accumulate per-monitor and per-run and show in the UI. With LLM calls in a loop, cost is a feature you want visible.

**Batching was non-negotiable.** Before writing any app code I measured a trivial `claude -p` call: ~6 seconds and ~$0.13 minimum. Judging 25 stories one-by-one would burn minutes and dollars per tick. So each tick sends *all* fresh candidates in one prompt and gets back one verdict array. A real tick costs ~$0.02-0.04 (haiku); a full dig-deeper run lands around $1.20 (4 sonnet agents + synthesis).

**Structured output is enforced, not requested.** My first judge prompt begged for raw JSON, and haiku still wrapped it in markdown fences sometimes (caught this in a spike, not in production, see below). I later found Claude Code's `--json-schema` flag and switched to it: the CLI itself guarantees a schema-valid `structured_output` field. One gotcha worth knowing: schema mode works through an internal StructuredOutput tool call, so it needs `--max-turns 3` even with no external tools. I kept the old fence-stripping parser as a fallback for schemaless calls.

**Failure containment.** A failed judge call flags the monitor and does *not* mark its items as seen, so they get re-judged next tick. A dead swarm agent doesn't kill the run; synthesis proceeds with the survivors and the UI shows which pane failed. Timeouts kill the subprocess; budgets cap the spend.

## Design decisions & trade-offs

**Python instead of Rust/Tauri.** What I gave up: a real bundled .app and Tauri's footprint. What I kept: every behavior in the spec (long-lived workers, tray persistence, native notifications, parallel orchestration), because all of it lives in asyncio + subprocesses, not in the shell language. For a weekend build, fighting the borrow checker for zero behavioral difference wasn't the trade I wanted.

**pywebview + pystray for the shell.** macOS wants exactly one Cocoa main loop and both libraries want the main thread. pywebview owns the loop; pystray's `run_detached()` exists precisely to attach a status item to an NSApp someone else runs (that's the documented pattern in its docs). I rejected rumps because it insists on owning the loop. I de-risked this combination with a throwaway spike (`spikes/`) before writing any real code, since it was the thing most likely to sink the design. Belt and braces: even if the tray dies, close-is-hide plus a non-daemon server thread already satisfies "keeps running with the window closed".

**Algolia HN Search API over the official Firebase one.** `search_by_date` with `numericFilters=created_at_i>cursor` gives me fully-hydrated stories newer than my cursor in one request; Firebase would be N+1. The cost is that Algolia's index lags the live site by a few minutes, which doesn't matter for a monitor ticking every 30.

**Plain asyncio tasks over APScheduler.** A monitor is literally `while enabled: tick(); sleep(interval)`. A scheduler dependency buys cron syntax I don't need.

**stdlib sqlite3, synchronous, no ORM.** Five tables, sub-ms operations, one writer thread. If it ever measurably blocks the loop, aiosqlite is the drop-in upgrade. The DB lives in `./data/` so you can just open it; `~/Library/Application Support` is where it would go in a "real" release.

**Dedup at two levels.** `seen_items` records every id ever *judged* per monitor, match or not, so I never pay to re-judge a story. The `UNIQUE(monitor_id, hn_id)` constraint on `feed_items` (+ `INSERT OR IGNORE`) is the backstop.

**Things I looked at and decided against:**
- `--bare`: Anthropic's recommended mode for scripted calls (skips hooks/plugins/CLAUDE.md, deterministic), but it skips OAuth too and needs an API key; I'm targeting machines logged in via subscription. `--no-session-persistence` keeps runs stateless instead.
- The **Claude Agent SDK**: the production way to embed this (native message objects, no subprocess parsing). The assignment mandates `claude -p`, and honestly the subprocess boundary buys real isolation: a crashed/hung/over-budget agent is a dead process, never a dead app.
- `--include-partial-messages`: token-level streaming. Per-message events (each turn, each tool call) already make the live view feel alive; token deltas triple the parser for cosmetics.

## What's stubbed

- Settings are constants in `app/config.py`; no settings UI
- Localhost bind, no auth
- No .app bundle/codesigning, so notifications show as from "Script Editor" (a signed bundle or terminal-notifier would fix the identity)
- The judge sees title/url/points only; comment text is only read by dig-deeper agents
- Only the latest swarm run per item is surfaced (history stays in the DB)
- Next in line: let claude pick the 4 angles per story, aiosqlite, .app bundle

## Repo tour

```
app/claude_runner.py   the shared claude -p wrapper, start here
app/monitors.py        scheduler + tick pipeline (+ judge prompt/schema)
app/swarm.py           orchestrator + agent/synthesis prompts
app/server.py          FastAPI routes + SSE
app/main.py            the threading model (webview/tray/uvicorn)
app/db.py, hn.py       persistence, Algolia client
app/static/            the frontend
spikes/                the Phase-0 shell de-risk spike
PROMPTS.md             how I drove the coding agent (per the assignment)
```

Built with Claude Code doing the typing and me doing the deciding. The full prompt log is in [PROMPTS.md](PROMPTS.md), as the assignment asks.
