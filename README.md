# HN Watch

A small native macOS desktop app that watches Hacker News for you. You create **monitors** — a natural-language prompt ("AI-agent startup launches", "Rust async runtime discussions") plus a schedule — and each one runs as a long-lived background worker that pulls recent HN content, has **Claude Code in headless mode (`claude -p`)** judge what's relevant, and appends matches to a single Twitter-style feed. Every feed item has a **Dig deeper** action that fans out a small research swarm: four `claude -p` agents investigating the story from different angles in parallel, streaming their progress live, then compiled into one combined brief.

> Note: the assignment spec'd Tauri + Rust and explicitly allowed changing the stack. This implementation is Python end-to-end — the trade-off is discussed in [Design decisions](#design-decisions--trade-offs).

## Quickstart

Requirements:
- macOS, Homebrew `python@3.12` (`brew install python@3.12`)
- [Claude Code](https://claude.com/claude-code) installed and authenticated — verify with `claude -p "say hi"`

```bash
git clone https://github.com/SamieZian/hn-watch.git
cd hn-watch
./run.sh
```

First run creates a venv and installs six dependencies. The app opens a window, puts an orange **Y** icon in the menu bar, and serves its UI from `http://127.0.0.1:8321`. Closing the window hides it — monitors keep running; reopen or quit from the tray icon.

Try it: create a monitor (e.g. *"Launches or Show HN posts of AI agent products"*), hit **▶ Run now**. The first tick looks back 24 hours so the feed isn't empty. Then hit **🔎 Dig deeper** on a match.

## Architecture

```
macOS process
│
├── MAIN THREAD — Cocoa/NSApplication, owned by pywebview
│     window (close intercepted → hide, app keeps running)
│     pystray tray icon via run_detached()  ← attaches to the SAME NSApp
│
└── SERVER THREAD — its own asyncio loop
      uvicorn + FastAPI  (REST + one SSE stream + static frontend)
      ├── one asyncio task per enabled monitor (tick loop)
      ├── swarm orchestrator tasks (asyncio.gather of 4 agents)
      ├── ALL `claude -p` subprocesses          ┐ shared
      │       asyncio.Semaphore(4) ─────────────┘ governor
      └── notifications via osascript
```

Data flow: **Algolia HN API** → dedup filter → **`claude -p` judge (haiku)** → SQLite → SSE → vanilla-JS feed. No build step, no ORM, no message broker — the whole app is ~1,300 lines.

### The claude runtime layer (the interesting part)

Both workloads — *one call per tick* and *many calls at once* — go through a single module, [`app/claude_runner.py`](app/claude_runner.py), with two entry points:

| | `run_json()` | `run_stream()` |
|---|---|---|
| used by | monitor ticks, swarm synthesis | swarm research agents |
| model | haiku (judging) / sonnet (synthesis) | sonnet |
| tools | none — `--max-turns 1` forbids a tool loop | `--allowedTools WebSearch WebFetch` |
| output | `--output-format json` envelope, parsed | `stream-json` lines → normalized events → SSE |

Both paths acquire the **same global `asyncio.Semaphore(4)`**. That one primitive is the whole answer to "how do you handle one-call-per-tick vs many-at-once": a monitor tick that fires while a 4-agent swarm is running simply queues until a slot frees. Cost from every CLI envelope (`total_cost_usd`) is accumulated per-monitor and per-run and shown in the UI.

**Batching is non-negotiable.** A trivial `claude -p` call measures ~6s and ~$0.13 minimum. Judging 25 items one-by-one would cost dollars and minutes per tick; instead each tick sends *all* fresh candidates in one prompt and gets back a strict-JSON verdict array. A full tick costs ~$0.02–0.04 (haiku); a full dig-deeper run costs ~$1.20 (4 sonnet agents + synthesis).

**Models are split by job**: haiku judges relevance (cheap, fast, no tools), sonnet does research and synthesis (needs reasoning + web tools).

**Failure containment**: the judge prompt demands raw JSON, but haiku still wraps it in markdown fences sometimes (observed during the spike), so parsing falls back raw → fenced → first balanced block. A failed judge call marks the monitor `error` and does *not* mark items seen, so they're re-judged next tick. A failed swarm agent doesn't sink the run — synthesis proceeds with the survivors, and the pane shows the error.

## Design decisions & trade-offs

**Python instead of Rust/Tauri.** The assignment allowed it and my ecosystem depth is Python. What's lost: a real bundled .app and Tauri's tiny footprint. What's kept: everything behavioral — real background workers, tray persistence, native notifications, parallel agent orchestration — because the concurrency lives in asyncio + subprocesses, not in the shell language.

**pywebview + pystray, not rumps/Electron.** macOS wants exactly one Cocoa main loop. pywebview owns it; pystray's `run_detached()` is designed to attach a status item to an *existing* NSApp. rumps was rejected because it insists on owning the main loop itself. Belt-and-braces: even if the tray died, close-is-hide plus the non-daemon server thread already satisfies "keeps running with the window closed".

**Algolia HN Search API, not the official Firebase API.** `search_by_date` with `numericFilters=created_at_i>cursor` returns fully-hydrated items newer than a cursor in one request; Firebase would need N+1 calls. Trade-off: Algolia's index lags the live site by minutes — irrelevant for a monitor that ticks every 30.

**Plain asyncio tasks, not APScheduler.** A monitor is literally `while enabled: tick(); sleep(interval)`. A scheduler library buys cron syntax we don't need.

**stdlib sqlite3, sync, no ORM.** Five tables, sub-millisecond ops, one writer thread. `aiosqlite` is the upgrade path if it ever blocks the loop measurably. The DB lives in `./data/` so an evaluator can just open it; `~/Library/Application Support` would be the "real" location.

**Two-level dedup.** `seen_items` records every id ever *judged* per monitor (match or not) so we never pay to re-judge; `UNIQUE(monitor_id, hn_id)` on `feed_items` with `INSERT OR IGNORE` is the backstop.

**Per-message streaming, not `--include-partial-messages`.** Each assistant turn and tool call is already a lively progress event; token-level streaming would triple the parser complexity for cosmetics.

**Vanilla JS frontend served by FastAPI.** One HTML file, one JS file, one SSE connection, vendored marked.js. No build step means `git clone && ./run.sh` is the entire setup.

## Stubbed / what I'd do next

- Settings are constants in `app/config.py` (no settings UI)
- Server binds localhost only; no auth
- No .app bundling/codesigning — so notifications are attributed to "Script Editor" (a signed bundle or terminal-notifier fixes that)
- Judging sees title/url/points only; comment *text* is only read by dig-deeper agents
- Only the latest swarm run per item is surfaced (history is in the DB)
- Next: angle-picker call (let claude choose the 4 angles per story), token-level streaming, aiosqlite, .app bundle

## Repo tour

```
app/claude_runner.py   the shared claude -p wrapper — start here
app/monitors.py        scheduler + tick pipeline (+ judge prompt)
app/swarm.py           orchestrator + agent/synthesis prompts
app/server.py          FastAPI routes + SSE
app/main.py            the threading model (webview/tray/uvicorn)
app/db.py, hn.py       persistence, Algolia client
app/static/            the frontend
spikes/                Phase-0 de-risk scripts (shell/judge/stream)
PROMPTS.md             prompts used to drive the coding agent
```
