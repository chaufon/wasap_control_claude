"""Best-effort Markdown -> WhatsApp formatting, and long-message splitting.

WhatsApp only understands: *bold*, _italic_, ~strikethrough~, and
```monospace``` blocks. Everything else (headers, tables, links) is
flattened to plain text rather than dropped.
"""

from __future__ import annotations

import re

MAX_MESSAGE_LENGTH = 4000

_FENCE_RE = re.compile(r"```(?:[^\n`]*\n)?(.*?)```", re.DOTALL)
_HEADER_RE = re.compile(r"^(#{1,6})[ \t]+(.*)$", re.MULTILINE)
# Bold (**text**) and italic (*text* or _text_) in one pass, so the output
# of one alternative is never re-matched by another (that caused **bug**
# to become *bug* and then get re-italicized to _bug_).
_EMPHASIS_RE = re.compile(
    r"\*\*(?P<bold>.+?)\*\*"
    r"|_(?P<italic_u>.+?)_"
    r"|(?<!\*)\*(?!\*)(?P<italic_a>.+?)(?<!\*)\*(?!\*)"
)
_STRIKE_RE = re.compile(r"~~(.+?)~~")
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_BULLET_RE = re.compile(r"^([ \t]*)[-*][ \t]+", re.MULTILINE)
# Use [ \t] rather than \s in these two: \s also matches newlines, which let
# the "trailing whitespace" part of the pattern swallow the line break
# between rows and merge adjacent table rows onto one line.
_TABLE_SEPARATOR_RE = re.compile(r"^[ \t]*\|?[ \t:|-]+\|[ \t:|-]+\|?[ \t]*$", re.MULTILINE)
_TABLE_ROW_RE = re.compile(r"^[ \t]*\|(.+)\|[ \t]*$", re.MULTILINE)

# Placeholder used while code fences are protected from the text transforms below.
_FENCE_PLACEHOLDER = "\x00FENCE{}\x00"


def _flatten_table_row(match: re.Match[str]) -> str:
    cells = [cell.strip() for cell in match.group(1).split("|")]
    return " | ".join(cells)


def _emphasis_sub(match: re.Match[str]) -> str:
    if match.group("bold") is not None:
        return f"*{match.group('bold')}*"
    italic = match.group("italic_u") if match.group("italic_u") is not None else match.group("italic_a")
    return f"_{italic}_"


def markdown_to_whatsapp(text: str) -> str:
    fences: list[str] = []

    def _stash_fence(match: re.Match[str]) -> str:
        fences.append(match.group(1).strip("\n"))
        return _FENCE_PLACEHOLDER.format(len(fences) - 1)

    working = _FENCE_RE.sub(_stash_fence, text)

    working = _TABLE_SEPARATOR_RE.sub("", working)
    working = _TABLE_ROW_RE.sub(_flatten_table_row, working)
    working = _LINK_RE.sub(lambda m: f"{m.group(1)} ({m.group(2)})", working)
    working = _STRIKE_RE.sub(lambda m: f"~{m.group(1)}~", working)
    # Emphasis must run before headers: headers wrap their line in *bold*,
    # and that wrapper must not be exposed to any further *later* pass that
    # also matches single asterisks (it no longer is, since this is a single
    # combined pass rather than chained bold-then-italic substitutions).
    working = _EMPHASIS_RE.sub(_emphasis_sub, working)
    working = _HEADER_RE.sub(lambda m: f"*{m.group(2).strip()}*", working)
    working = _INLINE_CODE_RE.sub(lambda m: f"```{m.group(1)}```", working)
    working = _BULLET_RE.sub(lambda m: f"{m.group(1)}• ", working)

    for index, fence_content in enumerate(fences):
        working = working.replace(_FENCE_PLACEHOLDER.format(index), f"```\n{fence_content}\n```")

    return working.strip()


def split_message(text: str, max_length: int = MAX_MESSAGE_LENGTH) -> list[str]:
    """Split text into WhatsApp-sized chunks, splitting on line boundaries
    and keeping fenced code blocks balanced across chunks where possible."""
    if len(text) <= max_length:
        return [text]

    lines = text.split("\n")
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    fence_open = False

    def flush() -> None:
        nonlocal current, current_len, fence_open
        if not current:
            return
        chunk = "\n".join(current)
        if fence_open:
            chunk += "\n```"
        chunks.append(chunk)
        current = ["```"] if fence_open else []
        current_len = len(current[0]) if current else 0

    for line in lines:
        if line.strip().startswith("```"):
            fence_open = not fence_open

        # A single line longer than the whole budget: hard-split it.
        if len(line) > max_length:
            flush()
            for start in range(0, len(line), max_length):
                chunks.append(line[start : start + max_length])
            continue

        added_len = len(line) + 1
        if current_len + added_len > max_length:
            flush()

        current.append(line)
        current_len += added_len

    flush()
    return [c for c in chunks if c.strip()]


def format_response(text: str, max_length: int = MAX_MESSAGE_LENGTH) -> list[str]:
    return split_message(markdown_to_whatsapp(text), max_length=max_length)


_CODE_BLOCK_PLACEHOLDER = " (code block omitted) "


def strip_for_speech(text: str) -> str:
    """Reduce Markdown to plain prose for TTS — the text
    channel keeps full Markdown via format_response above; this is only for
    what gets spoken. Code isn't meaningful read aloud, so fenced/inline
    code is dropped rather than transliterated."""
    working = _FENCE_RE.sub(_CODE_BLOCK_PLACEHOLDER, text)
    working = _TABLE_SEPARATOR_RE.sub("", working)
    working = _TABLE_ROW_RE.sub(lambda m: _flatten_table_row(m).replace(" | ", ", "), working)
    working = _LINK_RE.sub(lambda m: m.group(1), working)
    working = _STRIKE_RE.sub(lambda m: m.group(1), working)
    working = _EMPHASIS_RE.sub(
        lambda m: m.group("bold") or m.group("italic_u") or m.group("italic_a"), working
    )
    working = _HEADER_RE.sub(lambda m: m.group(2).strip(), working)
    working = _INLINE_CODE_RE.sub(lambda m: m.group(1), working)
    working = _BULLET_RE.sub("", working)
    return re.sub(r"\n{2,}", "\n", working).strip()
