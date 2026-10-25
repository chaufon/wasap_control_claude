import asyncio
import json
import os
import socket
import stat
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from wasap_control import socket_bus
from wasap_control.main import start_bus
from wasap_control.models import IncomingMessage, OutgoingMessage
from wasap_control.socket_bus import SocketBus, SocketInUse, default_socket_path

if TYPE_CHECKING:
    from _pytest.logging import LogCaptureFixture
    from _pytest.monkeypatch import MonkeyPatch

INCOMING = {"message_id": "m1", "timestamp": "2026-10-24T10:00:00Z", "sender_jid": "me", "type": "text", "text": "hi"}
HELLO = {"kind": "hello", "data": {"role": "bridge"}}


def _line(frame: object) -> bytes:
    return json.dumps(frame).encode() + b"\n"


async def _connect(path: Path, *frames: object) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    reader, writer = await asyncio.open_unix_connection(str(path), limit=4 * 1024 * 1024)
    for frame in frames:
        writer.write(_line(frame))
    await writer.drain()
    return reader, writer


async def _next(stream: AsyncIterator, timeout: float = 2) -> object:
    """The stream's next item, failing the test after `timeout` seconds."""
    return await asyncio.wait_for(stream.__anext__(), timeout)


def _with_bus(tmp_path: Path, scenario: Callable[[SocketBus, Path], Awaitable[None]]) -> None:
    """Run `scenario` against a started bus on a socket in tmp_path, then close it."""
    path = tmp_path / "bus.sock"

    async def run() -> None:
        bus = SocketBus(path)
        await bus.start()
        try:
            await scenario(bus, path)
        finally:
            await asyncio.wait_for(bus.close(), 5)

    asyncio.run(run())


async def _sync(bus: SocketBus, path: Path) -> None:
    """Round-trip a hook frame on a new connection, so the bus has read the
    frames sent earlier on other connections."""
    _, writer = await _connect(path, {"kind": "hook", "data": {"sync": True}})
    hooks = bus.hook_events()
    while await _next(hooks) != {"sync": True}:
        pass
    writer.close()


def test_incoming_frame_reaches_incoming_messages(tmp_path: Path) -> None:
    """An `incoming` frame is yielded as an IncomingMessage."""

    async def scenario(bus: SocketBus, path: Path) -> None:
        _, writer = await _connect(path, {"kind": "incoming", "data": INCOMING})
        assert await _next(bus.incoming_messages()) == IncomingMessage.from_dict(INCOMING)
        writer.close()

    _with_bus(tmp_path, scenario)


def test_hook_frame_reaches_hook_events(tmp_path: Path) -> None:
    """A `hook` frame's data is yielded by hook_events()."""

    async def scenario(bus: SocketBus, path: Path) -> None:
        event = {"hook_event_name": "Stop", "live_session": "rc-app"}
        _, writer = await _connect(path, {"kind": "hook", "data": event})
        assert await _next(bus.hook_events()) == event
        writer.close()

    _with_bus(tmp_path, scenario)


def test_outgoing_goes_only_to_bridges(tmp_path: Path) -> None:
    """Only a connection that said hello as a bridge gets `outgoing` frames."""

    async def scenario(bus: SocketBus, path: Path) -> None:
        bridge_reader, bridge = await _connect(path, HELLO)
        other_reader, other = await _connect(path)
        await _sync(bus, path)

        message = OutgoingMessage(text="reply", in_reply_to="m1")
        await bus.publish_outgoing(message)

        line = await asyncio.wait_for(bridge_reader.readline(), 2)
        assert json.loads(line) == {"kind": "outgoing", "data": message.to_dict()}
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(other_reader.readline(), 0.2)
        bridge.close()
        other.close()

    _with_bus(tmp_path, scenario)


def test_outgoing_without_bridge_is_dropped(tmp_path: Path) -> None:
    """With no bridge connected, publish_outgoing returns at once without error."""

    async def scenario(bus: SocketBus, path: Path) -> None:
        await asyncio.wait_for(bus.publish_outgoing(OutgoingMessage(text="nobody")), 1)

    _with_bus(tmp_path, scenario)


