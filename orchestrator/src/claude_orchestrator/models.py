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
    # WhatsApp's reply/quote feature (SPEC.md §5.9/§7.6) — populated
    # regardless of this message's own `type`, since you can reply to an
    # earlier message with text, voice, or an attachment interchangeably.
    # quoted_text is set verbatim when the quoted message was itself text;
    # quoted_media_path/mime_type are set when it was an attachment/voice
    # note the bridge successfully re-downloaded; quoted_media_type_label
    # (e.g. "image", "voice message") is set whenever quoting a
    # media message, whether or not the download itself succeeded — it's
    # the fallback description when quoted_media_path is absent.
    quoted_text: str | None = None
    quoted_media_path: str | None = None
    quoted_media_mime_type: str | None = None
    quoted_media_type_label: str | None = None

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
            quoted_text=data.get("quoted_text"),
            quoted_media_path=data.get("quoted_media_path"),
            quoted_media_mime_type=data.get("quoted_media_mime_type"),
            quoted_media_type_label=data.get("quoted_media_type_label"),
        )


@dataclass(frozen=True)
class OutgoingMessage:
    # Exactly one of text/media_path is set per message (SPEC.md §7.4/§7.5):
    # a voice-note or file reply is its own separate message, not tacked
    # onto the text reply already sent for the same result. `caption` is
    # only meaningful alongside media_path (a document's on-screen caption,
    # e.g. its filename) — it is not a substitute for `text`.
    text: str | None = None
    in_reply_to: str | None = None
    media_path: str | None = None
    media_mime_type: str | None = None
    media_filename: str | None = None
    caption: str | None = None
    # Whether the bridge should delete media_path after sending it: True for
    # synthesized TTS temp files, False for whatsapp-outbox/ files, which the
    # orchestrator has already relocated to their permanent sent/ home
    # (SPEC.md §7.5).
    delete_after_send: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "in_reply_to": self.in_reply_to,
            "media_path": self.media_path,
            "media_mime_type": self.media_mime_type,
            "media_filename": self.media_filename,
            "caption": self.caption,
            "delete_after_send": self.delete_after_send,
        }
