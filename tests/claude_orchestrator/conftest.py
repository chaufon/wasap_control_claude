"""Fakes and a dispatcher factory shared by the dispatcher tests."""

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from claude_orchestrator import dispatcher as dispatcher_module
from claude_orchestrator.config import Config, ProgressUpdatesConfig, ProjectConfig, WhatsAppConfig
from claude_orchestrator.dispatcher import Dispatcher
from claude_orchestrator.live_session import LaunchResult, PaneState, ProcessInfo, session_name
from claude_orchestrator.models import OutgoingMessage
from claude_orchestrator.state import PersistedState

if TYPE_CHECKING:
    from _pytest.monkeypatch import MonkeyPatch


class FakeBus:
    def __init__(self) -> None:
        self.sent: list[OutgoingMessage] = []

    async def publish_outgoing(self, message: OutgoingMessage) -> None:
        self.sent.append(message)

    @property
    def texts(self) -> list[str]:
        return [m.text for m in self.sent if m.text is not None]


class FakeLive:
    """In-memory LiveSessions: a launched session is live, registered, idle
    and connected to the app until a test changes it."""

    poll_interval = 0.01

    def __init__(self, root: Path) -> None:
        self.root = root
        self.panes: dict[str, PaneState] = {}
        self.infos: dict[str, ProcessInfo] = {}
        self.outside: list[ProcessInfo] = []
        self.typed: list[tuple[str, str]] = []
        self.keys: list[tuple[str, str]] = []
        self.pending: dict[str, bool] = {}
        self.launches: list[tuple[str, str | None]] = []
        self.screen = ""
        self.launch_status = "started"

    def register(self, session: str, *, busy: bool = False, connected: bool = True, cwd: Path | None = None) -> None:
        self.panes[session] = "live"
        self.infos[session] = ProcessInfo(
            pid=1,
            session_id=f"id-{session}",
            cwd=cwd or self.root,
            busy=busy,
            bridge_session_id="session_x" if connected else None,
            tmux_session=session,
        )

    async def launch(self, project_name: str, path: Path, resume_session_id: str | None = None) -> LaunchResult:
        session = session_name(project_name)
        if self.panes.get(session) == "live":
            return LaunchResult("running", session, url=self.infos[session].url)
        self.launches.append((project_name, resume_session_id))
        if self.launch_status == "failed":
            return LaunchResult("failed", session, detail="boom")
        self.register(session, cwd=path)
        return LaunchResult("started", session, url="https://claude.ai/code/session_x")

    async def pane_state(self, session: str) -> PaneState:
        return self.panes.get(session, "absent")

    def process(self, session: str) -> ProcessInfo | None:
        return self.infos.get(session) if self.panes.get(session) == "live" else None

    def all_processes(self) -> list[ProcessInfo]:
        return [*self.infos.values(), *self.outside]

    async def sessions(self) -> dict[str, PaneState]:
        return dict(sorted(self.panes.items()))

    async def wait_registered(self, session: str, timeout_seconds: float) -> ProcessInfo | None:
        return self.process(session)

    def mark_pending(self, session: str, voice: bool) -> None:
        self.pending[session] = voice

    def clear_pending(self, session: str) -> None:
        self.pending.pop(session, None)

    async def send_text(self, session: str, text: str) -> bool:
        self.typed.append((session, text))
        return True

    async def send_key(self, session: str, key: str) -> bool:
        self.keys.append((session, key))
        return True

    async def capture(self, session: str, history: bool = False) -> str:
        return self.screen

    async def last_line(self, session: str) -> str:
        return "Connection lost"

    async def close(self, project_name: str) -> bool:
        return await self.kill(session_name(project_name))

    async def kill(self, session: str) -> bool:
        self.infos.pop(session, None)
        return self.panes.pop(session, None) is not None


@pytest.fixture
def root(tmp_path: Path) -> Path:
    for name in ("main", "chaufon", "c/fitnotes", "c/whey", "c/car/crash26", "c/car/maintenance"):
        (tmp_path / name).mkdir(parents=True)
    return tmp_path


type MakeDispatcher = Callable[..., tuple[Dispatcher, FakeBus, FakeLive]]


@pytest.fixture
def make_dispatcher(root: Path, monkeypatch: "MonkeyPatch") -> MakeDispatcher:
    """A Dispatcher over `root`, with in-memory state, bus and live sessions."""

    def make(
        containers: tuple[str, ...] = ("c", "c/car"), state: PersistedState | None = None
    ) -> tuple[Dispatcher, FakeBus, FakeLive]:
        persisted = state or PersistedState()
        monkeypatch.setattr(dispatcher_module, "load_state", lambda: persisted)
        monkeypatch.setattr(dispatcher_module, "save_state", lambda state: None)
        config = Config(
            whatsapp=WhatsAppConfig(allowed_sender_jid="me"),
            projects=[ProjectConfig(name="main", path=root / "main")],
            project_root=root,
            project_containers=containers,
            progress_updates=ProgressUpdatesConfig(interval_seconds=3600),
        )
        bus = FakeBus()
        live = FakeLive(root)
        return Dispatcher(config, bus, None, None, live), bus, live  # type: ignore[arg-type]

    return make
