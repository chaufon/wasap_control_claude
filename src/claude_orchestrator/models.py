"""Message models matching the Redis channel schemas (SPEC.md §6)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

IncomingType = Literal["text", "voice", "attachment"]


@dataclass(frozen=True)
class IncomingMessage:
    message_id: str
    timestamp: str
    sender_jid: str
    type: IncomingType
    text: str | None = None
    media_path: str | None = None
    media_mime_type: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "IncomingMessage":
        return cls(
            message_id=data["message_id"],
            timestamp=data["timestamp"],
            sender_jid=data["sender_jid"],
            type=data["type"],
            text=data.get("text"),
            media_path=data.get("media_path"),
            media_mime_type=data.get("media_mime_type"),
        )


@dataclass(frozen=True)
class OutgoingMessage:
    text: str
    in_reply_to: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "in_reply_to": self.in_reply_to}
