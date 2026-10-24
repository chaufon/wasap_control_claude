"""Unix socket bus between the orchestrator, the WhatsApp bridge and the
live sessions' hook relay.

The orchestrator owns the socket. Every frame is one JSON object per line:
`{"kind": "hello" | "incoming" | "outgoing" | "hook", "data": {...}}`.

- A bridge connects and sends `{"kind": "hello", "data": {"role": "bridge"}}`
  first. It then sends `incoming` frames (`data` is an IncomingMessage dict)
  and receives `outgoing` frames (`data` is an OutgoingMessage dict).
- The hook relay (hook_relay.py) connects, sends one `hook` frame and
  disconnects.

`outgoing` frames go to every connection that said hello as a bridge. With
no bridge connected they are dropped. Malformed lines are logged and skipped.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import stat
from collections.abc import AsyncIterator
from pathlib import Path

from .models import IncomingMessage, OutgoingMessage

logger = logging.getLogger(__name__)

SOCKET_NAME = "claude-orchestrator.sock"

# Longest accepted line. Incoming messages carry text, never media bytes.
MAX_LINE_BYTES = 16 * 1024 * 1024

# Frames queued for one bridge before it counts as stalled and is dropped.
MAX_PENDING_FRAMES = 1000


def default_socket_path() -> Path:
    """`$XDG_RUNTIME_DIR/claude-orchestrator.sock`, else
    `${TMPDIR:-/tmp}/claude-orchestrator-<uid>.sock`."""
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if runtime_dir:
        return Path(runtime_dir) / SOCKET_NAME
    tmp_dir = os.environ.get("TMPDIR") or "/tmp"
    return Path(tmp_dir) / f"claude-orchestrator-{os.getuid()}.sock"


class SocketInUse(Exception):
    """Another process is listening on the socket path."""

    def __init__(self, path: Path) -> None:
        super().__init__(f"another orchestrator is listening on {path}")
        self.path = path


class _Connection:
    """One client connection. Frames for it go through a bounded queue and a
    writer task, so a slow reader never blocks the bus."""

    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self.writer = writer
        self.is_bridge = False
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(MAX_PENDING_FRAMES)
        self._sender: asyncio.Task[None] | None = None

    def send(self, line: bytes) -> bool:
        """Queue a line; False when the connection is stalled."""
        if self._sender is None:
            self._sender = asyncio.create_task(self._send_loop())
        try:
            self._queue.put_nowait(line)
        except asyncio.QueueFull:
            return False
        return True

    async def _send_loop(self) -> None:
        try:
            while True:
                line = await self._queue.get()
                self.writer.write(line)
                await self.writer.drain()
        except (ConnectionError, OSError) as exc:
            logger.info("bridge connection lost while sending: %s", exc)
            self.close()

    def close(self) -> None:
        """Drop the connection at once, discarding unsent data. The peer
        sees EOF, and the read loop in SocketBus._handle ends."""
        if self._sender is not None and self._sender is not asyncio.current_task():
            self._sender.cancel()
        self.writer.transport.abort()


class SocketBus:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._server: asyncio.AbstractServer | None = None
        # (st_dev, st_ino) of the socket file this bus created.
        self._identity: tuple[int, int] | None = None
        self._connections: set[_Connection] = set()
        self._handlers: set[asyncio.Task[None]] = set()
        self._closing = False
        self._incoming: asyncio.Queue[IncomingMessage] = asyncio.Queue()
        self._hooks: asyncio.Queue[dict] = asyncio.Queue()

    @property
    def path(self) -> Path:
        return self._path

    async def start(self) -> None:
        """Listen on the socket path, replacing a stale socket file.

        Raises SocketInUse when another process listens on the path, and
        FileExistsError when the path holds something that is not a socket.
        """
        await self._clear_stale_socket()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._server = await asyncio.start_unix_server(self._handle, path=str(self._path), limit=MAX_LINE_BYTES)
        os.chmod(self._path, 0o600)
        info = os.stat(self._path)
        self._identity = (info.st_dev, info.st_ino)
        logger.info("listening on %s", self._path)

    async def _clear_stale_socket(self) -> None:
        try:
            info = os.lstat(self._path)
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(info.st_mode):
            raise FileExistsError(f"{self._path} exists and is not a socket")
        try:
            _, writer = await asyncio.open_unix_connection(str(self._path))
        except (ConnectionRefusedError, FileNotFoundError):
            logger.info("replacing stale socket %s", self._path)
            with contextlib.suppress(FileNotFoundError):
                self._path.unlink()
            return
        writer.close()
        raise SocketInUse(self._path)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._closing:
            writer.close()
            return
        connection = _Connection(writer)
        self._connections.add(connection)
        task = asyncio.current_task()
        if task is not None:
            self._handlers.add(task)
        try:
            while True:
                try:
                    line = await reader.readline()
                except ValueError:
                    logger.warning("closing a connection that sent a line over %d bytes", MAX_LINE_BYTES)
                    return
                if not line:
                    return
                if not line.endswith(b"\n"):
                    logger.warning("dropping a partial line from a closed connection")
                    return
                self._dispatch(connection, line)
        except (ConnectionError, OSError) as exc:
            logger.info("connection lost: %s", exc)
        finally:
            self._connections.discard(connection)
            if task is not None:
                self._handlers.discard(task)
            connection.close()

    def _dispatch(self, connection: _Connection, line: bytes) -> None:
        try:
            frame = json.loads(line)
        except ValueError:
            logger.warning("dropping malformed line: %r", line[:200])
            return
        if not isinstance(frame, dict) or not isinstance(frame.get("data"), dict):
            logger.warning("dropping frame without a data object: %r", line[:200])
            return
        kind, data = frame.get("kind"), frame["data"]
        if kind == "hello":
            connection.is_bridge = data.get("role") == "bridge"
        elif kind == "incoming":
            try:
                self._incoming.put_nowait(IncomingMessage.from_dict(data))
            except KeyError:
                logger.warning("dropping malformed incoming message: %r", data)
        elif kind == "hook":
            self._hooks.put_nowait(data)
        else:
            logger.warning("dropping frame of unknown kind %r", kind)

    async def publish_outgoing(self, message: OutgoingMessage) -> None:
        """Send the message to every bridge; dropped when none is connected."""
        line = json.dumps({"kind": "outgoing", "data": message.to_dict()}).encode() + b"\n"
        bridges = [c for c in self._connections if c.is_bridge]
        if not bridges:
            logger.warning("no bridge connected; dropping outgoing message")
            return
        for bridge in bridges:
            if not bridge.send(line):
                logger.warning("bridge is not reading; closing its connection")
                self._connections.discard(bridge)
                bridge.close()

    async def incoming_messages(self) -> AsyncIterator[IncomingMessage]:
        while True:
            yield await self._incoming.get()

    async def hook_events(self) -> AsyncIterator[dict]:
        """Hook events from the live sessions (hook_relay.py)."""
        while True:
            yield await self._hooks.get()

    async def close(self) -> None:
        """Stop listening, drop every connection, and remove the socket file
        if it is still the one this bus created."""
        self._closing = True
        if self._server is not None:
            # One loop pass lets a connection accepted just before close get
            # its transport; its handler sees _closing and closes it.
            await asyncio.sleep(0)
            self._server.close()
        for connection in list(self._connections):
            connection.close()
        for task in list(self._handlers):
            task.cancel()
        if self._server is not None:
            await self._server.wait_closed()
            self._server = None
        if self._identity is not None:
            with contextlib.suppress(FileNotFoundError):
                info = os.stat(self._path)
                if (info.st_dev, info.st_ino) == self._identity:
                    self._path.unlink()
            self._identity = None
