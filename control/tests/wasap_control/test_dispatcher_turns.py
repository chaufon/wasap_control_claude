import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from wasap_control import dispatcher as dispatcher_module
from wasap_control.commands import Command
from wasap_control.dispatcher import Dispatcher
from wasap_control.models import IncomingMessage
from wasap_control.state import PersistedState

from .conftest import FakeBus, FakeLive, MakeDispatcher

if TYPE_CHECKING:
    from _pytest.monkeypatch import MonkeyPatch

MESSAGE = IncomingMessage(message_id="m1", timestamp="t", sender_jid="me", type="text", text="")


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0.02)


async def _event(dispatcher: Dispatcher, kind: str, **fields: object) -> None:
    await dispatcher._on_hook_event({"hook_event_name": kind, "live_session": "rc-main", **fields})


async def _answer(dispatcher: Dispatcher, prompt_id: str, text: str) -> None:
    """The hooks of a WhatsApp prompt, from submit to Stop."""
    await _event(dispatcher, "UserPromptSubmit", origin="whatsapp", prompt_id=prompt_id)
    await _event(dispatcher, "Stop", prompt_id=prompt_id, last_assistant_message=text)


async def _drain(dispatcher: Dispatcher) -> None:
    worker = dispatcher._projects["main"].worker
    if worker is not None:
        await asyncio.wait_for(worker, 2)


def _start(make_dispatcher: MakeDispatcher, **state: object) -> tuple[Dispatcher, FakeBus, FakeLive]:
    dispatcher, bus, live = make_dispatcher(state=PersistedState(**state))  # type: ignore[arg-type]
    live.register("rc-main")
    return dispatcher, bus, live


def test_a_prompt_is_typed_and_its_answer_relayed(make_dispatcher: MakeDispatcher) -> None:
    """A WhatsApp prompt is typed into the session, marked as WhatsApp's, and its Stop answer is sent back."""
    dispatcher, bus, live = _start(make_dispatcher)

    async def scenario() -> None:
        await dispatcher._handle_prompt("fix the bug", MESSAGE)
        await _settle()
        assert live.typed == [("rc-main", "fix the bug")]
        assert live.pending == {"rc-main": False}
        await _answer(dispatcher, "p1", "Fixed **it**.")
        await _drain(dispatcher)

    asyncio.run(scenario())
    assert bus.texts == ["⏳ Working on it...", "Fixed *it*."]
    assert dispatcher._state.last_answers == {"main": "Fixed **it**."}
    assert live.pending == {}


def test_answers_to_other_devices_are_kept_but_not_sent(make_dispatcher: MakeDispatcher) -> None:
    """A turn typed on the phone updates /last and the session ID, and sends nothing to WhatsApp."""
    dispatcher, bus, _ = _start(make_dispatcher)

    async def scenario() -> None:
        await _event(dispatcher, "SessionStart", session_id="new-id", source="startup")
        await _event(dispatcher, "UserPromptSubmit", prompt_id="p9")
        await _event(dispatcher, "Stop", prompt_id="p9", last_assistant_message="from the phone")

    asyncio.run(scenario())
    assert bus.texts == []
    assert dispatcher._state.last_answers == {"main": "from the phone"}
    assert dispatcher._state.shared_sessions == {"main": "new-id"}


def test_a_busy_session_queues_and_stop_drops_the_queue(
    make_dispatcher: MakeDispatcher, monkeypatch: "MonkeyPatch"
) -> None:
    """While the session is busy a prompt waits in the queue; /stop drops it and interrupts the running turn."""
    monkeypatch.setattr(dispatcher_module, "_BUSY_GRACE_SECONDS", 0.05)
    dispatcher, bus, live = _start(make_dispatcher)
    live.register("rc-main", busy=True)

    async def scenario() -> None:
        await dispatcher._handle_prompt("later", MESSAGE)
        await _settle()
        assert live.typed == []
        await dispatcher._handle_command(Command(name="stop"), MESSAGE)
        await _drain(dispatcher)

    asyncio.run(scenario())
    assert bus.texts == ["⏳ Busy, queued. Send /stop to drop it.", "🛑 Dropped 1 queued message.", "🛑 Stopped."]
    assert live.keys == [("rc-main", "Escape")]


