# Voice Audio-Summary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When voice replies (`/voice on`) are enabled, the voice note speaks a short (≤100 word) spoken-style summary of the answer instead of the full raw text, while the WhatsApp text reply keeps carrying the complete answer unchanged.

**Architecture:** Claude Code is told (via a per-invocation `--append-system-prompt` fragment, added only when `voice_enabled` is true) to end its reply with a `## 🔊 Audio Summary` heading followed by the short summary. The dispatcher splits that section off the raw response before formatting: the part before the heading goes to the text channel as today; the part after the heading (hard-truncated to 100 words as a safety net) is what gets spoken. If Claude doesn't include the heading, the dispatcher falls back to today's behavior — speaking the full response — so the change can only make voice replies shorter, never break them.

**Tech Stack:** Python 3.14, pytest (new dev dependency — the project has no test suite today), uv.

**Spec:** No existing SPEC.md section covers this; it was designed via a `/grilling` session in this conversation, not against a written spec doc. Decisions reached (referenced throughout the tasks below):
1. Summary is produced by the *same* Claude Code invocation via a marked heading in `result.text`, not a second LLM call.
2. Default cap: 100 words, enforced primarily via the system-prompt instruction, with a code-side hard-truncate as a safety net.
3. If the raw answer is already short, no separate summarization step runs — but this is handled implicitly: `extract_voice_summary` only *acts* when the heading is present, and Claude is instructed to always emit the heading when told voice is enabled, so "already short" answers still get a (trivially short) summary. No special-case branch needed.
4. Bundled into the existing `/voice on` toggle — no second toggle.
5. Cap is words, not seconds.
6. Marker syntax is a distinctive Markdown-ish heading: `## 🔊 Audio Summary`, chosen for near-zero collision risk with organic content.
7. The heading + everything after it is stripped from the text reply — the text channel never shows it.
8. Fallback: no heading found → speak the full `result.text` (today's behavior, unchanged). Heading found but summary over the word cap → hard-truncate at the cap.
9. This deliberately departs from SPEC.md's stated preference against parsing markers out of Claude's free-form reply text (SPEC.md:408, SPEC.md:426) — acknowledged as an intentional exception, since a missed/malformed marker here degrades gracefully (falls back to full-text speech) rather than silently breaking an action, which is what motivated SPEC's rule in the other two cases.

## Global Constraints

- Python 3.14 (per this machine's global convention; the project already targets `requires-python = ">=3.11"` — no change needed there).
- After any code change in this repo, restart the affected `systemctl --user` service(s) (`claude-orchestrator.service`) without asking first (CLAUDE.md).
- No history/justification language in code or docs — write every comment and docstring as if the new behavior always existed.
- Follow the existing code style: `from __future__ import annotations`, dataclasses, keyword-only params after `cwd`/`prompt`-style positional args, alphabetically-ordered local imports.

---

## File Structure

- **Create** `src/claude_orchestrator/voice_summary.py` — the audio-summary marker convention: heading constant, word cap constant, the system-prompt fragment text, and the pure `extract_voice_summary()` splitting function. Kept separate from `formatting.py` (which owns Markdown→WhatsApp transforms and TTS text-stripping, not response-splitting) and mirrors how `outbox.py` already owns its own file-delivery convention.
- **Create** `tests/claude_orchestrator/test_voice_summary.py` — unit tests for `extract_voice_summary()`.
- **Create** `tests/claude_orchestrator/test_claude_code.py` — unit tests for the new pure args-building helper in `claude_code.py`.
- **Modify** `src/claude_orchestrator/claude_code.py` — extract a pure `_build_args()` helper out of `ClaudeCodeRunner.start()`, add an `extra_system_prompt` parameter that gets appended to `OUTBOX_SYSTEM_PROMPT`.
- **Modify** `src/claude_orchestrator/dispatcher.py` — pass `VOICE_SUMMARY_SYSTEM_PROMPT` into `runner.start()` when voice is enabled; split `result.text` via `extract_voice_summary()` before sending the text reply; speak the summary (or fall back to the full text) in `_reply_voice`.
- **Modify** `pyproject.toml` — add `pytest` as a dev dependency (via `uv add --dev pytest`).

---

### Task 1: Audio-summary extraction (`voice_summary.py`)

**Files:**
- Create: `src/claude_orchestrator/voice_summary.py`
- Test: `tests/claude_orchestrator/test_voice_summary.py`
- Modify: `pyproject.toml` (dev dependency)

**Interfaces:**
- Produces: `VOICE_SUMMARY_HEADING: str` (`"## 🔊 Audio Summary"`), `VOICE_SUMMARY_WORD_CAP: int` (`100`), `VOICE_SUMMARY_SYSTEM_PROMPT: str`, `extract_voice_summary(text: str, word_cap: int = VOICE_SUMMARY_WORD_CAP) -> tuple[str, str | None]`. Task 3 imports all four names from `claude_orchestrator.voice_summary`.

- [ ] **Step 1: Add pytest as a dev dependency**

Run:
```bash
cd /home/centos/PycharmProjects/wasap_grill_me
~/.local/bin/uv add --dev pytest
```

Expected: `pyproject.toml` gains a `[dependency-groups]` (or `[tool.uv]`) section listing `pytest`, and `.venv/bin/pytest` exists afterward.

- [ ] **Step 2: Create the tests directory and write the failing tests**

Create `tests/claude_orchestrator/__init__.py` (empty file) and `tests/claude_orchestrator/test_voice_summary.py`:

```python
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
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/claude_orchestrator/test_voice_summary.py -v`
Expected: FAIL/ERROR with `ModuleNotFoundError: No module named 'claude_orchestrator.voice_summary'`.

- [ ] **Step 4: Implement `voice_summary.py`**

```python
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/claude_orchestrator/test_voice_summary.py -v`
Expected: all 5 tests PASS.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock tests/claude_orchestrator/__init__.py tests/claude_orchestrator/test_voice_summary.py src/claude_orchestrator/voice_summary.py
git commit -m "feat: add audio-summary marker extraction for voice replies"
```

---

### Task 2: Conditional system-prompt injection (`claude_code.py`)

**Files:**
- Modify: `src/claude_orchestrator/claude_code.py:69-108`
- Test: `tests/claude_orchestrator/test_claude_code.py`

**Interfaces:**
- Consumes: nothing from Task 1 directly (this task only adds a generic `extra_system_prompt: str | None` parameter — it doesn't import `voice_summary`).
- Produces: `ClaudeCodeRunner.start()` gains a new keyword-only parameter `extra_system_prompt: str | None = None`. A pure helper `_build_args(binary: str, prompt: str, resume_session_id: str | None, model: str | None, extra_system_prompt: str | None) -> list[str]` becomes importable from `claude_orchestrator.claude_code` for testing. Task 3 calls `runner.start(..., extra_system_prompt=...)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/claude_orchestrator/test_claude_code.py`:

```python
from claude_orchestrator.claude_code import _build_args
from claude_orchestrator.outbox import OUTBOX_SYSTEM_PROMPT


def test_build_args_minimal():
    args = _build_args("claude", "do the thing", None, None, None)
    assert args == [
        "claude",
        "-p",
        "do the thing",
        "--output-format",
        "stream-json",
        "--verbose",
        "--dangerously-skip-permissions",
        "--append-system-prompt",
        OUTBOX_SYSTEM_PROMPT,
    ]


def test_build_args_with_resume_and_model():
    args = _build_args("claude", "prompt", "session-123", "opus", None)
    assert args[args.index("--resume") + 1] == "session-123"
    assert args[args.index("--model") + 1] == "opus"


def test_build_args_appends_extra_system_prompt():
    args = _build_args("claude", "prompt", None, None, "extra instruction")
    system_prompt = args[args.index("--append-system-prompt") + 1]
    assert system_prompt == f"{OUTBOX_SYSTEM_PROMPT}\n\nextra instruction"


def test_build_args_without_extra_system_prompt_is_unchanged():
    args = _build_args("claude", "prompt", None, None, None)
    system_prompt = args[args.index("--append-system-prompt") + 1]
    assert system_prompt == OUTBOX_SYSTEM_PROMPT
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/claude_orchestrator/test_claude_code.py -v`
Expected: FAIL/ERROR with `ImportError: cannot import name '_build_args'`.

- [ ] **Step 3: Implement `_build_args` and wire it into `start()`**

Replace `src/claude_orchestrator/claude_code.py:69-108` (the `ClaudeCodeRunner` class) with:

```python
class ClaudeCodeRunner:
    def __init__(self, binary: str = "claude") -> None:
        self.binary = binary

    async def start(
        self,
        *,
        cwd: Path,
        prompt: str,
        resume_session_id: str | None,
        model: str | None = None,
        extra_system_prompt: str | None = None,
    ) -> ClaudeCodeInvocation:
        args = _build_args(self.binary, prompt, resume_session_id, model, extra_system_prompt)

        process = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(cwd),
            # asyncio's default readline() buffer is 64KiB, which a single
            # stream-json event can exceed once file content (e.g. a read
            # PDF/document) ends up embedded in a tool-result line — raise it
            # generously rather than truncating/crashing mid-task.
            limit=32 * 1024 * 1024,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        return ClaudeCodeInvocation(process)


def _build_args(
    binary: str,
    prompt: str,
    resume_session_id: str | None,
    model: str | None,
    extra_system_prompt: str | None,
) -> list[str]:
    system_prompt = OUTBOX_SYSTEM_PROMPT
    if extra_system_prompt:
        system_prompt = f"{OUTBOX_SYSTEM_PROMPT}\n\n{extra_system_prompt}"

    args = [
        binary,
        "-p",
        prompt,
        "--output-format",
        "stream-json",
        "--verbose",
        "--dangerously-skip-permissions",
        "--append-system-prompt",
        system_prompt,
    ]
    if resume_session_id:
        args += ["--resume", resume_session_id]
    if model:
        args += ["--model", model]
    return args
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/claude_orchestrator/test_claude_code.py -v`
Expected: all 4 tests PASS.

- [ ] **Step 5: Run the full test suite to check nothing else broke**

Run: `.venv/bin/pytest -v`
Expected: all tests (Task 1's and Task 2's) PASS.

- [ ] **Step 6: Commit**

```bash
git add src/claude_orchestrator/claude_code.py tests/claude_orchestrator/test_claude_code.py
git commit -m "feat: allow injecting an extra system-prompt fragment per Claude Code invocation"
```

---

### Task 3: Wire voice-enabled invocations through the dispatcher

**Files:**
- Modify: `src/claude_orchestrator/dispatcher.py:17-26` (imports), `dispatcher.py:482-538` (`_handle_prompt`)

**Interfaces:**
- Consumes: `VOICE_SUMMARY_SYSTEM_PROMPT`, `extract_voice_summary` from `claude_orchestrator.voice_summary` (Task 1); `extra_system_prompt` parameter of `ClaudeCodeRunner.start()` (Task 2).
- Produces: no new public interface — this is the final integration point.

This task's logic is a small, sequential change inside one existing async method that already has no unit tests (constructing a `Dispatcher` requires a real `Config`, `RedisBus`, and touches the real `~/.local/share/claude-orchestrator/state.json` via `load_state()` in `__init__` — no existing test scaffolding does this anywhere in the codebase, so this task is verified by manual end-to-end check via the running service instead of a new mocking harness, consistent with how the rest of `dispatcher.py` is tested today).

- [ ] **Step 1: Add the import**

In `src/claude_orchestrator/dispatcher.py`, after line 26 (`from .tts import Synthesizer`), add:

```python
from .voice_summary import VOICE_SUMMARY_SYSTEM_PROMPT, extract_voice_summary
```

- [ ] **Step 2: Pass the extra system prompt when voice is enabled**

Replace `dispatcher.py:495-499`:

```python
        invocation = await self._runner.start(
            cwd=state.config.path,
            prompt=prompt,
            resume_session_id=state.session_id,
        )
```

with:

```python
        invocation = await self._runner.start(
            cwd=state.config.path,
            prompt=prompt,
            resume_session_id=state.session_id,
            extra_system_prompt=(
                VOICE_SUMMARY_SYSTEM_PROMPT if self._state.voice_enabled else None
            ),
        )
```

- [ ] **Step 3: Split the summary off before sending the text reply, and speak it instead of the full text**

Replace `dispatcher.py:534-538`:

```python
        for chunk in format_response(result.text):
            await self._reply(chunk, message)

        if self._state.voice_enabled and result.text.strip():
            await self._reply_voice(result.text, message)
```

with:

```python
        text_for_reply, voice_summary = extract_voice_summary(result.text)

        for chunk in format_response(text_for_reply):
            await self._reply(chunk, message)

        if self._state.voice_enabled and result.text.strip():
            await self._reply_voice(voice_summary or result.text, message)
```

- [ ] **Step 4: Run the full test suite**

Run: `.venv/bin/pytest -v`
Expected: all tests still PASS (this task touches no tested pure function, so this just guards against an import/syntax error).

- [ ] **Step 5: Restart the service**

Per this repo's working agreement, restart without asking first:

```bash
systemctl --user daemon-reload
systemctl --user restart claude-orchestrator.service
sleep 2
systemctl --user is-active claude-orchestrator.service
```

Expected: `active`.

- [ ] **Step 6: Manual end-to-end verification over WhatsApp**

1. Send `/voice on` to the bot.
2. Send a prompt that produces a multi-paragraph answer (e.g. "explain what this project does in a few paragraphs").
3. Confirm: the text reply in WhatsApp contains the full answer with **no** `## 🔊 Audio Summary` heading visible anywhere in it.
4. Confirm: a voice note arrives, and its spoken content is a short (~100-word) summary, not a verbatim reading of the full text reply.
5. Send `/voice off`, then repeat step 2 — confirm no voice note is sent (unchanged pre-existing behavior).
6. Check the service log for any exceptions from this code path: `journalctl --user -u claude-orchestrator.service -n 100 --no-pager | grep -i "voice\|traceback"`.

- [ ] **Step 7: Commit**

```bash
git add src/claude_orchestrator/dispatcher.py
git commit -m "feat: speak a short summary instead of the full text when voice replies are enabled"
```

---

## Self-Review

**Spec coverage:** All 9 grilling decisions above map to a task: (1)/(6)/(9) → Task 1's marker design; (2)/(8) → Task 1's word-cap truncation + Task 3's fallback-to-full-text; (3) → implicit (no special-case needed, noted in Architecture); (4) → Task 3 reuses `self._state.voice_enabled`, no new state field added; (5) → `extract_voice_summary`'s `word_cap` param counts words; (7) → Task 3's `text_for_reply` (heading-stripped) is what's sent to the text channel.

**Placeholder scan:** No TBD/TODO markers; every step has runnable code and exact expected output.

**Type consistency:** `extract_voice_summary(text: str, word_cap: int = VOICE_SUMMARY_WORD_CAP) -> tuple[str, str | None]` (Task 1) is called in Task 3 exactly as `text_for_reply, voice_summary = extract_voice_summary(result.text)` — matches. `_build_args(binary, prompt, resume_session_id, model, extra_system_prompt) -> list[str]` (Task 2) matches its use inside `start()`. `ClaudeCodeRunner.start(..., extra_system_prompt: str | None = None)` (Task 2) matches the Task 3 call site's keyword argument name.
