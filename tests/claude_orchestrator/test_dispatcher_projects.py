import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from claude_orchestrator import dispatcher as dispatcher_module
from claude_orchestrator.commands import Command
from claude_orchestrator.config import Config, ProjectConfig, WhatsAppConfig
from claude_orchestrator.dispatcher import Dispatcher
from claude_orchestrator.models import IncomingMessage, OutgoingMessage
from claude_orchestrator.state import PersistedState

if TYPE_CHECKING:
    from _pytest.logging import LogCaptureFixture
    from _pytest.monkeypatch import MonkeyPatch

MESSAGE = IncomingMessage(message_id="m1", timestamp="t", sender_jid="me", type="text", text="")


class FakeBus:
    def __init__(self) -> None:
        self.sent: list[OutgoingMessage] = []

    async def publish_outgoing(self, message: OutgoingMessage) -> None:
        self.sent.append(message)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    for name in ("main", "chaufon", "c/fitnotes", "c/whey", "c/car/crash26", "c/car/maintenance"):
        (tmp_path / name).mkdir(parents=True)
    return tmp_path


def _dispatcher(
    root: Path, monkeypatch: "MonkeyPatch", containers: tuple[str, ...] = ("c", "c/car")
) -> tuple[Dispatcher, FakeBus]:
    monkeypatch.setattr(dispatcher_module, "load_state", lambda: PersistedState())
    monkeypatch.setattr(dispatcher_module, "save_state", lambda state: None)
    config = Config(
        whatsapp=WhatsAppConfig(allowed_sender_jid="me"),
        projects=[ProjectConfig(name="main", path=root / "main")],
        project_root=root,
        project_containers=containers,
    )
    bus = FakeBus()
    return Dispatcher(config, bus, None, None, None), bus  # type: ignore[arg-type]


def _run(dispatcher: Dispatcher, bus: FakeBus, name: str, arg: str | None = None) -> str:
    bus.sent.clear()
    asyncio.run(dispatcher._handle_command(Command(name=name, arg=arg), MESSAGE))
    return bus.sent[-1].text or ""


def test_projects_lists_containers_with_children(root: Path, monkeypatch: "MonkeyPatch") -> None:
    """/projects shows containers as headers and their children numbered beneath them."""
    dispatcher, bus = _dispatcher(root, monkeypatch)
    assert _run(dispatcher, bus, "projects") == (
        "Available projects:\n"
        "📁 c\n"
        "  📁 c/car\n"
        "    1. c/car/crash26\n"
        "    2. c/car/maintenance\n"
        "  3. c/fitnotes\n"
        "  4. c/whey\n"
        "5. chaufon\n"
        "6. main\n\n"
        "Active: main"
    )


def test_project_switches_to_nested_project_by_name_and_number(
    root: Path, monkeypatch: "MonkeyPatch"
) -> None:
    """A nested project can be selected by its relative path or its list number."""
    dispatcher, bus = _dispatcher(root, monkeypatch)
    assert _run(dispatcher, bus, "project", "c/car/maintenance") == (
        "✅ Switched to project 'c/car/maintenance'."
    )
    assert _run(dispatcher, bus, "project", "4") == "✅ Switched to project 'c/whey'."


def test_project_on_container_lists_its_children(root: Path, monkeypatch: "MonkeyPatch") -> None:
    """Selecting a container replies with its subtree instead of switching."""
    dispatcher, bus = _dispatcher(root, monkeypatch)
    assert _run(dispatcher, bus, "project", "c/car") == (
        "📁 c/car is a folder of projects. Pick one:\n1. c/car/crash26\n2. c/car/maintenance"
    )
    assert dispatcher._active_project == "main"


def test_create_inside_container(root: Path, monkeypatch: "MonkeyPatch") -> None:
    """/create <container>/<name> makes the folder and switches to it."""
    dispatcher, bus = _dispatcher(root, monkeypatch)
    reply = _run(dispatcher, bus, "create", "c/car/newapp")
    assert reply.startswith("✅ Created and switched to project 'c/car/newapp'")
    assert (root / "c" / "car" / "newapp").is_dir()


def test_create_rejects_parent_that_is_not_a_container(
    root: Path, monkeypatch: "MonkeyPatch"
) -> None:
    """/create refuses a parent folder that is not a listed container."""
    dispatcher, bus = _dispatcher(root, monkeypatch)
    reply = _run(dispatcher, bus, "create", "chaufon/sub")
    assert reply == "❌ 'chaufon' is not a project container. Containers: c, c/car."
    assert not (root / "chaufon" / "sub").exists()


@pytest.mark.parametrize("arg", ["../escape", "c/../x", "c/.hidden", "c/"])
def test_create_rejects_unsafe_names(root: Path, monkeypatch: "MonkeyPatch", arg: str) -> None:
    """/create refuses traversal, hidden and empty folder names."""
    dispatcher, bus = _dispatcher(root, monkeypatch)
    assert _run(dispatcher, bus, "create", arg).startswith("❌")


def test_missing_container_logs_a_warning(
    root: Path, monkeypatch: "MonkeyPatch", caplog: "LogCaptureFixture"
) -> None:
    """A configured container with no folder is skipped with a warning at startup."""
    with caplog.at_level(logging.WARNING):
        dispatcher, bus = _dispatcher(root, monkeypatch, containers=("c", "gone"))
    assert "project container 'gone' not found" in caplog.text
    assert "gone" not in _run(dispatcher, bus, "projects")
