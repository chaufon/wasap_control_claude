"""Core message dispatcher: project/session state, command handling,
attachment staging, progress updates, and Claude Code invocation
(SPEC.md §5, §7)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .claude_code import ClaudeCodeInvocation, ClaudeCodeRunner, consume
from .commands import HELP_TEXT, Command, parse_command
from .config import Config, ProjectConfig
from .formatting import format_response
from .models import IncomingMessage, OutgoingMessage
from .redis_client import RedisBus
from .state import (
    DynamicProjectEntry,
    ToggleState,
    add_dynamic_project,
    load_dynamic_projects,
    load_state,
    save_state,
)
from .transcription import Transcriber

logger = logging.getLogger(__name__)

# Flat names only — no slashes/dots/".." (SPEC.md §7.2 Q4): this makes an
# entire class of path-traversal bugs structurally impossible rather than
# relying on careful normalization.
_VALID_PROJECT_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")


@dataclass
class ProjectState:
    config: ProjectConfig
    session_id: str | None = None
    invocation: ClaudeCodeInvocation | None = None


class ProgressBatcher:
    """Buffers tool-call names and flushes a summary at a fixed interval
    while a task is running (SPEC.md §5.4)."""

    def __init__(
        self, interval_seconds: int, send: Callable[[str], Awaitable[None]]
    ) -> None:
        self._interval = interval_seconds
        self._send = send
        self._buffer: list[str] = []
        self._task: asyncio.Task[None] | None = None

    async def record(self, tool_name: str) -> None:
        self._buffer.append(tool_name)

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            await self._flush()

    async def _flush(self) -> None:
        if not self._buffer:
            return
        summary = self._summarize(self._buffer)
        self._buffer = []
        await self._send(f"🔧 {summary}")

    @staticmethod
    def _summarize(tool_calls: list[str]) -> str:
        counts: dict[str, int] = {}
        for name in tool_calls:
            counts[name] = counts.get(name, 0) + 1
        parts = [f"{name} x{count}" if count > 1 else name for name, count in counts.items()]
        return ", ".join(parts)

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task


class Dispatcher:
    def __init__(
        self,
        config: Config,
        bus: RedisBus,
        runner: ClaudeCodeRunner,
        transcriber: Transcriber,
    ) -> None:
        self._config = config
        self._bus = bus
        self._runner = runner
        self._transcriber = transcriber
        self._projects: dict[str, ProjectState] = {
            p.name: ProjectState(config=p) for p in config.projects
        }
        self._merge_dynamic_projects()
        self._active_project = config.default_project.name
        self._toggle: ToggleState = load_state()

    def _merge_dynamic_projects(self) -> None:
        """Fold in projects created via /create (SPEC.md §7.2), skipping any
        whose folder has since been removed or that collide with a
        statically-configured project name."""
        for entry in load_dynamic_projects():
            if entry.name in self._projects:
                logger.warning(
                    "dynamic project '%s' collides with a configured project; ignoring",
                    entry.name,
                )
                continue
            path = Path(entry.path)
            if not path.is_dir():
                logger.warning(
                    "dynamic project '%s' path no longer exists; skipping: %s",
                    entry.name,
                    path,
                )
                continue
            self._projects[entry.name] = ProjectState(
                config=ProjectConfig(name=entry.name, path=path)
            )

    async def run(self) -> None:
        async for message in self._bus.incoming_messages():
            asyncio.create_task(self._handle_safely(message))

    async def _handle_safely(self, message: IncomingMessage) -> None:
        try:
            await self._handle(message)
        except Exception:
            logger.exception("unhandled error processing message %s", message.message_id)
            await self._reply("❌ Internal error — see local log for detail.", message)

    async def _handle(self, message: IncomingMessage) -> None:
        # Defense-in-depth: the bridge already scopes to the self-chat and
        # applies its own allow-list check (SPEC.md §4.1, §9).
        if message.sender_jid != self._config.whatsapp.allowed_sender_jid:
            logger.warning("ignoring message from non-allowed sender %s", message.sender_jid)
            return

        if message.type == "text":
            text = message.text or ""
            command = parse_command(text)
            if command is not None:
                # Commands (including /enable) always run, even while disabled —
                # otherwise there'd be no way to turn Claude back on.
                await self._handle_command(command, message)
                return
            if not self._toggle.claude_enabled:
                return  # personal note/backup content: leave it alone (SPEC.md addendum)
            await self._handle_prompt(text, message)
            return

        if message.type == "voice":
            if not self._toggle.claude_enabled:
                return  # skip transcription entirely — nothing to do with it while disabled
            if not message.media_path:
                await self._reply("❌ Voice message had no audio attached.", message)
                return
            transcript = await self._transcriber.transcribe_async(Path(message.media_path))
            if not transcript:
                await self._reply("❌ Could not transcribe that voice note.", message)
                return
            await self._reply(f"🎙️ Heard: “{transcript}”", message)
            await self._handle_prompt(transcript, message)
            return

        if message.type == "attachment":
            if not self._toggle.claude_enabled:
                return  # skip staging entirely — nothing to do with it while disabled
            if not message.media_path:
                await self._reply("❌ Attachment message had no file.", message)
                return
            prompt_text = await self._stage_attachment(message)
            await self._handle_prompt(prompt_text, message)
            return

    async def _handle_command(self, command: Command, message: IncomingMessage) -> None:
        if command.name == "project":
            if not command.arg:
                await self._reply("Usage: /project <name>", message)
                return
            if command.arg not in self._projects:
                names = ", ".join(self._projects)
                await self._reply(
                    f"❌ Unknown project '{command.arg}'. Known projects: {names}", message
                )
                return
            self._active_project = command.arg
            await self._reply(f"✅ Switched to project '{command.arg}'.", message)
            return

        if command.name == "projects":
            lines = [f"• {p.config.name}: {p.config.path}" for p in self._projects.values()]
            marker = f"\n\nActive: {self._active_project}"
            await self._reply("Configured projects:\n" + "\n".join(lines) + marker, message)
            return

        if command.name == "create":
            await self._handle_create(command, message)
            return

        if command.name == "new":
            state = self._projects[self._active_project]
            state.session_id = None
            await self._reply(f"🆕 Started a new session for '{self._active_project}'.", message)
            return

        if command.name == "stop":
            state = self._projects[self._active_project]
            if state.invocation is None:
                await self._reply("Nothing is running right now.", message)
                return
            # The confirmation is sent by the in-flight _handle_prompt task
            # once consume() observes the cancellation, to avoid duplicate replies.
            await state.invocation.cancel()
            return

        if command.name == "enable":
            self._toggle.claude_enabled = True
            save_state(self._toggle)
            await self._reply("🟢 Claude Code enabled.", message)
            return

        if command.name == "disable":
            self._toggle.claude_enabled = False
            save_state(self._toggle)
            await self._reply(
                "🔴 Claude Code disabled. Messages won't be sent to Claude Code until /enable.",
                message,
            )
            return

        if command.name == "status":
            state = self._projects[self._active_project]
            toggle_label = "🟢 enabled" if self._toggle.claude_enabled else "🔴 disabled"
            busy_label = "busy" if state.invocation is not None else "idle"
            await self._reply(
                f"{toggle_label}\nproject: {self._active_project} ({busy_label})",
                message,
            )
            return

        if command.name == "help":
            await self._reply(HELP_TEXT, message)
            return

        await self._reply(f"❌ Unknown command '/{command.name}'. Send /help for the list.", message)

    async def _handle_create(self, command: Command, message: IncomingMessage) -> None:
        if self._config.project_root is None:
            await self._reply(
                "❌ /create is not configured — set project_root in config.yaml first.",
                message,
            )
            return

        if not command.arg:
            await self._reply("Usage: /create <name>", message)
            return

        name = command.arg
        if not _VALID_PROJECT_NAME_RE.match(name):
            await self._reply(
                "❌ Invalid project name. Use only letters, numbers, hyphens, and "
                "underscores (1-100 characters) — no slashes or nested paths.",
                message,
            )
            return

        if name in self._projects:
            await self._reply(
                f"❌ Project '{name}' already exists. Use /project {name} to switch to it.",
                message,
            )
            return

        path = self._config.project_root / name
        if path.exists():
            await self._reply(
                f"❌ A folder named '{name}' already exists under "
                f"{self._config.project_root} but isn't a registered project. "
                "Pick a different name.",
                message,
            )
            return

        path.mkdir(parents=True)
        self._projects[name] = ProjectState(config=ProjectConfig(name=name, path=path))
        add_dynamic_project(DynamicProjectEntry(name=name, path=str(path)))
        self._active_project = name
        await self._reply(f"✅ Created and switched to project '{name}' ({path}).", message)

    async def _handle_prompt(self, prompt: str, message: IncomingMessage) -> None:
        state = self._projects[self._active_project]

        if state.invocation is not None:
            await self._reply(
                f"⏳ Still working on your previous request for '{self._active_project}'. "
                "Send /stop to cancel it, or wait.",
                message,
            )
            return

        await self._reply("⏳ Working on it...", message)

        invocation = await self._runner.start(
            cwd=state.config.path,
            prompt=prompt,
            resume_session_id=state.session_id,
        )
        state.invocation = invocation

        progress = ProgressBatcher(
            interval_seconds=self._config.progress_updates.interval_seconds,
            send=lambda summary: self._reply(summary, message),
        )
        await progress.start()
        try:
            result = await consume(
                invocation,
                on_tool_call=progress.record,
                timeout_seconds=self._config.claude_code.task_timeout_seconds,
            )
        finally:
            await progress.stop()
            state.invocation = None

        if result.session_id:
            state.session_id = result.session_id

        if result.cancelled:
            await self._reply("🛑 Stopped.", message)
            return

        if result.is_error:
            summary = (result.error_detail or "unknown error").splitlines()[0][:300]
            logger.error(
                "claude code failed for project=%s: %s",
                self._active_project,
                result.error_detail,
            )
            await self._reply(f"❌ Claude Code failed: {summary}", message)
            return

        for chunk in format_response(result.text):
            await self._reply(chunk, message)

    async def _stage_attachment(self, message: IncomingMessage) -> str:
        state = self._projects[self._active_project]
        assert message.media_path is not None
        source = Path(message.media_path)
        inbox = state.config.path / "whatsapp-inbox"
        inbox.mkdir(exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
        dest = inbox / f"{timestamp}-{source.name}"
        await asyncio.to_thread(shutil.move, str(source), str(dest))
        relative = dest.relative_to(state.config.path)
        caption = f'User said: "{message.text}" — ' if message.text else ""
        return f"{caption}attached file: {relative}"

    async def _reply(self, text: str, message: IncomingMessage) -> None:
        await self._bus.publish_outgoing(
            OutgoingMessage(text=text, in_reply_to=message.message_id)
        )
