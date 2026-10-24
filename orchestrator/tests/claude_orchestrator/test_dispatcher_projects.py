import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from claude_orchestrator.commands import Command
from claude_orchestrator.dispatcher import NO_PROJECT_REPLY, Dispatcher
from claude_orchestrator.live_session import ProcessInfo
from claude_orchestrator.models import IncomingMessage
from claude_orchestrator.state import PersistedState

from .conftest import FakeBus, FakeLive, MakeDispatcher

if TYPE_CHECKING:
    from _pytest.logging import LogCaptureFixture

MESSAGE = IncomingMessage(message_id="m1", timestamp="t", sender_jid="me", type="text", text="")
LIVE_LINE = (
    "📱 {session} is live in the Claude app: https://claude.ai/code/session_x\n"
    "🖥️ On the computer: ```tmux attach -t {session}```\n"
    "🚪 To leave, close the terminal window or tab; /exit ends the session"
)


def _run_all(dispatcher: Dispatcher, bus: FakeBus, name: str, arg: str | None = None) -> list[str]:
    bus.sent.clear()
    asyncio.run(dispatcher._handle_command(Command(name=name, arg=arg), MESSAGE))
    return bus.texts


def _run(dispatcher: Dispatcher, bus: FakeBus, name: str, arg: str | None = None) -> str:
    return _run_all(dispatcher, bus, name, arg)[-1]


def test_projects_lists_containers_with_children(make_dispatcher: MakeDispatcher) -> None:
    """/projects shows containers as headers and their children numbered beneath them."""
    dispatcher, bus, _ = make_dispatcher()
    assert _run(dispatcher, bus, "projects") == (
        "📁 *Projects*\n"
        "📁 c\n"
        "  📁 c/car\n"
        "    1. c/car/crash26\n"
        "    2. c/car/maintenance\n"
        "  3. c/fitnotes\n"
        "  4. c/whey\n"
        "5. chaufon\n"
        "6. main\n\n"
        "▶️ Active: main"
    )


def test_project_switches_to_nested_project_by_name_and_number(make_dispatcher: MakeDispatcher) -> None:
    """A nested project can be selected by its relative path or its list number."""
    dispatcher, bus, _ = make_dispatcher()
    assert _run(dispatcher, bus, "project", "c/car/maintenance") == (
        "✅ Switched to project 'c/car/maintenance'.\n" + LIVE_LINE.format(session="rc-c-car-maintenance")
    )
    assert _run(dispatcher, bus, "project", "4").startswith("✅ Switched to project 'c/whey'.\n")


def test_project_resumes_the_shared_session(make_dispatcher: MakeDispatcher) -> None:
    """A project with a stored shared session starts by resuming it."""
    dispatcher, bus, live = make_dispatcher(state=PersistedState(shared_sessions={"chaufon": "abc"}))
    _run(dispatcher, bus, "project", "chaufon")
    _run(dispatcher, bus, "project", "c/whey")
    assert live.launches == [("chaufon", "abc"), ("c/whey", None)]


def test_project_sends_a_preview_of_the_last_answer(make_dispatcher: MakeDispatcher) -> None:
    """After the switch, a second message previews the project's last answer in 200 characters."""
    answer = "**Done.** " + "word " * 100 + "\n## 🔊 Audio Summary\nspoken"
    dispatcher, bus, _ = make_dispatcher(state=PersistedState(last_answers={"chaufon": answer}))
    switched, preview = _run_all(dispatcher, bus, "project", "chaufon")
    assert switched.startswith("✅ Switched to project 'chaufon'.")
    body = preview.split("\n")[1]
    assert preview.startswith("💬 *Last answer:*\n**Done.** word word")
    assert len(body) <= 201 and body.endswith("…")
    assert preview.endswith("\n\nSend /last for all of it.")
    assert len(_run_all(dispatcher, bus, "project", "c/whey")) == 1


def test_project_on_container_lists_its_children(make_dispatcher: MakeDispatcher) -> None:
    """Selecting a container replies with its subtree instead of switching."""
    dispatcher, bus, _ = make_dispatcher()
    assert _run(dispatcher, bus, "project", "c/car") == (
        "📁 c/car is a folder of projects. Pick one:\n1. c/car/crash26\n2. c/car/maintenance"
    )
    assert dispatcher._active_project == "main"


def test_create_inside_container(root: Path, make_dispatcher: MakeDispatcher) -> None:
    """/create <container>/<name> makes the folder, switches to it and starts its session."""
    dispatcher, bus, _ = make_dispatcher()
    reply = _run(dispatcher, bus, "create", "c/car/newapp")
    assert reply.startswith("✅ Created and switched to project 'c/car/newapp'")
    assert reply.endswith(LIVE_LINE.format(session="rc-c-car-newapp"))
    assert (root / "c" / "car" / "newapp").is_dir()


