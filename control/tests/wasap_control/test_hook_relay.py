import io
import json
import socket
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from wasap_control.hook_relay import PENDING_MAX_AGE_SECONDS, build, main
from wasap_control.live_session import pending_path
from wasap_control.voice_summary import VOICE_SUMMARY_SYSTEM_PROMPT

if TYPE_CHECKING:
    from _pytest.capture import CaptureFixture
    from _pytest.monkeypatch import MonkeyPatch

PROMPT = {
    "hook_event_name": "UserPromptSubmit",
    "session_id": "s1",
    "prompt_id": "p1",
    "prompt": "hello",
    "transcript_path": "/x.jsonl",
}


def _mark(live_dir: Path, voice: bool, at: float) -> Path:
    path = pending_path(live_dir, "rc-app")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"voice": voice, "at": at}))
    return path


def test_a_prompt_without_a_marker_is_forwarded_untagged(tmp_path: Path) -> None:
    """A prompt typed on the phone or the computer carries no origin and gets no reply."""
    message, reply = build(PROMPT, "rc-app", tmp_path, now=100)
    assert message == {
        "hook_event_name": "UserPromptSubmit",
        "session_id": "s1",
        "prompt_id": "p1",
        "live_session": "rc-app",
    }
    assert reply is None


def test_a_fresh_marker_tags_the_prompt_and_is_consumed(tmp_path: Path) -> None:
    """The prompt after a WhatsApp marker is tagged as WhatsApp's, once."""
    path = _mark(tmp_path, voice=False, at=100)
    message, reply = build(PROMPT, "rc-app", tmp_path, now=101)
    assert message["origin"] == "whatsapp"
    assert reply is None
    assert not path.exists()
    assert "origin" not in build(PROMPT, "rc-app", tmp_path, now=102)[0]


def test_voice_adds_the_audio_summary_instruction(tmp_path: Path) -> None:
    """With voice replies on, the prompt gets the audio-summary instruction as context."""
    _mark(tmp_path, voice=True, at=100)
    _, reply = build(PROMPT, "rc-app", tmp_path, now=101)
    assert reply == {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": VOICE_SUMMARY_SYSTEM_PROMPT,
        }
    }


def test_a_stale_marker_is_ignored(tmp_path: Path) -> None:
    """A marker older than the limit belongs to a message that never became a prompt."""
    _mark(tmp_path, voice=True, at=100)
    message, reply = build(PROMPT, "rc-app", tmp_path, now=101 + PENDING_MAX_AGE_SECONDS)
    assert "origin" not in message
    assert reply is None


def test_other_events_keep_only_the_needed_fields(tmp_path: Path) -> None:
    """Tool inputs and outputs are dropped; a Stop keeps its answer; markers are left alone."""
    path = _mark(tmp_path, voice=False, at=100)
    tool = {"hook_event_name": "PostToolUse", "prompt_id": "p1", "tool_name": "Bash", "tool_input": {"x": 1}}
    assert build(tool, "rc-app", tmp_path, now=100)[0] == {
        "hook_event_name": "PostToolUse",
        "prompt_id": "p1",
        "tool_name": "Bash",
        "live_session": "rc-app",
    }
    stop = {"hook_event_name": "Stop", "prompt_id": "p1", "last_assistant_message": "done"}
    assert build(stop, "rc-app", tmp_path, now=100)[0]["last_assistant_message"] == "done"
    assert path.exists()


def _serve_once(path: Path) -> tuple[threading.Thread, list[bytes]]:
    """A fake orchestrator that accepts one connection and records what it reads."""
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(path))
    server.listen(1)
    server.settimeout(5)
    received: list[bytes] = []

    def accept() -> None:
        try:
            conn, _ = server.accept()
        except TimeoutError:
            server.close()
            return
        with conn:
            while chunk := conn.recv(65536):
                received.append(chunk)
        server.close()

    thread = threading.Thread(target=accept)
    thread.start()
    return thread, received


def test_main_writes_one_hook_frame_to_the_socket(tmp_path: Path, monkeypatch: "MonkeyPatch") -> None:
    """The relay sends the event as a single `hook` frame line and disconnects."""
    path = tmp_path / "bus.sock"
    thread, received = _serve_once(path)
    stop = {"hook_event_name": "Stop", "prompt_id": "p1", "last_assistant_message": "done"}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(stop)))

    main(["--session", "rc-app", "--socket", str(path), "--live-dir", str(tmp_path)])
    thread.join(timeout=5)

    data = b"".join(received)
    assert data.endswith(b"\n") and data.count(b"\n") == 1
    assert json.loads(data) == {
        "kind": "hook",
        "data": {**stop, "live_session": "rc-app"},
    }


def test_main_exits_0_when_nobody_listens(
    tmp_path: Path, monkeypatch: "MonkeyPatch", capsys: "CaptureFixture[str]"
) -> None:
    """A missing orchestrator is logged to stderr and never fails the session."""
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"hook_event_name": "Stop"})))
    main(["--session", "rc-app", "--socket", str(tmp_path / "absent.sock"), "--live-dir", str(tmp_path)])
    assert "wasap-control hook relay" in capsys.readouterr().err


def test_main_exits_0_on_bad_arguments(monkeypatch: "MonkeyPatch", capsys: "CaptureFixture[str]") -> None:
    """Unusable arguments are logged to stderr; the hook still exits 0, since a
    failing UserPromptSubmit hook would block the prompt."""
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    main(["--session", "rc-app", "--unknown", "x"])
    assert "wasap-control hook relay" in capsys.readouterr().err