def test_a_queued_prompt_goes_in_once_the_session_is_idle(
    make_dispatcher: MakeDispatcher, monkeypatch: "MonkeyPatch"
) -> None:
    """The queued prompt is typed after the session turns idle, without a busy notice when it frees up in time."""
    monkeypatch.setattr(dispatcher_module, "_BUSY_GRACE_SECONDS", 0.05)
    dispatcher, bus, live = _start(make_dispatcher)
    live.register("rc-main", busy=True)

    async def scenario() -> None:
        await dispatcher._handle_prompt("later", MESSAGE)
        await _settle()
        live.register("rc-main")
        await _settle()
        assert live.typed == [("rc-main", "later")]
        await _answer(dispatcher, "p1", "ok")
        await _drain(dispatcher)

    asyncio.run(scenario())
    assert bus.texts == ["⏳ Busy, queued. Send /stop to drop it.", "⏳ Working on it...", "ok"]


def test_a_session_that_frees_up_quickly_is_not_called_busy(make_dispatcher: MakeDispatcher) -> None:
    """A session still busy for a moment after its answer gets no busy notice."""
    dispatcher, bus, live = _start(make_dispatcher)
    live.register("rc-main", busy=True)

    async def scenario() -> None:
        handled = asyncio.create_task(dispatcher._handle_prompt("next", MESSAGE))
        await asyncio.sleep(0.05)
        live.register("rc-main")
        await handled
        await _settle()
        await _answer(dispatcher, "p1", "ok")
        await _drain(dispatcher)

    asyncio.run(scenario())
    assert bus.texts == ["⏳ Working on it...", "ok"]


def test_an_interrupted_turn_reports_stopped(make_dispatcher: MakeDispatcher) -> None:
    """A started turn that ends with no Stop event (Esc) is reported stopped once the session stays idle."""
    dispatcher, bus, live = _start(make_dispatcher)

    async def scenario() -> None:
        await dispatcher._handle_prompt("long task", MESSAGE)
        await _settle()
        await _event(dispatcher, "UserPromptSubmit", origin="whatsapp", prompt_id="p1")
        await dispatcher._handle_command(Command(name="stop"), MESSAGE)
        await _drain(dispatcher)

    asyncio.run(scenario())
    assert live.keys == [("rc-main", "Escape")]
    assert bus.texts == ["⏳ Working on it...", "🛑 Stopped."]


def test_tool_calls_of_the_turn_become_progress(make_dispatcher: MakeDispatcher) -> None:
    """Only tool calls of the WhatsApp turn are counted in its progress update."""
    dispatcher, bus, _ = _start(make_dispatcher)

    async def scenario() -> None:
        await dispatcher._handle_prompt("go", MESSAGE)
        await _settle()
        await _event(dispatcher, "UserPromptSubmit", origin="whatsapp", prompt_id="p1")
        await _event(dispatcher, "PostToolUse", prompt_id="p1", tool_name="Read")
        await _event(dispatcher, "PostToolUse", prompt_id="p1", tool_name="Read")
        await _event(dispatcher, "PostToolUse", prompt_id="other", tool_name="Bash")
        turn = dispatcher._projects["main"].turn
        assert turn is not None and turn.progress is not None
        await turn.progress._flush()
        await _event(dispatcher, "Stop", prompt_id="p1", last_assistant_message="done")
        await _drain(dispatcher)

    asyncio.run(scenario())
    assert bus.texts == ["⏳ Working on it...", "🔧 Read x2", "done"]


