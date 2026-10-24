import json
from pathlib import Path

from claude_orchestrator.hook_relay import PENDING_MAX_AGE_SECONDS, build
from claude_orchestrator.live_session import pending_path
from claude_orchestrator.voice_summary import VOICE_SUMMARY_SYSTEM_PROMPT

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
