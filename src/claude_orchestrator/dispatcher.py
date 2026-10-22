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
from .projects import ProjectScan, render_listing, scan_project_root, sort_key
from .outbox import MAX_FILE_SIZE_BYTES, collect_outgoing_files, content_hash8
from .redis_client import RedisBus
from .remote_control import LaunchResult, RemoteControl, session_name
from .state import PersistedState, load_state, save_state
from .transcription import Transcriber
from .tts import Synthesizer
from .voice_summary import VOICE_SUMMARY_SYSTEM_PROMPT, split_reply

logger = logging.getLogger(__name__)

# One folder name — no slashes/dots/".." (SPEC.md §7.2 Q4). /create checks
# each new folder name against this, and its parent against the known
# containers, which makes an entire class of path-traversal bugs
# structurally impossible rather than relying on careful normalization.
_VALID_PROJECT_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")

# Anything starting with "/" is unambiguously an attempted command, never a
# personal note, so it gets an explicit notice while disabled — unlike plain
# text/voice/attachments, which stay silent (SPEC.md §7.1/§7.3).
_DISABLED_NOTICE = "🔴 Claude Code is disabled. Send /enable first."

# /new eagerly creates the replacement session right away (cheap model, inert
# prompt) rather than waiting for the next real message, so a service restart
# in between never silently resumes the session /new was meant to replace.
_BOOTSTRAP_MODEL = "claude-haiku-4-5-20251001"
_BOOTSTRAP_PROMPT = "Reply with just the word OK. Do not read or modify any files."
_BOOTSTRAP_TIMEOUT_SECONDS = 30


def _stage_into_inbox(source: Path, project_path: Path, timestamp: str) -> Path:
    """Move `source` into <project_path>/whatsapp-inbox/, content-hash-suffixed
    so a same-named file staged at a different time never silently overwrites
    an earlier one (SPEC.md §5.9's outbox naming applies the same idea).
    Shared by a message's own attachment and any quoted attachment (§5.9)."""
    inbox = project_path / "whatsapp-inbox"
    inbox.mkdir(exist_ok=True)
    dest = inbox / f"{timestamp}-{source.stem}_{content_hash8(source)}{source.suffix}"
    shutil.move(str(source), str(dest))
    return dest.relative_to(project_path)


