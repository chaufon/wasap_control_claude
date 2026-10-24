"""Claude Code hook command for the live sessions (live_session.py): reads
one hook event from stdin and writes it to the orchestrator's socket as one
`hook` frame (socket_bus.py), tagged with the tmux session it came from
(SPEC.md §7.7).

On UserPromptSubmit it also consumes the session's pending marker: the
orchestrator writes one right before typing a WhatsApp message, so the
prompt that follows is tagged `"origin": "whatsapp"`, and, with voice
replies on, gets the audio-summary instruction as extra context.

A hook must never break the session, so every failure, bad arguments
included, is logged to stderr and the command still exits 0.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from pathlib import Path

from .live_session import LIVE_DIR, pending_path
from .voice_summary import VOICE_SUMMARY_SYSTEM_PROMPT

# Seconds to connect to the orchestrator and write the frame.
SOCKET_TIMEOUT_SECONDS = 2

# A marker older than this belongs to a message that never became a prompt.
PENDING_MAX_AGE_SECONDS = 30

# Only these fields reach the orchestrator; tool inputs and outputs can be
# megabytes and are never needed.
_FORWARDED_FIELDS = ("hook_event_name", "session_id", "prompt_id", "source", "tool_name", "last_assistant_message")


def take_pending(path: Path, now: float) -> dict | None:
    """Consume the pending marker, or None if there is no fresh one."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        path.unlink()
    except (OSError, ValueError):
        return None
    if now - float(data.get("at", 0)) > PENDING_MAX_AGE_SECONDS:
        return None
    return data


def build(event: dict, session: str, live_dir: Path, now: float) -> tuple[dict, dict | None]:
    """The message to send, and the hook's stdout reply (if any)."""
    message = {key: event[key] for key in _FORWARDED_FIELDS if key in event}
    message["live_session"] = session
    reply = None
    if event.get("hook_event_name") == "UserPromptSubmit":
        pending = take_pending(pending_path(live_dir, session), now)
        if pending is not None:
            message["origin"] = "whatsapp"
            if pending.get("voice"):
                reply = {
                    "hookSpecificOutput": {
                        "hookEventName": "UserPromptSubmit",
                        "additionalContext": VOICE_SUMMARY_SYSTEM_PROMPT,
                    }
                }
    return message, reply


def send(path: Path, message: dict) -> None:
    """Write `message` to the orchestrator's socket as one `hook` frame."""
    frame = json.dumps({"kind": "hook", "data": message}).encode() + b"\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(SOCKET_TIMEOUT_SECONDS)
        client.connect(str(path))
        client.sendall(frame)


class _ArgumentError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        raise _ArgumentError(message)


def main(argv: list[str] | None = None) -> None:
    parser = _Parser(description=__doc__)
    parser.add_argument("--session", required=True)
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--live-dir", type=Path, default=LIVE_DIR)
    try:
        args = parser.parse_args(argv)
        event = json.load(sys.stdin)
        message, reply = build(event, args.session, args.live_dir, time.time())
        if reply is not None:
            print(json.dumps(reply))
        send(args.socket, message)
    except Exception as exc:  # noqa: BLE001 - a hook must never fail the session
        print(f"claude-orchestrator hook relay: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
