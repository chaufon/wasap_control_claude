import asyncio
import json
import shutil
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from claude_orchestrator.config import RemoteControlConfig
from claude_orchestrator.remote_control import RemoteControl, ensure_trusted, session_name

URL = "https://claude.ai/code?environment=env_test"


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


def _remote_control(tmp_path: Path, socket: str, binary: str, timeout: float = 5) -> RemoteControl:
    return RemoteControl(
        RemoteControlConfig(),
        binary,
        claude_json=tmp_path / ".claude.json",
        shell="/bin/sh",
        tmux_socket=socket,
        use_scope=False,
        ready_timeout_seconds=timeout,
        poll_interval_seconds=0.1,
    )


@needs_tmux
def test_launch_reuse_list_and_close(tmp_path: Path, socket: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """A server comes up once, is listed while live, and is gone after close."""
    binary = _fake_claude(
        tmp_path,
        f'echo "args: $*"; echo "ld=[$LD_LIBRARY_PATH]"; echo "Continue coding in the app or {URL}"; sleep 60',
    )
    (tmp_path / ".claude.json").write_text('{"claudeInChromeDefaultEnabled": true}')  # ignored
    monkeypatch.setenv("LD_LIBRARY_PATH", "/service/only")
    rc = _remote_control(tmp_path, socket, binary)

    async def scenario() -> None:
        first = await rc.launch("c/whey", tmp_path)
        assert (first.status, first.session, first.url) == ("started", "rc-c-whey", URL)
        pane = (await rc._capture("rc-c-whey")).replace("\n", "")
        assert "args: remote-control --name rc-c-whey --spawn same-dir --permission-mode bypassPermissions --no-chrome" in pane
        assert "ld=[]" in pane
        second = await rc.launch("c/whey", tmp_path)
        assert (second.status, second.url) == ("running", URL)
        assert await rc.running() == ["rc-c-whey"]
        assert await rc.close("c/whey") is True
        assert await rc.close("c/whey") is False
        assert await rc.running() == []

    asyncio.run(scenario())
    trusted = json.loads((tmp_path / ".claude.json").read_text())["projects"][str(tmp_path.resolve())]
    assert trusted == {"hasTrustDialogAccepted": True}


@needs_tmux
def test_launch_reports_a_server_that_exits(tmp_path: Path, socket: str) -> None:
    """A server that dies before printing its URL is reported with its output, and cleaned up."""
    binary = _fake_claude(tmp_path, 'echo "Error: not logged in"; exit 1')
    rc = _remote_control(tmp_path, socket, binary)

    async def scenario() -> None:
        result = await rc.launch("app", tmp_path)
        assert result.status == "failed"
        assert "Error: not logged in" in result.detail
        assert await rc.running() == []

    asyncio.run(scenario())


@needs_tmux
def test_launch_reports_a_slow_server_as_pending(tmp_path: Path, socket: str) -> None:
    """A server still running without a URL after the wait is left running and reported pending."""
    binary = _fake_claude(tmp_path, 'echo "Connecting..."; sleep 60')
    rc = _remote_control(tmp_path, socket, binary, timeout=0.5)

    async def scenario() -> None:
        result = await rc.launch("app", tmp_path)
        assert (result.status, result.detail) == ("pending", "Connecting...")
        assert await rc.running() == ["rc-app"]

    asyncio.run(scenario())
