import assert from 'node:assert/strict';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { describe, it } from 'node:test';
import { loadConfig } from '../src/config.js';

function load(yaml: string, options?: { requireSender: boolean }) {
  const dir = mkdtempSync(path.join(os.tmpdir(), 'cfg-'));
  try {
    const p = path.join(dir, 'config.yaml');
    writeFileSync(p, yaml);
    return loadConfig(p, options);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

describe('loadConfig socketPath', () => {
  it('reads transport.socket_path and expands ~', () => {
    const c = load('whatsapp:\n  allowed_sender_jid: a@b\ntransport:\n  socket_path: ~/x.sock\n');
    assert.equal(c.socketPath, path.join(os.homedir(), 'x.sock'));
  });

  it('ignores a leftover redis section', () => {
    const c = load('whatsapp:\n  allowed_sender_jid: a@b\nredis:\n  host: h\n  port: 1\n');
    assert.match(c.socketPath, /claude-orchestrator.*\.sock$/);
  });
});

describe('loadConfig requireSender', () => {
  it('without allowed_sender_jid and requireSender false returns a config', () => {
    const c = load('bridge:\n  auth_dir: /tmp/a\n', { requireSender: false });
    assert.equal(c.allowedSenderJid, '');
    assert.equal(c.authDir, '/tmp/a');
  });

  it('treats the placeholder JID as unset when requireSender is false', () => {
    const c = load('whatsapp:\n  allowed_sender_jid: "REPLACE_ME@s.whatsapp.net"\n', {
      requireSender: false,
    });
    assert.equal(c.allowedSenderJid, '');
  });

  it('without allowed_sender_jid and default options throws', () => {
    assert.throws(() => load('bridge:\n  auth_dir: /tmp/a\n'), /allowed_sender_jid is required/);
  });
});
