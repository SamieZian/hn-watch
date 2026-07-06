"""App-wide constants. A settings UI is deliberately out of scope."""
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "hnwatch.db"

HOST = "127.0.0.1"
PORT = 8321

# claude -p runtime
CLAUDE_BIN = "claude"
MAX_CONCURRENT_CLAUDE = 4      # one semaphore governs monitors AND swarm agents
JUDGE_MODEL = "haiku"          # cheap, fast: relevance verdicts
RESEARCH_MODEL = "sonnet"      # research agents + synthesis
JUDGE_TIMEOUT_S = 120
AGENT_TIMEOUT_S = 300
AGENT_MAX_TURNS = 12
JUDGE_MAX_BUDGET_USD = 0.25    # hard cap per judge/synthesis call
AGENT_MAX_BUDGET_USD = 1.00    # hard cap per research agent

# monitors
MAX_ITEMS_PER_TICK = 25        # cap candidates per judge call (prompt size / cost)
FIRST_TICK_LOOKBACK_S = 24 * 3600  # new monitor bootstraps from the last 24h
DEFAULT_INTERVAL_MINUTES = 30

# swarm
SWARM_ANGLES = [
    (
        "The thing itself",
        "What is it, technically? Read the linked page; explain what it actually does and how.",
    ),
    (
        "Who's behind it",
        "Company/author background, funding, track record, notable prior work.",
    ),
    (
        "Community reaction",
        "Fetch the HN comment thread; summarize praise, criticism, and notable commenters.",
    ),
    (
        "Context & competitors",
        "Prior art, alternatives, how it compares, and why this is appearing now.",
    ),
]
