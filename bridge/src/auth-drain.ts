// Tracks in-flight WhatsApp auth state writes so the process can wait for
// all of them before it exits.

import type { SignalKeyStore } from '@whiskeysockets/baileys';

export const DRAIN_TIMEOUT_MS = 10_000;
const DRAIN_QUIET_MS = 250;

export class DrainTimeoutError extends Error {
  constructor(timeoutMs: number) {
    super(`auth state writes did not finish within ${timeoutMs / 1000}s; exiting without waiting further`);
    this.name = 'DrainTimeoutError';
  }
}

export interface DrainOptions {
  /** Give up and reject after this long. */
  timeoutMs?: number;
  /** After everything settles, wait this long and finish only if nothing new started. */
  quietMs?: number;
}

type ErrorHandler = (err: unknown, kind: 'creds' | 'keys') => void;

export class AuthWriteTracker {
  private credsChain: Promise<void> = Promise.resolve();
  private readonly inFlight = new Set<Promise<unknown>>();
  private readonly keyFailures: unknown[] = [];

  constructor(private readonly onError: ErrorHandler = () => undefined) {}

  /**
   * Queues a credentials save after the previous one. Each save writes the
   * whole credentials object, so only the outcome of the latest one counts.
   */
  saveCreds(save: () => Promise<void>): void {
    const next = this.credsChain.catch(() => undefined).then(save);
    this.credsChain = next;
    next.catch((err: unknown) => this.onError(err, 'creds'));
  }

  /** Records a key store operation; a rejected one makes `drain` reject. */
  track<T>(op: Promise<T>): Promise<T> {
    this.inFlight.add(op);
    op.then(
      () => this.inFlight.delete(op),
      (err: unknown) => {
        this.inFlight.delete(op);
        this.keyFailures.push(err);
        this.onError(err, 'keys');
      },
    );
    return op;
  }

  /** Wraps a key store so every `get` and `set` is tracked. */
  wrapKeys(keys: SignalKeyStore): SignalKeyStore {
    return {
      get: (type, ids) => this.track(Promise.resolve(keys.get(type, ids))),
      set: (data) => this.track(Promise.resolve(keys.set(data))),
      clear: keys.clear ? () => this.track(Promise.resolve(keys.clear!())) : undefined,
    };
  }

  /**
   * Resolves once no credentials save and no key operation is in flight,
   * including ones started while draining. Rejects if the latest credentials
   * save or any key write failed, or with `DrainTimeoutError` after
   * `timeoutMs`.
   */
  async drain({ timeoutMs = DRAIN_TIMEOUT_MS, quietMs = DRAIN_QUIET_MS }: DrainOptions = {}): Promise<void> {
    let timer: NodeJS.Timeout | undefined;
    const timeout = new Promise<never>((_, reject) => {
      timer = setTimeout(() => reject(new DrainTimeoutError(timeoutMs)), timeoutMs);
    });
    const settle = async (): Promise<void> => {
      for (;;) {
        const chain = this.credsChain;
        const ops = [...this.inFlight];
        await chain;
        await Promise.allSettled(ops);
        await new Promise((resolve) => setTimeout(resolve, quietMs));
        if (chain === this.credsChain && this.inFlight.size === 0) break;
      }
      if (this.keyFailures.length > 0) {
        throw new AggregateError(this.keyFailures, 'one or more auth key writes failed');
      }
    };
    try {
      await Promise.race([settle(), timeout]);
    } finally {
      clearTimeout(timer);
    }
  }
}
