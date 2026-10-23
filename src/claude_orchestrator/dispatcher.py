"""Core message dispatcher: project state, command handling, attachment
staging, and WhatsApp turns in each project's shared live session (SPEC.md
§5, §7)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import shutil
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from .commands import HELP_TEXT, Command, parse_command
from .config import Config, ProjectConfig
from .formatting import format_response
from .live_session import SESSION_PREFIX, LaunchResult, LiveSessions, screen_text, session_name
from .models import IncomingMessage, OutgoingMessage
from .outbox import MAX_FILE_SIZE_BYTES, collect_outgoing_files, content_hash8
from .projects import ProjectScan, render_listing, scan_project_root, sort_key
from .redis_client import RedisBus
from .state import PersistedState, load_state, save_state
from .transcription import Transcriber
from .tts import Synthesizer
from .voice_summary import split_reply

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

# Length of the last-answer preview sent after /project.
_PREVIEW_CHARS = 200
# A /c slash command that has not become a prompt by then is a built-in
# command (/usage, /cost...), which Claude never answers: its reply is the
# screen it draws.
_COMMAND_GRACE_SECONDS = 3
_SCREEN_SETTLE_SECONDS = 2
_CLEAR_WAIT_SECONDS = 10
# A typed message that has not become a prompt by then never will.
_ACCEPT_TIMEOUT_SECONDS = 20
# An interrupted turn ends without a Stop event: the session reporting idle
# this many polls in a row ends it instead.
_IDLE_CONFIRMATIONS = 3
_RECONNECT_WAIT_SECONDS = 15
# The session reports busy for a moment after it answers, so a session is
# only called busy once it stays busy this long.
_BUSY_GRACE_SECONDS = 2

TurnOutcome = Literal["answer", "stopped", "exited", "timeout", "unconfirmed"]


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


def _monospace(text: str) -> str:
    return f"```{text}```"


def _attach_command(session: str) -> str:
    """The command that opens a live session in a terminal on the computer."""
    return _monospace(f"tmux attach -t {session}")


def _describe_launch(result: LaunchResult) -> str:
    """The WhatsApp lines for a live-session launch: where the session is
    in the Claude app, and how to open and leave it on the computer
    (SPEC.md §7.7)."""
    computer = (
        f"🖥️ On the computer: {_attach_command(result.session)}\n"
        "🚪 To leave, close the terminal window or tab; /exit ends the session"
    )
    if result.status in ("started", "running") and result.url:
        verb = "is live" if result.status == "started" else "is already live"
        return f"📱 {result.session} {verb} in the Claude app: {result.url}\n{computer}"
    if result.status in ("running", "pending"):
        return (
            f"🟡 {result.session} is running, not connected to the Claude app yet. Check /status in a moment.\n"
            f"{computer}"
        )
    return f"❌ {result.session} failed to start:\n{result.detail}".rstrip()


def _preview(answer: str) -> str:
    """The first _PREVIEW_CHARS characters of an answer, on one line."""
    text, _ = split_reply(answer)
    flat = " ".join(text.split())
    return flat if len(flat) <= _PREVIEW_CHARS else flat[:_PREVIEW_CHARS].rstrip() + "…"


@dataclass(frozen=True)
class QueuedPrompt:
    text: str
    message: IncomingMessage


@dataclass
class Turn:
    """A WhatsApp message typed into a live session, until it is answered."""

    message: IncomingMessage
    answer: asyncio.Future[str]
    started: asyncio.Event = field(default_factory=asyncio.Event)
    cleared: asyncio.Event = field(default_factory=asyncio.Event)
    prompt_id: str | None = None
    progress: ProgressBatcher | None = None


@dataclass
class ProjectState:
    config: ProjectConfig
    queue: deque[QueuedPrompt] = field(default_factory=deque)
    turn: Turn | None = None
    worker: asyncio.Task[None] | None = None


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
        transcriber: Transcriber,
        synthesizer: Synthesizer,
        live: LiveSessions,
    ) -> None:
        self._config = config
        self._bus = bus
        self._transcriber = transcriber
        self._synthesizer = synthesizer
        self._live = live
        self._projects: dict[str, ProjectState] = {
            p.name: ProjectState(config=p) for p in config.projects
        }
        self._state: PersistedState = load_state()
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
            self._projects[name] = ProjectState(config=ProjectConfig(name=name, path=path))

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
        if self._state.shared_sessions.get(name) == session_id:
            return
        self._state.shared_sessions[name] = session_id
        save_state(self._state)

    def _remember_answer(self, name: str, answer: str) -> None:
        self._state.last_answers[name] = answer
        save_state(self._state)

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
        independent ProjectState entries pointed at one directory, each
        with its own live session in the same cwd. Sorted by path parts for
        a stable /projects numbering that keeps a container's children
        together."""
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
        project for the rest of this process's lifetime; nothing is
        persisted to disk, so an untouched folder is simply rediscovered
        live next time (SPEC.md §7.2)."""
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

    def _project_for_session(self, session: str) -> str | None:
        """The project a live tmux session belongs to, adopting it when it
        was started before this orchestrator run."""
        for name in self._projects:
            if session_name(name) == session:
                return name
        for name, path in self._all_known_projects().items():
            if session_name(name) == session:
                self._adopt_project(name, path)
                return name
        return None

    async def run(self) -> None:
        hooks = asyncio.create_task(self._consume_hook_events())
        try:
            async for message in self._bus.incoming_messages():
                asyncio.create_task(self._handle_safely(message))
        finally:
            hooks.cancel()

    async def _consume_hook_events(self) -> None:
        async for event in self._bus.hook_events():
            try:
                await self._on_hook_event(event)
            except Exception:
                logger.exception("failed to handle hook event %r", event)

    async def _on_hook_event(self, event: dict) -> None:
        """Track the live sessions from their hooks (hook_relay.py): session
        IDs, last answers, and the progress and answer of WhatsApp turns."""
        name = self._project_for_session(str(event.get("live_session", "")))
        if name is None:
            return
        state = self._projects[name]
        turn = state.turn
        kind = event.get("hook_event_name")
        own_turn = turn is not None and turn.prompt_id is not None and event.get("prompt_id") == turn.prompt_id

        if kind == "SessionStart":
            if event.get("session_id"):
                self._remember_session(name, str(event["session_id"]))
            if event.get("source") == "clear" and turn is not None:
                turn.cleared.set()
        elif kind == "UserPromptSubmit":
            if event.get("origin") == "whatsapp" and turn is not None and turn.prompt_id is None:
                turn.prompt_id = str(event.get("prompt_id") or "")
                turn.started.set()
        elif kind == "PostToolUse":
            if own_turn and turn.progress is not None:
                await turn.progress.record(str(event.get("tool_name") or "tool"))
        elif kind == "Stop":
            answer = str(event.get("last_assistant_message") or "")
            if answer.strip():
                self._remember_answer(name, answer)
            if own_turn and not turn.answer.done():
                turn.answer.set_result(answer)

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
            await self._handle_project(command, message)
            return

        if command.name == "projects":
            lines = render_listing(
                self._all_known_projects(), self._scan().containers, self._format_project_line
            )
            await self._reply(
                "📁 *Projects*\n" + "\n".join(lines) + f"\n\n▶️ Active: {self._active_project}", message
            )
            return

        if command.name == "create":
            await self._handle_create(command, message)
            return

        if command.name == "stop":
            await self._handle_stop(message)
            return

        if command.name == "last":
            await self._handle_last(message)
            return

        if command.name == "close":
            await self._handle_close(command, message)
            return

        if command.name == "reconnect":
            await self._handle_reconnect(command, message)
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
            await self._reply(await self._status_text(), message)
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
            # A slash command goes in alone, so the session still sees it
            # as a command; anything else gets the quoted-message context.
            prefix = "" if command.arg.startswith("/") else await self._build_reply_context(message)
            await self._handle_prompt(prefix + command.arg, message)
            return

        if not self._state.claude_enabled:
            await self._reply(_DISABLED_NOTICE, message)
            return

        await self._reply(f"❌ Unknown command '/{command.name}'. Send /help for the list.", message)

    async def _handle_project(self, command: Command, message: IncomingMessage) -> None:
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
        live_line = await self._launch(name)
        await self._reply(f"✅ Switched to project '{name}'.\n{live_line}", message)
        answer = self._state.last_answers.get(name)
        if answer:
            await self._reply(f"💬 *Last answer:*\n{_preview(answer)}\n\nSend /last for all of it.", message)

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
        live_line = await self._launch(name)
        await self._reply(
            f"✅ Created and switched to project '{name}' ({path}).\n{live_line}", message
        )

    async def _launch(self, name: str) -> str:
        """Start the project's live session, unless it is already up,
        resuming its shared session, and describe the outcome in one line
        (SPEC.md §7.7). A project switch stands whatever the outcome."""
        result = await self._live.launch(
            name, self._projects[name].config.path, self._state.shared_sessions.get(name)
        )
        return _describe_launch(result)

    def _project_arg(self, command: Command) -> str | None:
        """A command's optional project argument, or the active project."""
        return self._resolve_project_name(command.arg) if command.arg else self._active_project

    async def _handle_close(self, command: Command, message: IncomingMessage) -> None:
        name = self._project_arg(command)
        if name is None:
            await self._reply(f"❌ Unknown project '{command.arg}'. Send /projects for the list.", message)
            return
        self._projects[name].queue.clear()
        session = session_name(name)
        if await self._live.close(name):
            await self._reply(f"⚫ Closed {session}.", message)
        else:
            await self._reply(f"⚪ {session} was not running.", message)

    async def _handle_reconnect(self, command: Command, message: IncomingMessage) -> None:
        name = self._project_arg(command)
        if name is None:
            await self._reply(f"❌ Unknown project '{command.arg}'. Send /projects for the list.", message)
            return
        session = session_name(name)
        if await self._live.pane_state(session) != "live":
            await self._reply(f"❌ {session} is not running. Send /project {name} to start it.", message)
            return
        info = self._live.process(session)
        if info is not None and info.url:
            await self._reply(f"📱 {session} is already connected: {info.url}", message)
            return
        await self._live.send_text(session, "/remote-control")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _RECONNECT_WAIT_SECONDS
        while loop.time() < deadline:
            await asyncio.sleep(self._live.poll_interval)
            info = self._live.process(session)
            if info is not None and info.url:
                await self._reply(f"📱 Reconnected {session}: {info.url}", message)
                return
        await self._reply(
            f"⚠️ {session} did not reconnect. Check it on the computer: {_attach_command(session)}",
            message,
        )

    async def _handle_stop(self, message: IncomingMessage) -> None:
        """Drop the queued WhatsApp messages and interrupt the running turn,
        wherever it was started."""
        name = self._active_project
        state = self._projects[name]
        session = session_name(name)
        dropped = len(state.queue)
        state.queue.clear()
        info = self._live.process(session)
        running = state.turn is not None or (info is not None and info.busy)
        if running:
            await self._live.send_key(session, "Escape")
        if dropped:
            noun = "message" if dropped == 1 else "messages"
            await self._reply(f"🛑 Dropped {dropped} queued {noun}.", message)
        if running and state.turn is None:
            # A WhatsApp turn reports its own "Stopped" once it ends.
            await self._reply("🛑 Stopped.", message)
        if not dropped and not running:
            await self._reply("Nothing is running right now.", message)

    async def _handle_last(self, message: IncomingMessage) -> None:
        name = self._active_project
        answer = self._state.last_answers.get(name)
        if not answer:
            await self._reply(f"💬 No answer yet in '{name}'.", message)
            return
        text, _ = split_reply(answer)
        for index, chunk in enumerate(format_response(text)):
            await self._reply(f"💬 *Last answer:*\n{chunk}" if index == 0 else chunk, message)

    async def _status_text(self) -> str:
        name = self._active_project
        state = self._projects[name]
        session = session_name(name)
        info = self._live.process(session)
        toggle_label = "🟢 enabled" if self._state.claude_enabled else "🔴 disabled"
        voice_label = "🔊 on" if self._state.voice_enabled else "🔇 off"
        busy_label = "busy" if state.turn is not None or (info is not None and info.busy) else "idle"
        if state.queue:
            busy_label += f", {len(state.queue)} queued"
        # Full session ID, not truncated: `claude --resume <id>` needs it
        # verbatim to continue this conversation from a terminal.
        session_id = (info.session_id if info else None) or self._state.shared_sessions.get(name) or "none yet"
        lines = [
            "📊 *Status*",
            f"• Claude Code: {toggle_label}",
            f"• Voice replies: {voice_label}",
            f"• Project: {name} ({busy_label})",
            f"• Session: {session_id}",
        ]
        if info is not None:
            lines.append(f"• On the computer: {_attach_command(session)}")
        lines += ["", "📱 *Remote Control*", *(await self._remote_control_lines())]
        return "\n".join(lines)

    async def _remote_control_lines(self) -> list[str]:
        """One "◦" line per live session (SPEC.md §7.7): 🟢 connected to the
        app, 🟡 running but not connected, 🔴 exited (reported once, then
        removed), ⚪ started outside WhatsApp."""
        sessions = await self._live.sessions()
        processes = self._live.all_processes()
        by_session = {p.tmux_session: p for p in processes if p.tmux_session}
        lines: list[str] = []
        for session, pane in sessions.items():
            if pane == "dead":
                reason = await self._live.last_line(session)
                lines.append(f"◦ 🔴 {session}: exited" + (f" ({reason})" if reason else ""))
                await self._live.kill(session)
                continue
            info = by_session.get(session)
            if info is None:
                lines.append(f"◦ 🟡 {session}: starting")
            elif info.url:
                lines.append(f"◦ 🟢 {session}: connected, {'busy' if info.busy else 'idle'}")
            else:
                name = self._project_for_session(session)
                hint = f"/reconnect {name}" if name else "/reconnect"
                lines.append(f"◦ 🟡 {session}: not connected to the app · {hint}")
        project_by_path = {path.resolve(): name for name, path in self._all_known_projects().items()}
        outside: set[str] = set()
        for process in processes:
            if not process.bridge_session_id or (process.tmux_session or "").startswith(SESSION_PREFIX):
                continue
            name = project_by_path.get(process.cwd.resolve())
            if name is not None and name not in outside:
                outside.add(name)
                lines.append(f"◦ ⚪ {name}: started outside WhatsApp")
        return lines or ["◦ none"]

    async def _handle_prompt(self, prompt: str, message: IncomingMessage) -> None:
        """Queue a WhatsApp prompt for the active project's live session,
        starting the session first when it is not running (SPEC.md §7.7)."""
        name = self._active_project
        if not await self._ensure_live(name, message):
            return
        state = self._projects[name]
        busy = bool(state.queue) or state.turn is not None or await self._stays_busy(session_name(name))
        state.queue.append(QueuedPrompt(prompt, message))
        if busy:
            await self._reply("⏳ Busy, queued. Send /stop to drop it.", message)
        if state.worker is None or state.worker.done():
            state.worker = asyncio.create_task(self._drain(name))

    async def _stays_busy(self, session: str) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _BUSY_GRACE_SECONDS
        while True:
            info = self._live.process(session)
            if info is None or not info.busy:
                return False
            if loop.time() >= deadline:
                return True
            await asyncio.sleep(self._live.poll_interval)

    async def _ensure_live(self, name: str, message: IncomingMessage) -> bool:
        session = session_name(name)
        if await self._live.pane_state(session) == "live":
            return True
        resumed = name in self._state.shared_sessions
        result = await self._live.launch(name, self._projects[name].config.path, self._state.shared_sessions.get(name))
        if result.status == "failed":
            await self._reply(_describe_launch(result), message)
            return False
        lead = "♻️ Session restarted." if resumed else "🟢 Session started."
        await self._reply(f"{lead}\n{_describe_launch(result)}", message)
        return True

    async def _drain(self, name: str) -> None:
        """Type the project's queued prompts into its session one at a time,
        each once the session is idle."""
        state = self._projects[name]
        session = session_name(name)
        while state.queue:
            info = await self._live.wait_registered(session, _ACCEPT_TIMEOUT_SECONDS)
            if info is None:
                dropped = list(state.queue)
                state.queue.clear()
                for item in dropped:
                    await self._reply(
                        f"❌ {session} is not running. Send /project {name} to start it, then resend.",
                        item.message,
                    )
                return
            if info.busy:
                await asyncio.sleep(self._live.poll_interval)
                continue
            item = state.queue.popleft()
            try:
                await self._run_turn(name, item)
            except Exception:
                logger.exception("turn failed for project=%s", name)
                await self._reply("❌ Internal error — see local log for detail.", item.message)

    async def _run_turn(self, name: str, item: QueuedPrompt) -> None:
        state = self._projects[name]
        session = session_name(name)
        turn = Turn(message=item.message, answer=asyncio.get_running_loop().create_future())
        state.turn = turn
        try:
            if item.text.startswith("/"):
                await self._run_slash_command(name, turn, item.text)
            else:
                await self._reply("⏳ Working on it...", item.message)
                if await self._type(session, item.text):
                    await self._follow_turn(name, turn)
                else:
                    await self._reply(f"❌ Could not type the message into {session}.", item.message)
        finally:
            state.turn = None
            self._live.clear_pending(session)

    async def _type(self, session: str, text: str) -> bool:
        self._live.mark_pending(session, voice=self._state.voice_enabled)
        return await self._live.send_text(session, text)

    async def _run_slash_command(self, name: str, turn: Turn, text: str) -> None:
        """A /c slash command: a skill becomes a normal turn; a built-in
        command replies with what it draws, and /clear with a confirmation."""
        session = session_name(name)
        command = text.split()[0]
        if not await self._type(session, text):
            await self._reply(f"❌ Could not type the command into {session}.", turn.message)
            return
        if command == "/clear":
            try:
                await asyncio.wait_for(turn.cleared.wait(), _CLEAR_WAIT_SECONDS)
            except TimeoutError:
                await self._reply("⚠️ Sent /clear, but the session did not confirm it.", turn.message)
                return
            await self._reply("🧹 Session cleared. The next message starts a fresh conversation.", turn.message)
            return
        try:
            await asyncio.wait_for(turn.started.wait(), _COMMAND_GRACE_SECONDS)
        except TimeoutError:
            self._live.clear_pending(session)
            await asyncio.sleep(_SCREEN_SETTLE_SECONDS)
            screen = screen_text(await self._live.capture(session), command)
            # Closes the panel a command like /usage leaves open.
            await self._live.send_key(session, "Escape")
            reply = f"🖥️ {command}\n```\n{screen}\n```" if screen else f"✅ Sent {command}."
            for chunk in format_response(reply):
                await self._reply(chunk, turn.message)
            return
        await self._reply("⏳ Working on it...", turn.message)
        await self._follow_turn(name, turn)

    async def _follow_turn(self, name: str, turn: Turn) -> None:
        """Relay a started turn's progress, then its answer or how it ended."""
        progress = ProgressBatcher(
            interval_seconds=self._config.progress_updates.interval_seconds,
            send=lambda summary: self._reply(summary, turn.message),
        )
        turn.progress = progress
        await progress.start()
        try:
            outcome, answer = await self._await_answer(name, turn)
        finally:
            await progress.stop()

        message = turn.message
        session = session_name(name)
        if outcome == "answer":
            await self._deliver_answer(name, answer, message)
        elif outcome == "stopped":
            await self._reply("🛑 Stopped.", message)
        elif outcome == "exited":
            await self._reply(f"🔴 {session} exited. Send your message again to restart it.", message)
        elif outcome == "timeout":
            timeout = self._config.claude_code.task_timeout_seconds
            await self._reply(f"❌ No answer after {timeout}s, so the turn was interrupted.", message)
        else:
            await self._reply(
                f"⚠️ {session} did not confirm the message. Check the Claude app, or on the computer: "
                f"{_attach_command(session)}",
                message,
            )

    async def _await_answer(self, name: str, turn: Turn) -> tuple[TurnOutcome, str]:
        """Wait for the turn's Stop event, watching the session meanwhile:
        an interrupted turn ends with no Stop, so a session that stays idle
        ends it too."""
        session = session_name(name)
        loop = asyncio.get_running_loop()
        start = loop.time()
        timeout = self._config.claude_code.task_timeout_seconds
        idle_polls = 0
        while True:
            try:
                return "answer", await asyncio.wait_for(asyncio.shield(turn.answer), self._live.poll_interval)
            except TimeoutError:
                pass
            elapsed = loop.time() - start
            info = self._live.process(session)
            if info is None:
                return "exited", ""
            if not turn.started.is_set():
                if elapsed > _ACCEPT_TIMEOUT_SECONDS:
                    return "unconfirmed", ""
                continue
            idle_polls = 0 if info.busy else idle_polls + 1
            if idle_polls >= _IDLE_CONFIRMATIONS:
                return "stopped", ""
            if elapsed > timeout:
                await self._live.send_key(session, "Escape")
                return "timeout", ""

    async def _deliver_answer(self, name: str, answer: str, message: IncomingMessage) -> None:
        text_for_reply, voice_text = split_reply(answer)
        for chunk in format_response(text_for_reply):
            await self._reply(chunk, message)
        if self._state.voice_enabled and answer.strip():
            await self._reply_voice(voice_text, message)
        await self._send_outbox_files(self._projects[name].config.path, message)

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
