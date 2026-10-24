import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { afterEach, beforeEach, describe, it } from 'node:test';
import { defaultSocketPath, SocketBus } from '../src/socket.js';
import type { IncomingEvent } from '../src/events.js';

const EVENT: IncomingEvent = {
  message_id: 'm1',
  timestamp: '2026-01-01T00:00:00Z',
  sender_jid: '1@s.whatsapp.net',
  type: 'text',
  text: 'hi',
  media_path: null,
  media_mime_type: null,
  quoted_text: null,
  quoted_media_path: null,
  quoted_media_mime_type: null,
  quoted_media_type_label: null,
};

function waitFor(cond: () => boolean, timeoutMs = 8000): Promise<void> {
  return new Promise((resolve, reject) => {
    const start = Date.now();
    const tick = (): void => {
      if (cond()) return resolve();
      if (Date.now() - start > timeoutMs) return reject(new Error('timeout'));
      setTimeout(tick, 10);
    };
    tick();
  });
}

class FakeServer {
  server: net.Server;
  sockets: net.Socket[] = [];
  lines: string[] = [];
  constructor(private readonly sockPath: string) {
    this.server = net.createServer((s) => {
      this.sockets.push(s);
      let buf = '';
      s.on('data', (d) => {
        buf += d.toString();
        let i;
        while ((i = buf.indexOf('\n')) >= 0) {
          this.lines.push(buf.slice(0, i));
          buf = buf.slice(i + 1);
        }
      });
      s.on('error', () => {});
    });
  }
  listen(): Promise<void> {
    return new Promise((r) => this.server.listen(this.sockPath, r));
  }
  async stop(): Promise<void> {
    for (const s of this.sockets) s.destroy();
    await new Promise((r) => this.server.close(r));
  }
}

describe('SocketBus', () => {
  let dir: string;
  let sockPath: string;
  let server: FakeServer;
  let bus: SocketBus | undefined;

  beforeEach(async () => {
    dir = mkdtempSync(path.join(os.tmpdir(), 'sockbus-'));
    sockPath = path.join(dir, 's.sock');
    server = new FakeServer(sockPath);
    await server.listen();
  });

  afterEach(async () => {
    await bus?.close();
    bus = undefined;
    await server.stop().catch(() => {});
    rmSync(dir, { recursive: true, force: true });
  });

  it('sends hello as bridge on connect', async () => {
    bus = new SocketBus(sockPath);
    await waitFor(() => server.lines.length >= 1);
    assert.deepEqual(JSON.parse(server.lines[0]), { kind: 'hello', data: { role: 'bridge' } });
  });

  it('publishIncoming writes one incoming frame', async () => {
    bus = new SocketBus(sockPath);
    await waitFor(() => server.lines.length >= 1);
    await bus.publishIncoming(EVENT);
    await waitFor(() => server.lines.length >= 2);
    assert.deepEqual(JSON.parse(server.lines[1]), { kind: 'incoming', data: EVENT });
  });

  it('outgoing frame calls the handler', async () => {
    bus = new SocketBus(sockPath);
    const got: unknown[] = [];
    await bus.onOutgoing((e) => {
      got.push(e);
    });
    await waitFor(() => server.sockets.length >= 1);
    const frame = JSON.stringify({ kind: 'outgoing', data: { text: 'yo' } }) + '\n';
    // split mid-line, with a malformed line in between
    server.sockets[0].write('not json\n');
    server.sockets[0].write(frame.slice(0, 10));
    await new Promise((r) => setTimeout(r, 20));
    server.sockets[0].write(frame.slice(10));
    await waitFor(() => got.length >= 1);
    assert.deepEqual(got, [{ text: 'yo' }]);
  });

  it('reconnects after the server restarts', async () => {
    bus = new SocketBus(sockPath);
    await waitFor(() => server.lines.length >= 1);
    await server.stop();
    server = new FakeServer(sockPath);
    await server.listen();
    await waitFor(() => server.lines.length >= 1);
    assert.deepEqual(JSON.parse(server.lines[0]), { kind: 'hello', data: { role: 'bridge' } });
  });

  it('drops events while disconnected', async () => {
    await server.stop();
    bus = new SocketBus(sockPath);
    await bus.publishIncoming(EVENT);
  });
});

describe('defaultSocketPath', () => {
  const saved = { x: process.env.XDG_RUNTIME_DIR, t: process.env.TMPDIR };
  afterEach(() => {
    for (const [k, v] of [['XDG_RUNTIME_DIR', saved.x], ['TMPDIR', saved.t]] as const) {
      if (v === undefined) delete process.env[k];
      else process.env[k] = v;
    }
  });

  it('uses XDG_RUNTIME_DIR, else tmpdir with uid', () => {
    process.env.XDG_RUNTIME_DIR = '/run/user/42';
    assert.equal(defaultSocketPath(), '/run/user/42/claude-orchestrator.sock');
    process.env.XDG_RUNTIME_DIR = '';
    process.env.TMPDIR = '/var/tmpx';
    assert.equal(defaultSocketPath(), `/var/tmpx/claude-orchestrator-${process.getuid!()}.sock`);
    process.env.TMPDIR = '';
    assert.equal(defaultSocketPath(), `/tmp/claude-orchestrator-${process.getuid!()}.sock`);
    delete process.env.XDG_RUNTIME_DIR;
    delete process.env.TMPDIR;
    assert.equal(defaultSocketPath(), `/tmp/claude-orchestrator-${process.getuid!()}.sock`);
  });
});
