"""Audio-summary marker convention for voice replies. When voice replies are
enabled for a message, Claude Code is instructed (VOICE_SUMMARY_SYSTEM_PROMPT,
added to that prompt by hook_relay.py) to end its reply with a "## 🔊 Audio Summary"
heading followed by a short spoken-style summary. extract_voice_summary
splits that section off the raw response so the text reply stays clean and
the voice reply speaks only the summary.
"""

from __future__ import annotations

VOICE_SUMMARY_HEADING = "## 🔊 Audio Summary"
VOICE_SUMMARY_WORD_CAP = 100

# Added as UserPromptSubmit context (hook_relay.py), only for WhatsApp
# messages sent while voice replies are enabled.
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
    words.

    The LAST line matching the heading is used, not the first: the heading
    text can legitimately appear earlier in the reply (Claude quoting it,
    mentioning it organically, or emitting it inside a code fence), and the
    real heading — the one instructing where the spoken summary starts — is
    always the one closest to the end, per VOICE_SUMMARY_SYSTEM_PROMPT's own
    instruction that it comes after the full answer."""
    lines = text.split("\n")
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].strip() == VOICE_SUMMARY_HEADING:
            remaining = "\n".join(lines[:index]).rstrip()
            summary = "\n".join(lines[index + 1 :]).strip()
            words = summary.split()
            if len(words) > word_cap:
                summary = " ".join(words[:word_cap])
            return remaining, summary
    return text, None


def split_reply(text: str, word_cap: int = VOICE_SUMMARY_WORD_CAP) -> tuple[str, str]:
    """Split a Claude Code response into (text_for_reply, voice_text): what
    the text channel sends, and what gets spoken if voice replies are
    enabled. text_for_reply never contains the audio-summary heading: when
    there's no content before the heading, the summary itself becomes
    text_for_reply instead of falling back to the raw (heading-including)
    text. voice_text is the summary when one exists, otherwise it falls back
    to text_for_reply — which is the full original text when there's no
    heading at all."""
    remaining, summary = extract_voice_summary(text, word_cap)
    text_for_reply = remaining if remaining.strip() else (summary or text)
    voice_text = summary or text_for_reply
    return text_for_reply, voice_text