def test_create_rejects_parent_that_is_not_a_container(root: Path, make_dispatcher: MakeDispatcher) -> None:
    """/create refuses a parent folder that is not a listed container."""
    dispatcher, bus, _ = make_dispatcher()
    reply = _run(dispatcher, bus, "create", "chaufon/sub")
    assert reply == "❌ 'chaufon' is not a project container. Containers: c, c/car."
    assert not (root / "chaufon" / "sub").exists()


@pytest.mark.parametrize("arg", ["../escape", "c/../x", "c/.hidden", "c/"])
def test_create_rejects_unsafe_names(make_dispatcher: MakeDispatcher, arg: str) -> None:
    """/create refuses traversal, hidden and empty folder names."""
    dispatcher, bus, _ = make_dispatcher()
    assert _run(dispatcher, bus, "create", arg).startswith("❌")


def test_missing_container_logs_a_warning(make_dispatcher: MakeDispatcher, caplog: "LogCaptureFixture") -> None:
    """A configured container with no folder is skipped with a warning at startup."""
    with caplog.at_level(logging.WARNING):
        dispatcher, bus, _ = make_dispatcher(containers=("c", "gone"))
    assert "project container 'gone' not found" in caplog.text
    assert "gone" not in _run(dispatcher, bus, "projects")


def test_project_twice_reuses_the_live_session(make_dispatcher: MakeDispatcher) -> None:
    """A second /project for the same project reports the session already live."""
    dispatcher, bus, _ = make_dispatcher()
    _run(dispatcher, bus, "project", "c/whey")
    assert _run(dispatcher, bus, "project", "c/whey") == (
        "✅ Switched to project 'c/whey'.\n"
        "📱 rc-c-whey is already live in the Claude app: https://claude.ai/code/session_x\n"
        "🖥️ On the computer: ```tmux attach -t rc-c-whey```\n"
        "🚪 To leave, close the terminal window or tab; /exit ends the session"
    )


def test_close_defaults_to_the_active_project(make_dispatcher: MakeDispatcher) -> None:
    """/close with no argument closes the active project's session."""
    dispatcher, bus, _ = make_dispatcher()
    _run(dispatcher, bus, "project", "c/whey")
    assert _run(dispatcher, bus, "close") == "⚫ Closed rc-c-whey."
    assert _run(dispatcher, bus, "close") == "⚪ rc-c-whey was not running."


def test_close_by_name_and_unknown(make_dispatcher: MakeDispatcher) -> None:
    """/close accepts a project name, and rejects one it does not know."""
    dispatcher, bus, _ = make_dispatcher()
    _run(dispatcher, bus, "project", "chaufon")
    _run(dispatcher, bus, "project", "main")
    assert _run(dispatcher, bus, "close", "chaufon") == "⚫ Closed rc-chaufon."
    assert _run(dispatcher, bus, "close", "nope").startswith("❌ Unknown project 'nope'")


def test_status_without_sessions(make_dispatcher: MakeDispatcher) -> None:
    """/status lists its fields as bullets, and "none" under Remote Control."""
    dispatcher, bus, _ = make_dispatcher()
    assert _run(dispatcher, bus, "status") == (
        "📊 *Status*\n"
        "• Claude Code: 🟢 enabled\n"
        "• Voice replies: 🔇 off\n"
        "• Project: main (idle)\n"
        "• Session: none yet\n"
        "\n"
        "📱 *Remote Control*\n"
        "◦ none"
    )


def test_status_shows_every_session_state(root: Path, make_dispatcher: MakeDispatcher) -> None:
    """/status marks connected, unconnected, exited and outside sessions, and drops the exited one."""
    dispatcher, bus, live = make_dispatcher()
    _run(dispatcher, bus, "project", "c/whey")
    _run(dispatcher, bus, "project", "chaufon")
    live.register("rc-chaufon", busy=True, connected=False)
    live.panes["rc-gone"] = "dead"
    live.outside.append(
        ProcessInfo(pid=2, session_id="t", cwd=root / "main", busy=False, bridge_session_id="s", tmux_session=None)
    )
    assert _run(dispatcher, bus, "status") == (
        "📊 *Status*\n"
        "• Claude Code: 🟢 enabled\n"
        "• Voice replies: 🔇 off\n"
        "• Project: chaufon (busy)\n"
        "• Session: id-rc-chaufon\n"
        "• On the computer: ```tmux attach -t rc-chaufon```\n"
        "\n"
        "📱 *Remote Control*\n"
        "◦ 🟢 rc-c-whey: connected, idle\n"
        "◦ 🟡 rc-chaufon: not connected to the app · /reconnect chaufon\n"
        "◦ 🔴 rc-gone: exited (Connection lost)\n"
        "◦ ⚪ main: started outside WhatsApp"
    )
    assert "rc-gone" not in live.panes


