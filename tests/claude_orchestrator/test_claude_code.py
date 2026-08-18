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
