"""Audio-summary marker convention for voice replies. When voice replies are
enabled for a message, Claude Code is instructed (VOICE_SUMMARY_SYSTEM_PROMPT,
wired in via claude_code.py) to end its reply with a "## 🔊 Audio Summary"
heading followed by a short spoken-style summary. extract_voice_summary
splits that section off the raw response so the text reply stays clean and
the voice reply speaks only the summary.
"""

from __future__ import annotations

VOICE_SUMMARY_HEADING = "## 🔊 Audio Summary"
VOICE_SUMMARY_WORD_CAP = 100

# Passed as an extra --append-system-prompt fragment (claude_code.py), only
# for messages where voice replies are enabled (dispatcher.py).
VOICE_SUMMARY_SYSTEM_PROMPT = (
    "Voice replies are enabled for this message. After your full answer, "
    f'add a line containing exactly "{VOICE_SUMMARY_HEADING}" and nothing '
    "else, then below it write a spoken-style summary of your answer in "
    f"{VOICE_SUMMARY_WORD_CAP} words or fewer. Write the summary as natural "
    "spoken language (no Markdown, code, file paths, or URLs) — it will be "
    "read aloud as-is."
)


def extract_voice_summary(
    text: str, word_cap: int = VOICE_SUMMARY_WORD_CAP
) -> tuple[str, str | None]:
    """Split off the audio-summary section Claude appends when voice replies
    are enabled. Returns (remaining_text, summary): remaining_text has the
    heading and everything after it removed; summary is None if the heading
    wasn't found (caller falls back to speaking remaining_text as-is),
    otherwise the section after the heading, hard-truncated to word_cap
    words."""
    lines = text.split("\n")
    for index, line in enumerate(lines):
        if line.strip() == VOICE_SUMMARY_HEADING:
            remaining = "\n".join(lines[:index]).rstrip()
            summary = "\n".join(lines[index + 1 :]).strip()
            words = summary.split()
            if len(words) > word_cap:
                summary = " ".join(words[:word_cap])
            return remaining, summary
    return text, None
