// WhatsApp auth state stored as one JSON file per entry in a single folder,
// with the same file layout and naming as Baileys' `useMultiFileAuthState`
// (`creds.json`, `<category>-<id>.json`, `/` → `__`, `:` → `-`).
//
// Every write goes to `<file>.tmp-<pid>-<n>`, is fsynced, and is renamed
// over the target, so a crash at any point leaves either the old or the new
// file, never an empty or partial one. Operations on one file run one at a
// time, in call order.

import { mkdir, open, readdir, readFile, rename, stat, unlink } from 'node:fs/promises';
import path from 'node:path';
import {
  type AuthenticationCreds,
  type AuthenticationState,
  BufferJSON,
  initAuthCreds,
  proto,
  type SignalDataSet,
  type SignalDataTypeMap,
} from '@whiskeysockets/baileys';

const TMP_MARKER = '.tmp-';
const TMP_PATTERN = /\.tmp-(\d+)-\d+$/;

let tmpCounter = 0;

const fileQueues = new Map<string, Promise<unknown>>();

/** Runs `op` after every earlier operation queued for `filePath` has settled. */
function withFileLock<T>(filePath: string, op: () => Promise<T>): Promise<T> {
  const previous = fileQueues.get(filePath) ?? Promise.resolve();
  const result = previous.then(op, op);
  const tail = result.catch(() => undefined);
  fileQueues.set(filePath, tail);
  void tail.then(() => {
    if (fileQueues.get(filePath) === tail) fileQueues.delete(filePath);
  });
  return result;
}

async function fsyncDir(dir: string): Promise<void> {
  let handle;
  try {
    handle = await open(dir, 'r');
    await handle.sync();
  } catch {
    // Some platforms and file systems cannot fsync a directory; the rename
    // itself is still atomic there.
  } finally {
    await handle?.close().catch(() => undefined);
  }
}

/** Replaces `filePath` with `contents` atomically (temp file, fsync, rename). */
export async function writeFileAtomic(filePath: string, contents: string): Promise<void> {
  tmpCounter += 1;
  const tmpPath = `${filePath}${TMP_MARKER}${process.pid}-${tmpCounter}`;
  const handle = await open(tmpPath, 'w', 0o600);
  try {
    try {
      await handle.writeFile(contents, 'utf-8');
      await handle.sync();
    } finally {
      await handle.close();
    }
    await rename(tmpPath, filePath);
  } catch (err) {
    await unlink(tmpPath).catch(() => undefined);
    throw err;
  }
  await fsyncDir(path.dirname(filePath));
}

function processIsAlive(pid: number): boolean {
  if (pid === process.pid) return true;
  try {
    process.kill(pid, 0);
    return true;
  } catch (err) {
    return (err as NodeJS.ErrnoException).code === 'EPERM';
  }
}

/** Deletes temp files whose writer process is not running. */
async function removeStaleTmpFiles(folder: string): Promise<void> {
  for (const name of await readdir(folder)) {
    const match = TMP_PATTERN.exec(name);
    if (match && !processIsAlive(Number(match[1]))) {
      await unlink(path.join(folder, name)).catch(() => undefined);
    }
  }
}

export function authFileName(file: string): string {
  return file.replace(/\//g, '__').replace(/:/g, '-');
}

/**
 * Drop-in replacement for Baileys' `useMultiFileAuthState` that reads and
 * writes the same folder format with atomic writes.
 */
export async function useAtomicMultiFileAuthState(
  folder: string,
): Promise<{ state: AuthenticationState; saveCreds: () => Promise<void> }> {
  const folderInfo = await stat(folder).catch(() => undefined);
  if (folderInfo && !folderInfo.isDirectory()) {
    throw new Error(
      `found something that is not a directory at ${folder}, either delete it or specify a different location`,
    );
  }
  if (!folderInfo) await mkdir(folder, { recursive: true });
  await removeStaleTmpFiles(folder);

  const filePathOf = (file: string): string => path.join(folder, authFileName(file));

  const writeData = (data: unknown, file: string): Promise<void> => {
    const filePath = filePathOf(file);
    return withFileLock(filePath, () =>
      writeFileAtomic(filePath, JSON.stringify(data, BufferJSON.replacer)),
    );
  };

  // A missing or unreadable file reads as null, as in Baileys.
  const readData = async (file: string): Promise<any> => {
    const filePath = filePathOf(file);
    try {
      return await withFileLock(filePath, async () =>
        JSON.parse(await readFile(filePath, { encoding: 'utf-8' }), BufferJSON.reviver),
      );
    } catch {
      return null;
    }
  };

  const removeData = (file: string): Promise<void> => {
    const filePath = filePathOf(file);
    return withFileLock(filePath, () => unlink(filePath).catch(() => undefined));
  };

  const creds: AuthenticationCreds = (await readData('creds.json')) || initAuthCreds();

  return {
    state: {
      creds,
      keys: {
        get: async <T extends keyof SignalDataTypeMap>(type: T, ids: string[]) => {
          const data: { [id: string]: SignalDataTypeMap[T] } = {};
          await Promise.all(
            ids.map(async (id) => {
              let value = await readData(`${type}-${id}.json`);
              if (type === 'app-state-sync-key' && value) {
                value = proto.Message.AppStateSyncKeyData.fromObject(value);
              }
              data[id] = value;
            }),
          );
          return data;
        },
        set: async (data: SignalDataSet) => {
          const tasks: Promise<void>[] = [];
          for (const category in data) {
            const entries = data[category as keyof SignalDataSet] ?? {};
            for (const id in entries) {
              const value = entries[id];
              const file = `${category}-${id}.json`;
              tasks.push(value ? writeData(value, file) : removeData(file));
            }
          }
          await Promise.all(tasks);
        },
      },
    },
    saveCreds: () => writeData(creds, 'creds.json'),
  };
}