def test_reconnect(make_dispatcher: MakeDispatcher) -> None:
    """/reconnect types /remote-control into an unconnected session, and leaves a connected one alone."""
    dispatcher, bus, live = make_dispatcher()
    assert _run(dispatcher, bus, "reconnect") == "❌ rc-main is not running. Send /project main to start it."
    _run(dispatcher, bus, "project", "main")
    assert _run(dispatcher, bus, "reconnect") == "📱 rc-main is already connected: https://claude.ai/code/session_x"
    live.register("rc-main", connected=False)

    original = live.send_text

    async def send_and_connect(session: str, text: str) -> bool:
        live.register(session)
        return await original(session, text)

    live.send_text = send_and_connect  # type: ignore[method-assign]
    assert _run(dispatcher, bus, "reconnect") == "📱 Reconnected rc-main: https://claude.ai/code/session_x"
    assert live.typed == [("rc-main", "/remote-control")]


def test_last(make_dispatcher: MakeDispatcher) -> None:
    """/last sends the full last answer without its audio summary, or says there is none."""
    dispatcher, bus, _ = make_dispatcher(state=PersistedState(last_answers={"main": "All **done**.\n## 🔊 Audio Summary\nok"}))
    assert _run(dispatcher, bus, "last") == "💬 *Last answer:*\nAll *done*."
    _run(dispatcher, bus, "project", "chaufon")
    assert _run(dispatcher, bus, "last") == "💬 No answer yet in 'chaufon'."


def test_help_is_grouped() -> None:
    """/help groups the commands under section headings, one bullet each."""
    from claude_orchestrator.commands import HELP_TEXT

    for heading in ("*Projects*", "*Session*", "*Settings*", "*Pass-through*"):
        assert heading in HELP_TEXT
    assert "/new" not in HELP_TEXT
    assert "• /last" in HELP_TEXT and "• /reconnect" in HELP_TEXT


def _empty_dispatcher(
    root: Path, monkeypatch: pytest.MonkeyPatch, state: PersistedState | None = None
) -> tuple[Dispatcher, FakeBus]:
    """A Dispatcher with no static projects over a project_root with no folders."""
    from claude_orchestrator import dispatcher as dispatcher_module
    from claude_orchestrator.config import Config, WhatsAppConfig

    empty = root / "empty-root"
    empty.mkdir()
    persisted = state or PersistedState()
    monkeypatch.setattr(dispatcher_module, "load_state", lambda: persisted)
    monkeypatch.setattr(dispatcher_module, "save_state", lambda state: None)
    config = Config(whatsapp=WhatsAppConfig(allowed_sender_jid="me"), project_root=empty)
    bus = FakeBus()
    return Dispatcher(config, bus, None, None, FakeLive(root)), bus  # type: ignore[arg-type]


def _incoming(type_: str = "text", **fields: object) -> IncomingMessage:
    return IncomingMessage(
        message_id="m2", timestamp="t", sender_jid="me", type=type_, **fields  # type: ignore[arg-type]
    )


def test_message_without_active_project_gets_no_project_reply(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dispatcher, bus = _empty_dispatcher(root, monkeypatch)
    asyncio.run(dispatcher._handle(_incoming(text="hello")))
    assert bus.texts == [NO_PROJECT_REPLY]


def test_voice_without_active_project_is_not_transcribed(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dispatcher, bus = _empty_dispatcher(root, monkeypatch)
    asyncio.run(dispatcher._handle(_incoming("voice", media_path="/tmp/x.ogg")))
    assert bus.texts == [NO_PROJECT_REPLY]


def test_attachment_without_active_project_gets_no_project_reply(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dispatcher, bus = _empty_dispatcher(root, monkeypatch)
    asyncio.run(dispatcher._handle(_incoming("attachment", media_path="/tmp/x.png")))
    assert bus.texts == [NO_PROJECT_REPLY]


def test_stale_persisted_project_without_fallback_gets_no_project_reply(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dispatcher, bus = _empty_dispatcher(root, monkeypatch, PersistedState(active_project="gone"))
    asyncio.run(dispatcher._handle(_incoming(text="hello")))
    assert bus.texts == [NO_PROJECT_REPLY]


@pytest.mark.parametrize("name", ["stop", "last", "close", "reconnect"])
def test_active_project_commands_without_project_reply_no_project(
    root: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    dispatcher, bus = _empty_dispatcher(root, monkeypatch)
    assert _run_all(dispatcher, bus, name) == [NO_PROJECT_REPLY]


def test_c_command_without_project_replies_no_project(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dispatcher, bus = _empty_dispatcher(root, monkeypatch)
    assert _run_all(dispatcher, bus, "c", "/usage") == [NO_PROJECT_REPLY]


def test_status_and_projects_work_without_project(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dispatcher, bus = _empty_dispatcher(root, monkeypatch)
    assert "• Project: none" in _run(dispatcher, bus, "status")
    assert "Active: none" in _run(dispatcher, bus, "projects")


def test_create_works_without_project(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dispatcher, bus = _empty_dispatcher(root, monkeypatch)
    assert "Created and switched to project 'first'" in _run(dispatcher, bus, "create", "first")
    assert dispatcher._active_project == "first"
