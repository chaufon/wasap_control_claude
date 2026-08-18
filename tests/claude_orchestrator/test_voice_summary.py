from claude_orchestrator.voice_summary import (
    VOICE_SUMMARY_HEADING,
    extract_voice_summary,
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
