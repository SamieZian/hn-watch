"""The shared `claude -p` runtime layer — used by BOTH workloads.

Monitor ticks call `run_json` (one one-shot call per tick, judging a batch of
items). The research swarm calls `run_stream` N times concurrently plus one
`run_json` for synthesis. Both paths go through the same subprocess wrapper
and the same global semaphore, so "one call per tick" and "many at once" are
governed by a single concurrency/cost primitive: a tick that fires mid-swarm
simply queues until a slot frees up.
"""
import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from . import config

CLAUDE_SEM = asyncio.Semaphore(config.MAX_CONCURRENT_CLAUDE)


@dataclass
class ClaudeResult:
    ok: bool
    text: str = ""
    data: Any | None = None
    cost_usd: float = 0.0
    duration_ms: int = 0
    error: str | None = None


def extract_json(text: str) -> Any:
    """Parse JSON out of model text: raw → fenced → first balanced block."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        try:
            return json.loads(fenced.group(1).strip())
        except json.JSONDecodeError:
            pass
    block = re.search(r"(\[.*\]|\{.*\})", text, re.DOTALL)
    if block:
        return json.loads(block.group(1))
    raise json.JSONDecodeError("no JSON found in model output", text, 0)


async def _spawn(args: list[str], timeout: float) -> tuple[bytes, bytes, int]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return stdout, stderr, proc.returncode or 0


async def run_json(
    prompt: str,
    *,
    model: str = config.JUDGE_MODEL,
    timeout: float = config.JUDGE_TIMEOUT_S,
    parse: bool = True,
) -> ClaudeResult:
    """One-shot call, no tools (--max-turns 1). Returns envelope result text,
    JSON-parsed into .data when parse=True."""
    args = [
        config.CLAUDE_BIN, "-p", prompt,
        "--output-format", "json",
        "--model", model,
        "--max-turns", "1",
        "--no-session-persistence",
    ]
    async with CLAUDE_SEM:
        try:
            stdout, stderr, rc = await _spawn(args, timeout)
        except asyncio.TimeoutError:
            return ClaudeResult(ok=False, error=f"claude timed out after {timeout}s")
    if rc != 0:
        return ClaudeResult(ok=False, error=stderr.decode()[:500] or f"exit {rc}")
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError:
        return ClaudeResult(ok=False, error="unparseable CLI envelope")
    result = ClaudeResult(
        ok=not envelope.get("is_error", False),
        text=envelope.get("result", ""),
        cost_usd=envelope.get("total_cost_usd", 0.0) or 0.0,
        duration_ms=envelope.get("duration_ms", 0) or 0,
        error=envelope.get("result") if envelope.get("is_error") else None,
    )
    if result.ok and parse:
        try:
            result.data = extract_json(result.text)
        except json.JSONDecodeError:
            result.ok = False
            result.error = "model output was not valid JSON"
    return result


async def run_stream(
    prompt: str,
    *,
    on_event: Callable[[dict], Awaitable[None]],
    model: str = config.RESEARCH_MODEL,
    allowed_tools: tuple[str, ...] = ("WebSearch", "WebFetch"),
    max_turns: int = config.AGENT_MAX_TURNS,
    timeout: float = config.AGENT_TIMEOUT_S,
) -> ClaudeResult:
    """Streaming agent call. Normalizes stream-json lines into simple events
    passed to `on_event`:
      {"kind": "text", "text": ...}
      {"kind": "tool", "tool": ..., "input_summary": ...}
    and returns the final result. Unknown line types are ignored so CLI schema
    additions don't break us.
    """
    args = [
        config.CLAUDE_BIN, "-p", prompt,
        "--output-format", "stream-json",
        "--verbose",
        "--model", model,
        "--allowedTools", *allowed_tools,
        "--max-turns", str(max_turns),
        "--no-session-persistence",
    ]
    async with CLAUDE_SEM:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
        )

        final = ClaudeResult(ok=False, error="stream ended without result event")
        try:
            async with asyncio.timeout(timeout):
                assert proc.stdout is not None
                while True:
                    line = await proc.stdout.readline()
                    if not line:
                        break
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    mtype = msg.get("type")
                    if mtype == "assistant":
                        for block in msg.get("message", {}).get("content", []):
                            if block.get("type") == "text" and block.get("text"):
                                await on_event({"kind": "text", "text": block["text"]})
                            elif block.get("type") == "tool_use":
                                await on_event({
                                    "kind": "tool",
                                    "tool": block.get("name", "?"),
                                    "input_summary": json.dumps(
                                        block.get("input", {})
                                    )[:160],
                                })
                    elif mtype == "result":
                        final = ClaudeResult(
                            ok=not msg.get("is_error", False),
                            text=msg.get("result", "") or "",
                            cost_usd=msg.get("total_cost_usd", 0.0) or 0.0,
                            duration_ms=msg.get("duration_ms", 0) or 0,
                            error=msg.get("result") if msg.get("is_error") else None,
                        )
                await proc.wait()
        except (asyncio.TimeoutError, TimeoutError):
            proc.kill()
            await proc.wait()
            return ClaudeResult(ok=False, error=f"agent timed out after {timeout}s")
    return final
