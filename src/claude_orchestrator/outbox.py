"""Outgoing files from Claude Code via a project-local whatsapp-outbox/
convention (SPEC.md §7.5) — the reverse of whatsapp-inbox/ for incoming
attachments (dispatcher.py's _stage_attachment). Claude Code itself decides
what to send by placing files there; nothing else it creates in the project
is ever touched.
"""

from __future__ import annotations

import hashlib
import mimetypes
from dataclasses import dataclass
from pathlib import Path

MAX_FILE_SIZE_BYTES = 64 * 1024 * 1024

# Passed via --append-system-prompt when a live session starts
# (live_session.py), so it stays in context for the session's whole
# lifetime, whichever device the prompts come from.
OUTBOX_SYSTEM_PROMPT = (
    "If asked to send the user a file over WhatsApp (e.g. a generated PDF "
    "report), save it into ./whatsapp-outbox/ (create the folder if it "
    "doesn't exist) — anything placed there is automatically delivered "
    "after your response, then archived into ./whatsapp-outbox/sent/. Keep "
    "individual files under 64MB. Don't use this folder for anything else."
)

# Keeps the folder out of git on its own, so the project's own .gitignore
# is never touched. Never sent.
_GITIGNORE = ".gitignore"


def content_hash8(path: Path) -> str:
    """First 8 hex chars of the file's sha256 — same content always gets the
    same suffix, so identical files sent at different times don't collide
    (used for both whatsapp-outbox/sent/ here and whatsapp-inbox/ in
    dispatcher.py)."""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:8]


@dataclass
class OutboxFile:
    path: Path
    mime_type: str


@dataclass
class OutboxResult:
    sent: list[OutboxFile]
    oversized: list[tuple[Path, int]]


def _ensure_self_ignored(outbox: Path) -> None:
    gitignore = outbox / _GITIGNORE
    if not gitignore.exists():
        gitignore.write_text("*\n", encoding="utf-8")


def collect_outgoing_files(project_path: Path) -> OutboxResult:
    """Scan <project>/whatsapp-outbox/ (top level only, not sent/) for files
    to send, moving each one into whatsapp-outbox/sent/ as it's picked up so
    it's never resent on a later call."""
    outbox = project_path / "whatsapp-outbox"
    if not outbox.is_dir():
        return OutboxResult(sent=[], oversized=[])

    _ensure_self_ignored(outbox)

    sent_dir = outbox / "sent"
    sent: list[OutboxFile] = []
    oversized: list[tuple[Path, int]] = []

    for entry in sorted(outbox.iterdir()):
        if not entry.is_file() or entry.name == _GITIGNORE:
            continue
        size = entry.stat().st_size
        if size > MAX_FILE_SIZE_BYTES:
            oversized.append((entry, size))
            continue
        sent_dir.mkdir(exist_ok=True)
        dest = sent_dir / f"{entry.stem}_{content_hash8(entry)}{entry.suffix}"
        entry.rename(dest)
        mime_type = mimetypes.guess_type(dest.name)[0] or "application/octet-stream"
        sent.append(OutboxFile(path=dest, mime_type=mime_type))

    return OutboxResult(sent=sent, oversized=oversized)
