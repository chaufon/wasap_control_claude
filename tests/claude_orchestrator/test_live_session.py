import asyncio
import json
import shlex
import shutil
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from claude_orchestrator.config import RemoteControlConfig
from claude_orchestrator.live_session import (
    HOOK_EVENTS,
    LiveSessions,
    ensure_trusted,
    hook_settings,
    read_processes,
    screen_text,
    session_name,
)

URL = "https://claude.ai/code/session_x"
RELAY = ["/venv/python", "-m", "claude_orchestrator.hook_relay"]


def test_session_name_replaces_separators() -> None:
    """Slashes, dots and colons become dashes so tmux targets stay unambiguous."""
    assert session_name("c/car/maintenance") == "rc-c-car-maintenance"
    assert session_name("my.app:v2") == "rc-my-app-v2"


def test_ensure_trusted_creates_the_entry(tmp_path: Path) -> None:
    """A folder with no entry gets one, and other keys are kept."""
    claude_json = tmp_path / ".claude.json"
    claude_json.write_text(json.dumps({"userID": "u", "projects": {"/other": {"x": 1}}}))
    ensure_trusted(tmp_path, claude_json)
    data = json.loads(claude_json.read_text())
    assert data["userID"] == "u"
    assert data["projects"]["/other"] == {"x": 1}
    assert data["projects"][str(tmp_path.resolve())] == {"hasTrustDialogAccepted": True}


def test_ensure_trusted_flips_false_and_keeps_entry_keys(tmp_path: Path) -> None:
    """An entry with the flag false is set to true; its other keys stay."""
    claude_json = tmp_path / ".claude.json"
    key = str(tmp_path.resolve())
    claude_json.write_text(json.dumps({"projects": {key: {"hasTrustDialogAccepted": False, "lastCost": 1}}}))
    ensure_trusted(tmp_path, claude_json)
    assert json.loads(claude_json.read_text())["projects"][key] == {"hasTrustDialogAccepted": True, "lastCost": 1}


def test_ensure_trusted_leaves_a_trusted_folder_alone(tmp_path: Path) -> None:
    """An already-trusted folder causes no write."""
    claude_json = tmp_path / ".claude.json"
    original = json.dumps({"projects": {str(tmp_path.resolve()): {"hasTrustDialogAccepted": True}}})
    claude_json.write_text(original)
    ensure_trusted(tmp_path, claude_json)
    assert claude_json.read_text() == original


def test_hook_settings_sends_every_event_to_the_relay() -> None:
    """Each forwarded event runs the relay, tagged with the tmux session."""
    hooks = json.loads(hook_settings(RELAY, "rc-app", Path("/live")))["hooks"]
    assert sorted(hooks) == sorted(HOOK_EVENTS)
    command = hooks["Stop"][0]["hooks"][0]["command"]
    assert shlex.split(command) == [*RELAY, "--session", "rc-app", "--live-dir", "/live"]


def test_read_processes_skips_exited_and_broken_files(tmp_path: Path) -> None:
    """Only parseable files of running processes are returned."""
    import os

    (tmp_path / "1.json").write_text("{not json")
    (tmp_path / "2.json").write_text(json.dumps({"pid": 999_999_999, "sessionId": "gone"}))
    (tmp_path / "3.json").write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "sessionId": "s1",
                "cwd": "/p",
                "status": "busy",
                "tmux": "rc-app:@3.%3",
                "bridgeSessionId": "session_y",
            }
        )
    )
    [info] = read_processes(tmp_path)
    assert (info.session_id, info.cwd, info.busy, info.tmux_session) == ("s1", Path("/p"), True, "rc-app")
    assert info.url == "https://claude.ai/code/session_y"


USAGE_PANEL = """\
  /remote-control is active · Continue here, on your phone
▔▔▔▔▔▔▔▔▔▔ tmux detected · scroll with PgUp/PgDn ▔
   Settings  Status   Config   Usage   Stats
   Session
   Total cost:            $0.1952
   Current session
   ██                                                 4% used                ↓
"""

INLINE = """\
❯ hello
● hi
❯ /cost
  ⎿  Total cost: $0.20
     Total duration: 1m

────────────────────
❯
────────────────────
  ⏵⏵ bypass permissions on
"""


def test_screen_text_of_a_panel_is_the_whole_panel() -> None:
    """A full-screen panel is everything below its top border, without the scroll marker."""
    assert screen_text(USAGE_PANEL, "/usage") == (
        "   Settings  Status   Config   Usage   Stats\n"
        "   Session\n"
        "   Total cost:            $0.1952\n"
        "   Current session\n"
        "   ██                                                 4% used"
    )


def test_screen_text_of_an_inline_command_stops_at_the_input_box() -> None:
    """Inline output is what follows the command's echo, up to the input box."""
    assert screen_text(INLINE, "/cost") == "  ⎿  Total cost: $0.20\n     Total duration: 1m"


needs_tmux = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")


@pytest.fixture
def socket() -> Iterator[str]:
    """A private tmux server, so tests never touch the user's sessions."""
    name = f"rc-test-{uuid.uuid4().hex[:8]}"
    yield name
    subprocess.run(["tmux", "-L", name, "kill-server"], capture_output=True)


def _fake_claude(tmp_path: Path, body: str) -> str:
    script = tmp_path / "fake-claude"
    script.write_text(f"#!/bin/sh\n{body}\n")
    script.chmod(0o755)
    return str(script)


