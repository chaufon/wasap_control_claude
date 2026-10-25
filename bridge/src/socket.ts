// Unix socket bus between the bridge and the Python orchestrator, which owns
// the socket. Every frame is one JSON object per line:
// `{"kind": "hello" | "incoming" | "outgoing" | "hook", "data": {...}}`.
// The bridge sends `hello` first on every (re)connect, then `incoming`
// frames, and receives `outgoing` frames.

import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import pino from 'pino';
import type { IncomingEvent, OutgoingEvent } from './events.js';

const logger = pino({ level: process.env.LOG_LEVEL ?? 'info' });

const SOCKET_NAME = 'wasap-control.sock';
const RECONNECT_INITIAL_DELAY_MS = 1_000;
const RECONNECT_MAX_DELAY_MS = 5_000;

/** `$XDG_RUNTIME_DIR/wasap-control.sock`, else
 * `${TMPDIR:-/tmp}/wasap-control-<uid>.sock`. */
export function defaultSocketPath(): string {
  const runtimeDir = process.env.XDG_RUNTIME_DIR;
  if (runtimeDir) return path.join(runtimeDir, SOCKET_NAME);
  const tmpDir = process.env.TMPDIR || '/tmp';
  const uid = os.userInfo().uid;
  return path.join(tmpDir, `wasap-control-${uid}.sock`);
}

type OutgoingHandler = (event: OutgoingEvent) => void | Promise<void>;

export class SocketBus {
  private socket: net.Socket | null = null;
  private connected = false;
  private closed = false;
  private delay = RECONNECT_INITIAL_DELAY_MS;
  private timer: NodeJS.Timeout | null = null;
  private readonly handlers: OutgoingHandler[] = [];

  constructor(private readonly socketPath: string) {
    this.connect();
  }

  async publishIncoming(event: IncomingEvent): Promise<void> {
    if (!this.connected || !this.socket) {
      logger.warn('orchestrator socket not connected; dropping incoming event');
      return;
    }
    this.send('incoming', event);
  }

  async onOutgoing(handler: OutgoingHandler): Promise<void> {
    this.handlers.push(handler);
  }

  async close(): Promise<void> {
    this.closed = true;
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    const socket = this.socket;
    this.socket = null;
    this.connected = false;
    if (socket && !socket.destroyed) {
      await new Promise<void>((resolve) => {
        socket.once('close', () => resolve());
        socket.destroy();
      });
    }
  }

  private send(kind: string, data: unknown): void {
    this.socket?.write(JSON.stringify({ kind, data }) + '\n');
  }

  private connect(): void {
    if (this.closed) return;
    const socket = net.createConnection(this.socketPath);
    this.socket = socket;
    let buffer = '';

    socket.on('connect', () => {
      this.connected = true;
      this.delay = RECONNECT_INITIAL_DELAY_MS;
      this.send('hello', { role: 'bridge' });
      logger.info({ path: this.socketPath }, 'connected to orchestrator socket');
    });

    socket.on('data', (chunk: Buffer) => {
      buffer += chunk.toString('utf-8');
      let index: number;
      while ((index = buffer.indexOf('\n')) >= 0) {
        const line = buffer.slice(0, index);
        buffer = buffer.slice(index + 1);
        if (line.trim()) this.handleLine(line);
      }
    });

    let handled = false;
    const onDown = (err?: Error): void => {
      if (handled) return;
      handled = true;
      if (this.socket === socket) this.socket = null;
      const wasConnected = this.connected;
      this.connected = false;
      socket.destroy();
      if (this.closed) return;
      if (wasConnected || err) {
        logger.warn({ err: err?.message, path: this.socketPath }, 'orchestrator socket down; reconnecting');
      }
      const wait = this.delay;
      this.delay = Math.min(this.delay * 2, RECONNECT_MAX_DELAY_MS);
      this.timer = setTimeout(() => {
        this.timer = null;
        this.connect();
      }, wait);
    };
    socket.on('error', (err) => onDown(err));
    socket.on('close', () => onDown());
  }

  private handleLine(line: string): void {
    let frame: { kind?: unknown; data?: unknown };
    try {
      frame = JSON.parse(line);
    } catch {
      logger.warn('skipping malformed frame from orchestrator');
      return;
    }
    if (!frame || typeof frame !== 'object' || frame.kind !== 'outgoing') return;
    if (!frame.data || typeof frame.data !== 'object') {
      logger.warn('skipping outgoing frame without data');
      return;
    }
    for (const handler of this.handlers) {
      Promise.resolve()
        .then(() => handler(frame.data as OutgoingEvent))
        .catch((err: unknown) => logger.error({ err }, 'outgoing handler failed'));
    }
  }
}