def test_a_dead_session_restarts_by_resuming(make_dispatcher: MakeDispatcher) -> None:
    """A prompt for a project whose session is gone restarts it from its stored session first."""
    dispatcher, bus, live = make_dispatcher(state=PersistedState(shared_sessions={"main": "abc"}))

    async def scenario() -> None:
        await dispatcher._handle_prompt("hi", MESSAGE)
        await _settle()
        await _answer(dispatcher, "p1", "hello")
        await _drain(dispatcher)

    asyncio.run(scenario())
    assert live.launches == [("main", "abc")]
    assert bus.texts[0] == (
        "♻️ Session restarted.\n"
        "📱 rc-main is live in the Claude app: https://claude.ai/code/session_x\n"
        "🖥️ On the computer: ```tmux attach -t rc-main```\n"
        "🚪 To leave, close the terminal window or tab; /exit ends the session"
    )
    assert bus.texts[-1] == "hello"


def test_a_failed_start_drops_the_prompt(make_dispatcher: MakeDispatcher) -> None:
    """When the session cannot start, the prompt is not queued and the failure is reported."""
    dispatcher, bus, live = make_dispatcher()
    live.launch_status = "failed"
    asyncio.run(dispatcher._handle_prompt("hi", MESSAGE))
    assert bus.texts == ["❌ rc-main failed to start:\nboom"]
    assert not dispatcher._projects["main"].queue


def test_c_clear_confirms_the_fresh_conversation(make_dispatcher: MakeDispatcher) -> None:
    """/c /clear replies once the session reports the cleared conversation."""
    dispatcher, bus, live = _start(make_dispatcher)

    async def scenario() -> None:
        await dispatcher._handle_command(Command(name="c", arg="/clear"), MESSAGE)
        await _settle()
        await _event(dispatcher, "SessionStart", session_id="fresh", source="clear")
        await _drain(dispatcher)

    asyncio.run(scenario())
    assert live.typed == [("rc-main", "/clear")]
    assert bus.texts == ["🧹 Session cleared. The next message starts a fresh conversation."]
    assert dispatcher._state.shared_sessions == {"main": "fresh"}


def test_c_builtin_command_replies_with_the_screen(make_dispatcher: MakeDispatcher, monkeypatch: "MonkeyPatch") -> None:
    """A built-in /c command that never becomes a prompt replies with what it drew, then closes it."""
    monkeypatch.setattr(dispatcher_module, "_COMMAND_GRACE_SECONDS", 0.05)
    monkeypatch.setattr(dispatcher_module, "_SCREEN_SETTLE_SECONDS", 0)
    dispatcher, bus, live = _start(make_dispatcher)
    live.screen = "❯ /usage\n  Current session 4% used\n────\n❯\n────\n"

    async def scenario() -> None:
        await dispatcher._handle_command(Command(name="c", arg="/usage"), MESSAGE)
        await _drain(dispatcher)

    asyncio.run(scenario())
    assert bus.texts == ["🖥️ /usage\n```\n  Current session 4% used\n```"]
    assert live.keys == [("rc-main", "Escape")]


def test_c_skill_is_a_normal_turn(make_dispatcher: MakeDispatcher) -> None:
    """A /c slash command that becomes a prompt (a skill) is answered like any prompt."""
    dispatcher, bus, _ = _start(make_dispatcher)

    async def scenario() -> None:
        await dispatcher._handle_command(Command(name="c", arg="/grill-me an idea"), MESSAGE)
        await _settle()
        await _answer(dispatcher, "p1", "Q1?")
        await _drain(dispatcher)

    asyncio.run(scenario())
    assert bus.texts == ["⏳ Working on it...", "Q1?"]


def test_voice_flag_reaches_the_marker(make_dispatcher: MakeDispatcher, root: Path) -> None:
    """With voice replies on, the pending marker asks the relay for the audio summary."""
    dispatcher, _, live = _start(make_dispatcher, voice_enabled=True)

    async def scenario() -> None:
        await dispatcher._handle_prompt("hi", MESSAGE)
        await _settle()
        assert live.pending == {"rc-main": True}
        dispatcher._projects["main"].queue.clear()
        worker = dispatcher._projects["main"].worker
        assert worker is not None
        worker.cancel()

    asyncio.run(scenario())