# Large enough that one frame fills the kernel buffer and the transport's
# write buffer, so a client that never reads stalls the sender.
BIG = OutgoingMessage(text="x" * 1_000_000)


async def _read_to_eof(reader: asyncio.StreamReader) -> None:
    """Read until EOF, failing the test if the server never disconnects."""
    while await asyncio.wait_for(reader.read(1024 * 1024), 5):
        pass


def test_a_stalled_bridge_is_dropped_without_blocking_the_others(
    tmp_path: Path, monkeypatch: "MonkeyPatch"
) -> None:
    """A bridge that never reads holds up neither publish_outgoing nor the
    bridges that do read; once its queue is full it is disconnected, and
    close() still returns."""
    monkeypatch.setattr(socket_bus, "MAX_PENDING_FRAMES", 5)

    async def scenario(bus: SocketBus, path: Path) -> None:
        # Keep the writers referenced: a collected StreamWriter closes its connection.
        stalled_reader, stalled = await _connect(path, HELLO)
        live_reader, live = await _connect(path, HELLO)
        await _sync(bus, path)

        for _ in range(30):
            await asyncio.wait_for(bus.publish_outgoing(BIG), 1)
            line = await asyncio.wait_for(live_reader.readline(), 5)
            assert json.loads(line)["data"] == BIG.to_dict()
        # The dropped bridge has read nothing. close() must not wait for it.
        await asyncio.wait_for(bus.close(), 5)
        await _read_to_eof(stalled_reader)
        stalled.close()
        live.close()

    _with_bus(tmp_path, scenario)


def test_close_does_not_wait_for_a_client_with_unsent_data(tmp_path: Path) -> None:
    """close() disconnects a bridge that has unread frames instead of waiting
    for it to read them."""

    async def scenario(bus: SocketBus, path: Path) -> None:
        reader, writer = await _connect(path, HELLO)
        await _sync(bus, path)
        for _ in range(20):
            await bus.publish_outgoing(BIG)
            await asyncio.sleep(0.01)
        await asyncio.wait_for(bus.close(), 5)
        await _read_to_eof(reader)
        writer.close()

    _with_bus(tmp_path, scenario)


def test_malformed_line_is_skipped(tmp_path: Path) -> None:
    """Non-JSON lines and malformed frames are skipped; the next valid frame still arrives."""

    async def scenario(bus: SocketBus, path: Path) -> None:
        _, writer = await asyncio.open_unix_connection(str(path))
        writer.write(b"not json\n")
        writer.write(_line(["not", "an", "object"]))
        writer.write(_line({"kind": "incoming", "data": {"missing": "fields"}}))
        writer.write(_line({"kind": "unknown", "data": {}}))
        writer.write(_line({"kind": "incoming", "data": INCOMING}))
        await writer.drain()
        assert await _next(bus.incoming_messages()) == IncomingMessage.from_dict(INCOMING)
        writer.close()

    _with_bus(tmp_path, scenario)


def test_a_client_disconnecting_mid_line_does_not_stop_the_server(tmp_path: Path) -> None:
    """A connection that closes mid-line is dropped and the server keeps serving."""

    async def scenario(bus: SocketBus, path: Path) -> None:
        _, cut = await asyncio.open_unix_connection(str(path))
        cut.write(b'{"kind": "incoming", "da')
        await cut.drain()
        cut.close()
        _, writer = await _connect(path, {"kind": "incoming", "data": INCOMING})
        assert await _next(bus.incoming_messages()) == IncomingMessage.from_dict(INCOMING)
        writer.close()

    _with_bus(tmp_path, scenario)


def test_socket_mode_is_0600(tmp_path: Path) -> None:
    """The socket file is readable and writable by its owner only."""

    async def scenario(bus: SocketBus, path: Path) -> None:
        assert stat.S_ISSOCK(path.stat().st_mode)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    _with_bus(tmp_path, scenario)


def test_close_removes_the_socket_file(tmp_path: Path) -> None:
    """close() removes the socket file the bus created."""
    _with_bus(tmp_path, lambda bus, path: asyncio.sleep(0))
    assert not (tmp_path / "bus.sock").exists()


