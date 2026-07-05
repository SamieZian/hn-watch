"""Native macOS notifications via osascript.

Zero-dependency and works from an unsigned Python process. Limitation: the
notification is attributed to "Script Editor"; a proper sender identity needs
a signed .app bundle (out of scope, noted in the README).
"""
import asyncio


def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


async def send(title: str, message: str, subtitle: str = "") -> None:
    script = (
        f'display notification "{_escape(message)}"'
        f' with title "{_escape(title)}"'
    )
    if subtitle:
        script += f' subtitle "{_escape(subtitle)}"'
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=10)
    except (OSError, asyncio.TimeoutError):
        pass  # notifications are best-effort
