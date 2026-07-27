"""Claude Code CLI wrapper (SPEC.md §4.4).

Invokes `claude -p ... --output-format stream-json --verbose
--dangerously-skip-permissions` as a subprocess confined to a project's
working directory, parses the streamed JSON event log, and exposes a
handle that lets the dispatcher cancel a running task (SPEC.md §5.8).

Event shapes below were captured from a live run of Claude Code CLI
2.1.220 and are not part of any public schema guarantee — re-verify
against the installed CLI version if invocations start failing to parse.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from .outbox import OUTBOX_SYSTEM_PROMPT


@dataclass
class ClaudeCodeResult:
    text: str
    is_error: bool
    session_id: str | None
    cancelled: bool = False
    duration_ms: int | None = None
    error_detail: str | None = None


class ClaudeCodeInvocation:
    """A single running (or finished) `claude` subprocess."""

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self.process = process
        self.session_id: str | None = None

    async def events(self) -> AsyncIterator[dict]:
        assert self.process.stdout is not None
        async for raw_line in self.process.stdout:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            session_id = event.get("session_id")
            if session_id:
                self.session_id = session_id
            yield event

    async def cancel(self, grace_seconds: float = 5.0) -> None:
        """Send SIGTERM, escalate to SIGKILL after a grace period (SPEC.md §5.8)."""
        if self.process.returncode is not None:
            return
        self.process.terminate()
        try:
            await asyncio.wait_for(self.process.wait(), timeout=grace_seconds)
        except asyncio.TimeoutError:
            self.process.kill()
            await self.process.wait()


class ClaudeCodeRunner:
    def __init__(self, binary: str = "claude") -> None:
        self.binary = binary

    async def start(
        self,
        *,
        cwd: Path,
        prompt: str,
        resume_session_id: str | None,
    ) -> ClaudeCodeInvocation:
        args = [
            self.binary,
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
            "--append-system-prompt",
            OUTBOX_SYSTEM_PROMPT,
        ]
        if resume_session_id:
            args += ["--resume", resume_session_id]

        process = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(cwd),
            # asyncio's default readline() buffer is 64KiB, which a single
            # stream-json event can exceed once file content (e.g. a read
            # PDF/document) ends up embedded in a tool-result line — raise it
            # generously rather than truncating/crashing mid-task.
            limit=32 * 1024 * 1024,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        return ClaudeCodeInvocation(process)


OnToolCall = Callable[[str], Awaitable[None]]


async def consume(
    invocation: ClaudeCodeInvocation,
    *,
    on_tool_call: OnToolCall | None,
    timeout_seconds: int,
) -> ClaudeCodeResult:
    """Drain an invocation's event stream, reporting tool calls as they occur
    and returning the final result (or a timeout/cancellation outcome)."""
    final_result: dict | None = None
    start = time.monotonic()

    async def _read() -> None:
        nonlocal final_result
        async for event in invocation.events():
            event_type = event.get("type")
            if event_type == "assistant":
                content = event.get("message", {}).get("content", [])
                for block in content:
                    if block.get("type") == "tool_use" and on_tool_call is not None:
                        await on_tool_call(block.get("name", "tool"))
            elif event_type == "result":
                final_result = event

    try:
        await asyncio.wait_for(_read(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        await invocation.cancel()
        return ClaudeCodeResult(
            text="",
            is_error=True,
            session_id=invocation.session_id,
            duration_ms=int((time.monotonic() - start) * 1000),
            error_detail=f"timed out after {timeout_seconds}s",
        )

    await invocation.process.wait()

    if final_result is None:
        stderr = b""
        if invocation.process.stderr is not None:
            stderr = await invocation.process.stderr.read()
        was_signaled = (invocation.process.returncode or 0) < 0
        return ClaudeCodeResult(
            text="",
            is_error=True,
            session_id=invocation.session_id,
            cancelled=was_signaled,
            duration_ms=int((time.monotonic() - start) * 1000),
            error_detail=stderr.decode("utf-8", errors="replace")[:2000]
            or "claude process exited without a result event",
        )

    return ClaudeCodeResult(
        text=final_result.get("result", ""),
        is_error=bool(final_result.get("is_error")),
        session_id=final_result.get("session_id") or invocation.session_id,
        duration_ms=final_result.get("duration_ms"),
    )