def test_close_leaves_a_socket_file_it_did_not_create(tmp_path: Path) -> None:
    """close() leaves a socket file another process put at the path."""

    async def scenario(bus: SocketBus, path: Path) -> None:
        path.unlink()
        other = socket.socket(socket.AF_UNIX)
        other.bind(str(path))
        other.close()

    _with_bus(tmp_path, scenario)
    assert (tmp_path / "bus.sock").exists()


def test_stale_socket_file_is_replaced(tmp_path: Path) -> None:
    """A leftover socket file nobody listens on is replaced."""
    path = tmp_path / "bus.sock"
    stale = socket.socket(socket.AF_UNIX)
    stale.bind(str(path))
    stale.close()

    async def scenario(bus: SocketBus, path: Path) -> None:
        _, writer = await _connect(path, {"kind": "incoming", "data": INCOMING})
        assert await _next(bus.incoming_messages()) == IncomingMessage.from_dict(INCOMING)
        writer.close()

    _with_bus(tmp_path, scenario)


def test_live_socket_raises_socket_in_use(tmp_path: Path) -> None:
    """A second bus on a live socket raises SocketInUse and leaves the socket working."""

    async def scenario(bus: SocketBus, path: Path) -> None:
        identity = os.stat(path).st_ino
        with pytest.raises(SocketInUse):
            await SocketBus(path).start()
        assert os.stat(path).st_ino == identity
        _, writer = await _connect(path, {"kind": "incoming", "data": INCOMING})
        assert await _next(bus.incoming_messages()) == IncomingMessage.from_dict(INCOMING)
        writer.close()

    _with_bus(tmp_path, scenario)


def test_a_second_orchestrator_exits_78_and_keeps_the_live_socket(
    tmp_path: Path, caplog: "LogCaptureFixture"
) -> None:
    """A second orchestrator exits 78 with a clear message and keeps the live socket."""

    async def scenario(bus: SocketBus, path: Path) -> None:
        identity = os.stat(path).st_ino
        with pytest.raises(SystemExit) as exited:
            await start_bus(path)
        assert exited.value.code == 78
        assert f"another orchestrator is listening on {path}" in caplog.text
        assert os.stat(path).st_ino == identity

    _with_bus(tmp_path, scenario)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_a_socket_path_without_write_permission_exits_78(
    tmp_path: Path, caplog: "LogCaptureFixture"
) -> None:
    """A socket directory the user cannot write to exits 78 with a clear message."""
    locked = tmp_path / "locked"
    locked.mkdir(mode=0o500)
    path = locked / "bus.sock"
    try:
        with pytest.raises(SystemExit) as exited:
            asyncio.run(start_bus(path))
    finally:
        locked.chmod(0o700)
    assert exited.value.code == 78
    assert f"no permission to create the socket at {path}" in caplog.text


def test_a_regular_file_at_the_socket_path_is_not_deleted(tmp_path: Path) -> None:
    """A regular file at the socket path is reported, never deleted."""
    path = tmp_path / "bus.sock"
    path.write_text("keep me")
    with pytest.raises(FileExistsError):
        asyncio.run(SocketBus(path).start())
    assert path.read_text() == "keep me"


def test_default_socket_path_uses_xdg_runtime_dir(monkeypatch: "MonkeyPatch") -> None:
    """The default socket lives in $XDG_RUNTIME_DIR when it is set."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1234")
    assert default_socket_path() == Path("/run/user/1234/wasap-control.sock")


def test_default_socket_path_falls_back_to_tmpdir(monkeypatch: "MonkeyPatch") -> None:
    """Without $XDG_RUNTIME_DIR the socket goes to $TMPDIR, named by uid."""
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setenv("TMPDIR", "/var/tmp")
    assert default_socket_path() == Path(f"/var/tmp/wasap-control-{os.getuid()}.sock")


def test_default_socket_path_falls_back_to_tmp(monkeypatch: "MonkeyPatch") -> None:
    """Without $XDG_RUNTIME_DIR and $TMPDIR the socket goes to /tmp, named by uid."""
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("TMPDIR", raising=False)
    assert default_socket_path() == Path(f"/tmp/wasap-control-{os.getuid()}.sock")
