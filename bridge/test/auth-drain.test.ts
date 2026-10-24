import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { describe, it } from 'node:test';
import { AuthWriteTracker, DrainTimeoutError } from '../src/auth-drain.js';

function deferred() {
  let resolve!: () => void;
  let reject!: (err: unknown) => void;
  const promise = new Promise<void>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

const FAST = { quietMs: 5, timeoutMs: 2_000 };

describe('AuthWriteTracker.drain', () => {
  it('awaits creds.update saves emitted while draining', async () => {
    const tracker = new AuthWriteTracker();
    const ev = new EventEmitter();
    const saved: number[] = [];
    let n = 0;
    ev.on('creds.update', () =>
      tracker.saveCreds(async () => {
        n += 1;
        const id = n;
        await new Promise((resolve) => setTimeout(resolve, 10));
        // The first save triggers another update, as Baileys does after open.
        if (id === 1) ev.emit('creds.update');
        saved.push(id);
      }),
    );

    ev.emit('creds.update');
    await tracker.drain(FAST);
    assert.deepEqual(saved, [1, 2]);
  });

  it('awaits creds.update events emitted during the quiet window', async () => {
    const tracker = new AuthWriteTracker();
    const ev = new EventEmitter();
    let saved = 0;
    ev.on('creds.update', () =>
      tracker.saveCreds(async () => {
        await new Promise((resolve) => setTimeout(resolve, 5));
        saved += 1;
      }),
    );
    setTimeout(() => ev.emit('creds.update'), 2);
    await tracker.drain({ quietMs: 30, timeoutMs: 2_000 });
    assert.equal(saved, 1);
  });

  it('awaits pending key writes', async () => {
    const tracker = new AuthWriteTracker();
    const write = deferred();
    let finished = false;
    tracker.track(write.promise.then(() => {
      finished = true;
    }));
    setTimeout(() => write.resolve(), 20);
    await tracker.drain(FAST);
    assert.equal(finished, true);
  });

  it('awaits key writes made through a wrapped key store', async () => {
    const tracker = new AuthWriteTracker();
    const write = deferred();
    const keys = tracker.wrapKeys({
      get: async () => ({}),
      set: () => write.promise,
    });
    void keys.set({ 'pre-key': {} });
    let drained = false;
    const drain = tracker.drain(FAST).then(() => {
      drained = true;
    });
    await new Promise((resolve) => setTimeout(resolve, 20));
    assert.equal(drained, false);
    write.resolve();
    await drain;
    assert.equal(drained, true);
  });

  it('rejects when the latest creds save fails', async () => {
    const errors: string[] = [];
    const tracker = new AuthWriteTracker((_err, kind) => errors.push(kind));
    tracker.saveCreds(async () => {
      throw new Error('disk full');
    });
    await assert.rejects(tracker.drain(FAST), /disk full/);
    assert.deepEqual(errors, ['creds']);
  });

  it('resolves when a failed creds save is followed by a successful one', async () => {
    const tracker = new AuthWriteTracker();
    tracker.saveCreds(async () => {
      throw new Error('disk full');
    });
    tracker.saveCreds(async () => undefined);
    await tracker.drain(FAST);
  });

  it('rejects when a key write fails', async () => {
    const tracker = new AuthWriteTracker();
    tracker.track(Promise.reject(new Error('EIO'))).catch(() => undefined);
    await assert.rejects(tracker.drain(FAST), (err: unknown) => {
      assert.ok(err instanceof AggregateError);
      assert.match(String((err as AggregateError).errors[0]), /EIO/);
      return true;
    });
  });

  it('rejects with DrainTimeoutError when a write never finishes', async () => {
    const tracker = new AuthWriteTracker();
    const never = deferred();
    tracker.track(never.promise);
    const started = Date.now();
    await assert.rejects(tracker.drain({ quietMs: 5, timeoutMs: 50 }), DrainTimeoutError);
    assert.ok(Date.now() - started < 1_000);
    never.resolve();
  });
});
