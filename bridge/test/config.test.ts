import assert from 'node:assert/strict';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { describe, it } from 'node:test';
import { ConfigError, EXIT_CONFIG, exitCodeFor, loadConfig } from '../src/config.js';

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

  it('treats an empty socket_path as unset', () => {
    const c = load('whatsapp:\n  allowed_sender_jid: a@b\ntransport:\n  socket_path: ""\n');
    assert.match(c.socketPath, /claude-orchestrator.*\.sock$/);
  });
});

describe('loadConfig errors', () => {
  it('throws ConfigError for a missing file', () => {
    assert.throws(
      () => loadConfig(path.join(os.tmpdir(), 'no-such-dir', 'config.yaml')),
      (err: unknown) => err instanceof ConfigError && /config file not found/.test(err.message),
    );
  });

  it('throws ConfigError for a missing allowed_sender_jid', () => {
    assert.throws(() => load('bridge:\n  auth_dir: /tmp/a\n'), ConfigError);
  });

  it('throws ConfigError for invalid YAML', () => {
    assert.throws(() => load('whatsapp: [unclosed\n'), ConfigError);
  });

  it('throws ConfigError for a value of the wrong type', () => {
    assert.throws(() => load('whatsapp:\n  allowed_sender_jid: a@b\nbridge:\n  auth_dir: 5\n'), ConfigError);
  });

  it('maps a ConfigError to exit code 78 and other errors to 1', () => {
    assert.equal(EXIT_CONFIG, 78);
    assert.equal(exitCodeFor(new ConfigError('bad')), 78);
    assert.equal(exitCodeFor(new Error('boom')), 1);
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
