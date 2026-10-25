from wasap_control.voice_summary import (
    VOICE_SUMMARY_HEADING,
    extract_voice_summary,
    split_reply,
)


def test_no_heading_returns_full_text_and_no_summary():
    text = "Here is your answer.\nSecond line."
    remaining, summary = extract_voice_summary(text)
    assert remaining == text
    assert summary is None


def test_heading_splits_text_and_summary():
    text = f"Full answer body.\nMore detail.\n\n{VOICE_SUMMARY_HEADING}\nShort spoken version."
    remaining, summary = extract_voice_summary(text)
    assert remaining == "Full answer body.\nMore detail."
    assert summary == "Short spoken version."


def test_heading_line_with_trailing_whitespace_still_matches():
    text = f"Answer.\n{VOICE_SUMMARY_HEADING}   \nSpoken summary."
    remaining, summary = extract_voice_summary(text)
    assert remaining == "Answer."
    assert summary == "Spoken summary."


def test_summary_truncated_to_word_cap():
    text = f"Answer.\n{VOICE_SUMMARY_HEADING}\n" + " ".join(f"word{i}" for i in range(150))
    remaining, summary = extract_voice_summary(text, word_cap=100)
    assert remaining == "Answer."
    assert summary is not None
    assert summary.split() == [f"word{i}" for i in range(100)]


def test_summary_under_word_cap_is_unchanged():
    text = f"Answer.\n{VOICE_SUMMARY_HEADING}\nJust five short words here."
    remaining, summary = extract_voice_summary(text, word_cap=100)
    assert summary == "Just five short words here."


def test_organic_earlier_mention_does_not_truncate_answer():
    # The heading text appears once organically inside the answer body (e.g.
    # Claude discussing this very feature), and again for real at the end.
    text = (
        "Sure, I can explain that.\n"
        f'The feature adds a "{VOICE_SUMMARY_HEADING}" heading to replies.\n'
        "Here is the rest of the detailed answer that must not be lost.\n"
        "Final paragraph of the real answer.\n\n"
        f"{VOICE_SUMMARY_HEADING}\n"
        "Short spoken version."
    )
    remaining, summary = extract_voice_summary(text)
    assert remaining == (
        "Sure, I can explain that.\n"
        f'The feature adds a "{VOICE_SUMMARY_HEADING}" heading to replies.\n'
        "Here is the rest of the detailed answer that must not be lost.\n"
        "Final paragraph of the real answer."
    )
    assert summary == "Short spoken version."


def test_heading_inside_earlier_code_fence_is_preserved():
    # The heading appears verbatim inside a fenced code block earlier in the
    # reply (e.g. Claude showing the system-prompt source), and again for
    # real at the end.
    code_block = (
        "```python\n"
        'VOICE_SUMMARY_HEADING = "## \U0001f50a Audio Summary"\n'
        f"{VOICE_SUMMARY_HEADING}\n"
        "```"
    )
    text = f"Here's the constant:\n{code_block}\nThat's the whole answer.\n\n{VOICE_SUMMARY_HEADING}\nSpoken summary."
    remaining, summary = extract_voice_summary(text)
    assert code_block in remaining
    assert remaining == f"Here's the constant:\n{code_block}\nThat's the whole answer."
    assert summary == "Spoken summary."


def test_heading_appearing_twice_in_a_row_does_not_crash_or_lose_content():
    # Splitting on the LAST occurrence is fine here (documented as
    # acceptable) — the only hard requirements are: no crash, and the
    # content before the first occurrence is not lost.
    text = (
        "Real answer content that must survive.\n\n"
        f"{VOICE_SUMMARY_HEADING}\n"
        f"{VOICE_SUMMARY_HEADING}\n"
        "Spoken summary."
    )
    remaining, summary = extract_voice_summary(text)
    assert remaining.startswith("Real answer content that must survive.")
    assert summary == "Spoken summary."


def test_split_reply_no_heading_returns_full_text_for_both():
    text = "Just a plain full answer with no summary section."
    text_for_reply, voice_text = split_reply(text)
    assert text_for_reply == text
    assert voice_text == text


def test_split_reply_with_content_before_heading():
    text = f"Full answer body.\nMore detail.\n\n{VOICE_SUMMARY_HEADING}\nShort spoken version."
    text_for_reply, voice_text = split_reply(text)
    assert text_for_reply == "Full answer body.\nMore detail."
    assert voice_text == "Short spoken version."


def test_split_reply_with_nothing_before_heading_uses_summary_for_text_reply():
    text = f"{VOICE_SUMMARY_HEADING}\nThe build passed."
    text_for_reply, voice_text = split_reply(text)
    assert text_for_reply == "The build passed."
    assert VOICE_SUMMARY_HEADING not in text_for_reply
    assert voice_text == "The build passed."


def test_split_reply_empty_text_with_no_heading():
    text_for_reply, voice_text = split_reply("")
    assert text_for_reply == ""
    assert voice_text == ""