def _register(sessions_dir: Path) -> str:
    """Shell that registers the fake like Claude Code does, under the name
    given to --remote-control."""
    return (
        'name=""; prev=""; for a in "$@"; do [ "$prev" = "--remote-control" ] && name="$a"; prev="$a"; done\n'
        f"printf '{{\"pid\": %s, \"sessionId\": \"s1\", \"cwd\": \"%s\", \"status\": \"idle\", "
        f"\"tmux\": \"%s:@0.%%0\", \"bridgeSessionId\": \"session_x\"}}' $$ \"$PWD\" \"$name\" "
        f'> "{sessions_dir}/$$.json"'
    )


def _live(tmp_path: Path, socket: str, binary: str, timeout: float = 5) -> LiveSessions:
    (tmp_path / "sessions").mkdir(exist_ok=True)
    return LiveSessions(
        RemoteControlConfig(),
        binary,
        RELAY,
        claude_json=tmp_path / ".claude.json",
        sessions_dir=tmp_path / "sessions",
        live_dir=tmp_path / "live",
        shell="/bin/sh",
        tmux_socket=socket,
        use_scope=False,
        ready_timeout_seconds=timeout,
        poll_interval_seconds=0.1,
        paste_settle_seconds=0.1,
    )


@needs_tmux
def test_launch_type_list_and_close(tmp_path: Path, socket: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """A session comes up once, takes typed text, is listed while live, and is gone after close."""
    sessions = tmp_path / "sessions"
    binary = _fake_claude(
        tmp_path,
        'for a in "$@"; do echo "arg: $a" | cut -c1-60; done; echo "ld=[$LD_LIBRARY_PATH]"\n'
        + _register(sessions)
        + '\nwhile read line; do echo "got: $line"; done',
    )
    monkeypatch.setenv("LD_LIBRARY_PATH", "/service/only")
    live = _live(tmp_path, socket, binary)

    async def scenario() -> None:
        first = await live.launch("c/whey", tmp_path)
        assert (first.status, first.session, first.url) == ("started", "rc-c-whey", URL)
        pane = await live.capture("rc-c-whey", history=True)
        for expected in (
            "arg: --remote-control\n",
            "arg: rc-c-whey\n",
            "arg: bypassPermissions\n",
            "arg: --no-chrome\n",
            "arg: --settings\n",
            "arg: --append-system-prompt\n",
            "ld=[]",
        ):
            assert expected in pane
        assert "--resume" not in pane
        second = await live.launch("c/whey", tmp_path)
        assert (second.status, second.url) == ("running", URL)
        assert await live.sessions() == {"rc-c-whey": "live"}
        assert await live.send_text("rc-c-whey", "hello there") is True
        assert await live.send_text("rc-c-whey", "line one\nline two") is True
        await asyncio.sleep(0.3)
        screen = await live.capture("rc-c-whey")
        assert "got: hello there" in screen
        assert "got: line one" in screen and "got: line two" in screen
        assert await live.close("c/whey") is True
        assert await live.close("c/whey") is False
        assert await live.sessions() == {}

    asyncio.run(scenario())
    trusted = json.loads((tmp_path / ".claude.json").read_text())["projects"][str(tmp_path.resolve())]
    assert trusted == {"hasTrustDialogAccepted": True}


@needs_tmux
def test_launch_resumes_the_given_session(tmp_path: Path, socket: str) -> None:
    """A stored session ID is passed to --resume."""
    binary = _fake_claude(tmp_path, 'echo "$*" | grep -o -- "--resume [a-z0-9-]*"\n' + _register(tmp_path / "sessions") + "\nsleep 60")
    live = _live(tmp_path, socket, binary)

    async def scenario() -> None:
        assert (await live.launch("app", tmp_path, "abc-123")).status == "started"
        assert "--resume abc-123" in await live.capture("rc-app", history=True)

    asyncio.run(scenario())


@needs_tmux
def test_launch_reports_a_session_that_exits(tmp_path: Path, socket: str) -> None:
    """A session that dies before registering is reported with its output, and cleaned up."""
    binary = _fake_claude(tmp_path, 'echo "Error: not logged in"; exit 1')
    live = _live(tmp_path, socket, binary)

    async def scenario() -> None:
        result = await live.launch("app", tmp_path)
        assert result.status == "failed"
        assert "Error: not logged in" in result.detail
        assert await live.sessions() == {}

    asyncio.run(scenario())


@needs_tmux
def test_launch_reports_an_unconnected_session_as_pending(tmp_path: Path, socket: str) -> None:
    """A session still not connected to the app after the wait is left running and reported pending."""
    binary = _fake_claude(tmp_path, 'echo "Connecting..."; sleep 60')
    live = _live(tmp_path, socket, binary, timeout=0.5)

    async def scenario() -> None:
        result = await live.launch("app", tmp_path)
        assert (result.status, result.detail) == ("pending", "Connecting...")
        assert await live.sessions() == {"rc-app": "live"}

    asyncio.run(scenario())


@needs_tmux
def test_a_dead_session_is_listed_with_its_last_line(tmp_path: Path, socket: str) -> None:
    """After the process exits, the kept pane shows as dead with its last output."""
    binary = _fake_claude(tmp_path, _register(tmp_path / "sessions") + '\nsleep 1; echo "Connection lost"; exit 1')
    live = _live(tmp_path, socket, binary)

    async def scenario() -> None:
        assert (await live.launch("app", tmp_path)).status == "started"
        await asyncio.sleep(1.5)
        assert await live.sessions() == {"rc-app": "dead"}
        assert await live.last_line("rc-app") == "Connection lost"

    asyncio.run(scenario())
