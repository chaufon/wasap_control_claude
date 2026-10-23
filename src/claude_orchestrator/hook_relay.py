"""Claude Code hook command for the live sessions (live_session.py): reads
one hook event from stdin and publishes it to the orchestrator's Redis hooks
channel, tagged with the tmux session it came from (SPEC.md §7.7).

On UserPromptSubmit it also consumes the session's pending marker: the
orchestrator writes one right before typing a WhatsApp message, so the
prompt that follows is tagged `"origin": "whatsapp"`, and, with voice
replies on, gets the audio-summary instruction as extra context.

A hook must never break the session, so every failure is logged to stderr
and the command still exits 0.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import redis

from .live_session import LIVE_DIR, pending_path
from .voice_summary import VOICE_SUMMARY_SYSTEM_PROMPT

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
    """The message to publish, and the hook's stdout reply (if any)."""
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


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", required=True)
    parser.add_argument("--redis-host", default="127.0.0.1")
    parser.add_argument("--redis-port", type=int, default=6379)
    parser.add_argument("--channel", required=True)
    parser.add_argument("--live-dir", type=Path, default=LIVE_DIR)
    args = parser.parse_args(argv)
    try:
        event = json.load(sys.stdin)
        message, reply = build(event, args.session, args.live_dir, time.time())
        if reply is not None:
            print(json.dumps(reply))
        client = redis.Redis(host=args.redis_host, port=args.redis_port, socket_timeout=2)
        client.publish(args.channel, json.dumps(message))
    except Exception as exc:  # noqa: BLE001 - a hook must never fail the session
        print(f"claude-orchestrator hook relay: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