def _describe_launch(result: LaunchResult) -> str:
    """One WhatsApp line for a /project or /create launch (SPEC.md §7.7)."""
    url = f" {result.url}" if result.url else ""
    if result.status == "started":
        return f"🟢 {result.session} is live. Open the Code tab.{url}"
    if result.status == "running":
        return f"🟢 {result.session} is already live.{url}"
    if result.status == "pending":
        return (
            f"⏳ {result.session} is still starting. Check /status in a moment.\n"
            f"{result.detail}"
        ).rstrip()
    return f"❌ {result.session} failed to start:\n{result.detail}".rstrip()


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
        synthesizer: Synthesizer,
        remote_control: RemoteControl,
    ) -> None:
        self._config = config
        self._bus = bus
        self._runner = runner
        self._transcriber = transcriber
        self._synthesizer = synthesizer
        self._remote_control = remote_control
        self._projects: dict[str, ProjectState] = {
            p.name: ProjectState(config=p) for p in config.projects
        }
        self._state: PersistedState = load_state()
        for project_name, project_state in self._projects.items():
            project_state.session_id = self._state.session_ids.get(project_name)
        self._active_project = self._restore_active_project()
        found = self._scan().containers
        for container in config.project_containers:
            if container not in found:
                logger.warning(
                    "project container %r not found under %s; skipping it",
                    container,
                    config.project_root,
                )

    def _adopt_project(self, name: str, path: Path) -> None:
        """Register a project_root-discovered folder the first time it's
        switched to this run (SPEC.md §7.2) — a no-op for names already
        known, whether static or previously adopted."""
        if name not in self._projects:
            self._projects[name] = ProjectState(
                config=ProjectConfig(name=name, path=path),
                session_id=self._state.session_ids.get(name),
            )

    def _restore_active_project(self) -> str:
        """The active project persists across restarts (state.json); fall
        back to config.default_project if the persisted name no longer
        resolves (e.g. removed from config.yaml or its project_root folder
        deleted)."""
        persisted = self._state.active_project
        if persisted is not None:
            all_projects = self._all_known_projects()
            if persisted in all_projects:
                self._adopt_project(persisted, all_projects[persisted])
                return persisted
            logger.warning(
                "persisted active project %r no longer exists; falling back to %r",
                persisted,
                self._config.default_project.name,
            )
        return self._config.default_project.name

    def _set_active_project(self, name: str) -> None:
        self._active_project = name
        self._state.active_project = name
        save_state(self._state)

    def _remember_session(self, name: str, session_id: str) -> None:
        self._projects[name].session_id = session_id
        self._state.session_ids[name] = session_id
        save_state(self._state)

    async def _bootstrap_session(self, name: str) -> str | None:
        """Eagerly create /new's replacement session (cheap model, inert
        prompt) instead of waiting for the next real message — returns None,
        leaving the previous session untouched, if this fails for any reason."""
        try:
            invocation = await self._runner.start(
                cwd=self._projects[name].config.path,
                prompt=_BOOTSTRAP_PROMPT,
                resume_session_id=None,
                model=_BOOTSTRAP_MODEL,
            )
            result = await consume(
                invocation,
                on_tool_call=None,
                timeout_seconds=_BOOTSTRAP_TIMEOUT_SECONDS,
            )
        except Exception:
            logger.exception("failed to bootstrap a new session for %r", name)
            return None
        if result.is_error or not result.session_id:
            logger.warning(
                "bootstrap session for %r came back as an error: %s", name, result.error_detail
            )
            return None
        return result.session_id

    def _scan(self) -> ProjectScan:
        """Live view of project_root's non-hidden subfolders and those of
        its project_containers (SPEC.md §7.2) — every one of them is
        automatically an available project, no registration step required.
        Re-scanned on every call rather than cached, so a folder created
        outside the bot (or by /create) shows up immediately without an
        orchestrator restart."""
        return scan_project_root(self._config.project_root, self._config.project_containers)

    def _all_known_projects(self) -> dict[str, Path]:
        """Every available project name -> path: config.yaml's static list
        plus every project_root subfolder, deduplicated by name (a static
        entry always wins) *and* by resolved path — a project_root folder
        whose basename differs from a project already known by that same
        path (e.g. a static entry named "wasap-grill-me" pointing at a
        folder literally called wasap_grill_me) must not appear a second
        time under its own basename, or the two names would end up as two
        independent ProjectState entries pointed at one directory — each
        with its own "busy" check, risking two `claude` processes racing
        against the same cwd. Sorted by path parts for a stable /projects
        numbering that keeps a container's children together."""
        combined: dict[str, Path] = {
            name: state.config.path for name, state in self._projects.items()
        }
        known_paths = {path.resolve() for path in combined.values()}
        for name, path in self._scan().projects.items():
            if name in combined or path.resolve() in known_paths:
                continue
            combined[name] = path
            known_paths.add(path.resolve())
        return dict(sorted(combined.items(), key=lambda item: sort_key(item[0])))

    def _format_project_line(self, name: str, path: Path) -> str:
        """Just the name for anything under project_root — the path is
        redundant once every project shares that root (SPEC.md §7.2). A
        static config.yaml entry pointing outside project_root still shows
        its path, since there's no shared root to omit it against."""
        root = self._config.project_root
        if root is not None and path.resolve().is_relative_to(root.resolve()):
            return name
        return f"{name}: {path}"

    def _resolve_project_name(self, arg: str) -> str | None:
        """Resolve /project's argument — a name or a 1-based /projects list
        number — to a known project name, lazily adopting a project_root
        folder that hasn't been switched to yet this run (SPEC.md §7.2).
        Once adopted it behaves exactly like any statically-configured
        project — session continuity included — for the rest of this
        process's lifetime; nothing is persisted to disk, so an untouched
        folder is simply rediscovered live next time (SPEC.md §7.2)."""
        all_projects = self._all_known_projects()
        if arg.isdigit():
            names = list(all_projects)
            index = int(arg) - 1
            if not (0 <= index < len(names)):
                return None
            name = names[index]
        elif arg in all_projects:
            name = arg
        else:
            return None

        self._adopt_project(name, all_projects[name])
        return name

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
            if not self._state.claude_enabled:
                return  # personal note/backup content: leave it alone (SPEC.md §7.1)
            prefix = await self._build_reply_context(message)
            await self._handle_prompt(prefix + text, message)
            return

        if message.type == "voice":
            if not self._state.claude_enabled:
                return  # skip transcription entirely — nothing to do with it while disabled
            if not message.media_path:
                await self._reply("❌ Voice message had no audio attached.", message)
                return
            transcript = await self._transcriber.transcribe_async(Path(message.media_path))
            if not transcript:
                await self._reply("❌ Could not transcribe that voice note.", message)
                return
            await self._reply(f"🎙️ Heard: “{transcript}”", message)
            prefix = await self._build_reply_context(message)
            await self._handle_prompt(prefix + transcript, message)
            return

        if message.type == "attachment":
            if not self._state.claude_enabled:
                return  # skip staging entirely — nothing to do with it while disabled
            if not message.media_path:
                await self._reply("❌ Attachment message had no file.", message)
                return
            prompt_text = await self._stage_attachment(message)
            prefix = await self._build_reply_context(message)
            await self._handle_prompt(prefix + prompt_text, message)
            return

    async def _handle_command(self, command: Command, message: IncomingMessage) -> None:
        if command.name == "project":
            if not command.arg:
                await self._reply("Usage: /project <name-or-number>", message)
                return
            container = command.arg.rstrip("/")
            if container in self._scan().containers:
                lines = render_listing(
                    self._all_known_projects(),
                    self._scan().containers,
                    self._format_project_line,
                    under=container,
                )
                await self._reply(
                    f"📁 {container} is a folder of projects. Pick one:\n" + "\n".join(lines),
                    message,
                )
                return
            name = self._resolve_project_name(command.arg)
            if name is None:
                await self._reply(
                    f"❌ Unknown project '{command.arg}'. Send /projects for the list.", message
                )
                return
            self._set_active_project(name)
            live_line = await self._launch_remote_control(name)
            await self._reply(f"✅ Switched to project '{name}'.\n{live_line}", message)
            return

        if command.name == "projects":
            lines = render_listing(
                self._all_known_projects(), self._scan().containers, self._format_project_line
            )
            marker = f"\n\nActive: {self._active_project}"
            await self._reply("Available projects:\n" + "\n".join(lines) + marker, message)
            return

        if command.name == "create":
            await self._handle_create(command, message)
            return

        if command.name == "new":
            name = self._active_project
            new_session_id = await self._bootstrap_session(name)
            if new_session_id is None:
                await self._reply(
                    f"❌ Couldn't start a new session for '{name}' "
                    "(still on the previous one) — try again.",
                    message,
                )
                return
            self._remember_session(name, new_session_id)
            await self._reply(f"🆕 Started a new session for '{name}'.", message)
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

        if command.name == "close":
            await self._handle_close(command, message)
            return

        if command.name == "enable":
            self._state.claude_enabled = True
            save_state(self._state)
            await self._reply("🟢 Claude Code enabled.", message)
            return

        if command.name == "disable":
            self._state.claude_enabled = False
            save_state(self._state)
            await self._reply(
                "🔴 Claude Code disabled. Messages won't be sent to Claude Code until /enable.",
                message,
            )
            return

        if command.name == "voice":
            if command.arg not in ("on", "off"):
                await self._reply("Usage: /voice on | off", message)
                return
            self._state.voice_enabled = command.arg == "on"
            save_state(self._state)
            label = "🔊 on" if self._state.voice_enabled else "🔇 off"
            await self._reply(f"Voice replies: {label}", message)
            return

        if command.name == "status":
            state = self._projects[self._active_project]
            toggle_label = "🟢 enabled" if self._state.claude_enabled else "🔴 disabled"
            voice_label = "🔊 on" if self._state.voice_enabled else "🔇 off"
            busy_label = "busy" if state.invocation is not None else "idle"
            # Full session ID, not truncated: `claude --resume <id>` needs it
            # verbatim to continue this conversation from a terminal (only
            # the ID reliably resumes interactively — a /name'd title only
            # resolves in --print mode, not the interactive picker).
            session_label = state.session_id or "none yet"
            live_label = ", ".join(await self._remote_control.running()) or "none"
            await self._reply(
                f"{toggle_label}\nvoice replies: {voice_label}\n"
                f"project: {self._active_project} ({busy_label})\n"
                f"session: {session_label}\n"
                f"remote control: {live_label}",
                message,
            )
            return

        if command.name == "help":
            await self._reply(HELP_TEXT, message)
            return

        if command.name == "c":
            # Escape hatch for Claude Code's own slash commands/skills (e.g. "/c
            # /usage"), which would otherwise be swallowed by the command table
            # above. Content bound for Claude, so it honors the toggle like any
            # other prompt — unlike the meta-commands above (SPEC.md §7.3).
            if not self._state.claude_enabled:
                await self._reply(_DISABLED_NOTICE, message)
                return
            if not command.arg:
                await self._reply("Usage: /c <text>", message)
                return
            prefix = await self._build_reply_context(message)
            await self._handle_prompt(prefix + command.arg, message)
            return

        if not self._state.claude_enabled:
            await self._reply(_DISABLED_NOTICE, message)
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
            await self._reply("Usage: /create <name> or /create <container>/<name>", message)
            return

        name = command.arg
        parent, _, leaf = name.rpartition("/")
        if not _VALID_PROJECT_NAME_RE.match(leaf):
            await self._reply(
                "❌ Invalid project name. Use only letters, numbers, hyphens, and "
                "underscores (1-100 characters).",
                message,
            )
            return
        containers = self._scan().containers
        if parent and parent not in containers:
            listed = ", ".join(containers) or "none"
            await self._reply(
                f"❌ '{parent}' is not a project container. Containers: {listed}.",
                message,
            )
            return

        path = self._config.project_root / name
        if name in self._all_known_projects() or path.exists():
            await self._reply(
                f"❌ '{name}' already exists. Use /project {name} to switch to it.",
                message,
            )
            return

        path.mkdir(parents=True)
        self._projects[name] = ProjectState(config=ProjectConfig(name=name, path=path))
        self._set_active_project(name)
        live_line = await self._launch_remote_control(name)
        await self._reply(
            f"✅ Created and switched to project '{name}' ({path}).\n{live_line}", message
        )

    async def _launch_remote_control(self, name: str) -> str:
        """Start the project's Remote Control server, unless it is already
        live, and describe the outcome in one WhatsApp-ready line (SPEC.md
        §7.7). The project switch stands whatever the outcome."""
        result = await self._remote_control.launch(name, self._projects[name].config.path)
        return _describe_launch(result)

    async def _handle_close(self, command: Command, message: IncomingMessage) -> None:
        name = self._active_project
        if command.arg:
            resolved = self._resolve_project_name(command.arg)
            if resolved is None:
                await self._reply(
                    f"❌ Unknown project '{command.arg}'. Send /projects for the list.", message
                )
                return
            name = resolved
        session = session_name(name)
        if await self._remote_control.close(name):
            await self._reply(f"⚫ Closed {session}.", message)
        else:
            await self._reply(f"{session} was not running.", message)

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
            extra_system_prompt=(
                VOICE_SUMMARY_SYSTEM_PROMPT if self._state.voice_enabled else None
            ),
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
            self._remember_session(self._active_project, result.session_id)

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

        text_for_reply, voice_text = split_reply(result.text)

        for chunk in format_response(text_for_reply):
            await self._reply(chunk, message)

        if self._state.voice_enabled and result.text.strip():
            await self._reply_voice(voice_text, message)

        await self._send_outbox_files(state.config.path, message)

    async def _reply_voice(self, text: str, message: IncomingMessage) -> None:
        try:
            audio_path = await self._synthesizer.synthesize_async(text)
        except Exception:
            logger.exception("voice synthesis failed for message %s", message.message_id)
            return  # text reply already sent; voice is a best-effort extra (SPEC.md §7.4)
        await self._bus.publish_outgoing(
            OutgoingMessage(
                in_reply_to=message.message_id,
                media_path=str(audio_path),
                media_mime_type="audio/ogg; codecs=opus",
            )
        )

    async def _send_outbox_files(self, project_path: Path, message: IncomingMessage) -> None:
        result = await asyncio.to_thread(collect_outgoing_files, project_path)

        for path, size in result.oversized:
            size_mb = size / (1024 * 1024)
            cap_mb = MAX_FILE_SIZE_BYTES // (1024 * 1024)
            await self._reply(
                f"❌ {path.name} is {size_mb:.0f}MB, over the {cap_mb}MB limit — "
                "left in whatsapp-outbox/, not sent.",
                message,
            )

        for outbox_file in result.sent:
            await self._bus.publish_outgoing(
                OutgoingMessage(
                    in_reply_to=message.message_id,
                    media_path=str(outbox_file.path),
                    media_mime_type=outbox_file.mime_type,
                    media_filename=outbox_file.path.name,
                    caption=outbox_file.path.name,
                    delete_after_send=False,
                )
            )

    async def _stage_attachment(self, message: IncomingMessage) -> str:
        state = self._projects[self._active_project]
        assert message.media_path is not None
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
        relative = await asyncio.to_thread(
            _stage_into_inbox, Path(message.media_path), state.config.path, timestamp
        )
        caption = f'User said: "{message.text}" — ' if message.text else ""
        return f"{caption}attached file: {relative}"

    async def _build_reply_context(self, message: IncomingMessage) -> str:
        """Prefix for messages that quote/reply to an earlier one (SPEC.md
        §5.9/§7.6) — applies regardless of this message's own type, since a
        reply can itself be text, voice, or an attachment."""
        if message.quoted_media_path:
            try:
                state = self._projects[self._active_project]
                timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
                relative = await asyncio.to_thread(
                    _stage_into_inbox, Path(message.quoted_media_path), state.config.path, timestamp
                )
                return f"Replying to attached file: {relative}\n\n"
            except OSError:
                logger.exception("failed to stage quoted media %s", message.quoted_media_path)
        if message.quoted_text:
            return f'Replying to: "{message.quoted_text}"\n\n'
        if message.quoted_media_type_label:
            return f"(Replying to a {message.quoted_media_type_label} that couldn't be retrieved)\n\n"
        return ""

    async def _reply(self, text: str, message: IncomingMessage) -> None:
        await self._bus.publish_outgoing(
            OutgoingMessage(text=text, in_reply_to=message.message_id)
        )
