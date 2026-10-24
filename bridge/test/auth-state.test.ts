import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdtemp, readdir, readFile, rm, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { afterEach, beforeEach, describe, it } from 'node:test';
import { BufferJSON } from '@whiskeysockets/baileys';
import { useAtomicMultiFileAuthState, writeFileAtomic } from '../src/auth-state.js';

const here = path.dirname(fileURLToPath(import.meta.url));

let dir: string;

beforeEach(async () => {
  dir = await mkdtemp(path.join(os.tmpdir(), 'auth-state-'));
});

afterEach(async () => {
  await rm(dir, { recursive: true, force: true });
});

async function tmpFiles(): Promise<string[]> {
  return (await readdir(dir)).filter((name) => name.includes('.tmp-'));
}

describe('useAtomicMultiFileAuthState', () => {
  it('round-trips creds and keys, including Buffers', async () => {
    const first = await useAtomicMultiFileAuthState(dir);
    first.state.creds.me = { id: '51999@s.whatsapp.net', name: 'me' };
    await first.saveCreds();
    const preKey = { public: Buffer.from([1, 2, 3]), private: Buffer.from([4, 5, 6]) };
    const session = Buffer.from('session-bytes');
    await first.state.keys.set({
      'pre-key': { '7': preKey },
      session: { '51999.0': session as never },
    });

    const second = await useAtomicMultiFileAuthState(dir);
    // JSON drops the fields initAuthCreds sets to undefined.
    const defined = Object.fromEntries(
      Object.entries(first.state.creds).filter(([, value]) => value !== undefined),
    );
    assert.deepEqual(second.state.creds, defined);
    assert.ok(Buffer.isBuffer(second.state.creds.noiseKey.private));
    const keys = await second.state.keys.get('pre-key', ['7']);
    assert.deepEqual(keys['7'], preKey);
    assert.ok(Buffer.isBuffer(keys['7'].public));
    const sessions = await second.state.keys.get('session', ['51999.0']);
    assert.deepEqual(sessions['51999.0'], session);
  });

  it('removes a key file when set to null', async () => {
    const { state } = await useAtomicMultiFileAuthState(dir);
    await state.keys.set({ 'pre-key': { '1': { public: Buffer.from([1]), private: Buffer.from([2]) } } });
    await state.keys.set({ 'pre-key': { '1': null } });
    assert.deepEqual(await readdir(dir), []);
  });

  it("uses Baileys' file names", async () => {
    const { state, saveCreds } = await useAtomicMultiFileAuthState(dir);
    await saveCreds();
    await state.keys.set({
      'device-list': { '51999': ['0'] },
      'lid-mapping': { 'a/b:c': 'x' as never },
    });
    assert.deepEqual((await readdir(dir)).sort(), [
      'creds.json',
      'device-list-51999.json',
      'lid-mapping-a__b-c.json',
    ]);
  });

  it('reads an auth folder written by the Baileys format', async () => {
    const creds = { registered: true, buf: Buffer.from([9, 8]) };
    await writeFile(path.join(dir, 'creds.json'), JSON.stringify(creds, BufferJSON.replacer));
    const { state } = await useAtomicMultiFileAuthState(dir);
    assert.deepEqual(state.creds, creds);
  });

  it('leaves no temp files after writing', async () => {
    const { state, saveCreds } = await useAtomicMultiFileAuthState(dir);
    await saveCreds();
    await state.keys.set({ 'pre-key': { '1': { public: Buffer.from([1]), private: Buffer.from([2]) } } });
    assert.deepEqual(await tmpFiles(), []);
  });

  it('ignores and removes a temp file left by a dead writer', async () => {
    const first = await useAtomicMultiFileAuthState(dir);
    first.state.creds.registered = true;
    await first.saveCreds();
    // 2^22 is above the default pid_max, so no process has this pid.
    const stray = path.join(dir, `creds.json.tmp-${2 ** 22 + 1}-1`);
    await writeFile(stray, '{"registered": fal');

    const second = await useAtomicMultiFileAuthState(dir);
    assert.equal(second.state.creds.registered, true);
    assert.deepEqual(await tmpFiles(), []);
  });

  it('serializes concurrent writes to one file; the last one wins', async () => {
    const { state, saveCreds } = await useAtomicMultiFileAuthState(dir);
    const writes: Promise<void>[] = [];
    for (let i = 0; i < 20; i += 1) {
      writes.push(state.keys.set({ session: { s: Buffer.from(String(i)) as never } }));
      state.creds.accountSyncCounter = i;
      writes.push(saveCreds());
    }
    await Promise.all(writes);
    const onDisk = JSON.parse(await readFile(path.join(dir, 'creds.json'), 'utf-8'), BufferJSON.reviver);
    assert.equal(onDisk.accountSyncCounter, 19);
    const sessions = await state.keys.get('session', ['s']);
    assert.deepEqual(sessions.s, Buffer.from('19'));
    assert.deepEqual(await tmpFiles(), []);
  });
});

describe('writeFileAtomic', () => {
  it('never leaves the target empty or partial when the writer is killed mid-write', async () => {
    const target = path.join(dir, 'creds.json');
    await writeFile(target, JSON.stringify({ n: 0 }));
    const writer = path.join(here, 'fixtures', 'atomic-writer.ts');

    for (let round = 0; round < 5; round += 1) {
      const child = spawn(process.execPath, ['--import', 'tsx', writer, target], {
        cwd: path.join(here, '..'),
        stdio: ['ignore', 'pipe', 'inherit'],
      });
      await new Promise<void>((resolve) => child.stdout.once('data', () => resolve()));
      await new Promise((resolve) => setTimeout(resolve, 20 + round * 37));
      const exited = new Promise((resolve) => child.once('exit', resolve));
      child.kill('SIGKILL');
      await exited;

      const parsed = JSON.parse(await readFile(target, 'utf-8'));
      assert.equal(typeof parsed.n, 'number');
    }
  });
});
