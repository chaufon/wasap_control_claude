// WhatsApp connection + self-chat message handling.
//
// Only messages in the owner's own "Message Yourself" chat are ever
// forwarded to the orchestrator — every other chat and group is filtered out here,
// before anything reaches the orchestrator.

import { randomUUID } from 'node:crypto';
import { mkdir, readFile, unlink, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { Boom } from '@hapi/boom';
import makeWASocket, {
  DisconnectReason,
  downloadMediaMessage,
  fetchLatestBaileysVersion,
  getContentType,
  isLidUser,
  jidNormalizedUser,
  type WAMessage,
  type WASocket,
} from '@whiskeysockets/baileys';
import pino from 'pino';
import qrcodeTerminal from 'qrcode-terminal';
import { AuthWriteTracker } from './auth-drain.js';
import { useAtomicMultiFileAuthState } from './auth-state.js';
import type { BridgeConfig } from './config.js';
import type { IncomingEvent } from './events.js';
import type { SocketBus } from './socket.js';

// Pairing mode reserves stdout for the single `PAIRED <jid>` line, so all
// logs go to stderr there.
const logger = process.argv.includes('--pair')
  ? pino({ level: process.env.LOG_LEVEL ?? 'info' }, pino.destination(2))
  : pino({ level: process.env.LOG_LEVEL ?? 'info' });

const MAX_MESSAGE_LENGTH = 4000;

const RECONNECT_INITIAL_DELAY_MS = 5_000;
const RECONNECT_MAX_DELAY_MS = 5 * 60_000;
const RECONNECT_STABLE_AFTER_MS = 60_000;
const RECONNECT_MAX_ATTEMPTS = 10;

// Exponential backoff for reconnect attempts (5s, 10s, 20s, ... capped at
// RECONNECT_MAX_DELAY_MS), so a rejected reconnect doesn't get hammered at a
// fixed rate. A connection has to stay open for RECONNECT_STABLE_AFTER_MS to
// count as recovered and reset the attempt count — a flap that closes again
// sooner doesn't reset it, since that's the exact pattern a strict fixed
// delay would otherwise hammer. Giving up after RECONNECT_MAX_ATTEMPTS stops
// that hammering for good rather than letting it degrade into a slower
// version of the same loop.
class ReconnectBackoff {
  private attempts = 0;
  private stableTimer: NodeJS.Timeout | undefined;

  /** Delay for the next reconnect attempt, or undefined once given up. */
  nextDelayMs(): number | undefined {
    this.attempts += 1;
    if (this.attempts > RECONNECT_MAX_ATTEMPTS) return undefined;
    return Math.min(
      RECONNECT_INITIAL_DELAY_MS * 2 ** (this.attempts - 1),
      RECONNECT_MAX_DELAY_MS,
    );
  }

  onConnectionOpen(): void {
    clearTimeout(this.stableTimer);
    this.stableTimer = setTimeout(() => {
      this.attempts = 0;
    }, RECONNECT_STABLE_AFTER_MS);
  }

  onConnectionClosed(): void {
    clearTimeout(this.stableTimer);
  }
}

function splitText(text: string, maxLength = MAX_MESSAGE_LENGTH): string[] {
  if (text.length <= maxLength) return [text];
  const chunks: string[] = [];
  for (let start = 0; start < text.length; start += maxLength) {
    chunks.push(text.slice(start, start + maxLength));
  }
  return chunks;
}

function guessFileName(msg: WAMessage): string {
  const message = msg.message;
  if (message?.documentMessage?.fileName) return message.documentMessage.fileName;
  if (message?.audioMessage) return message.audioMessage.ptt ? 'voice-note.ogg' : 'audio.ogg';
  if (message?.imageMessage) return 'image.jpg';
  if (message?.videoMessage) return 'video.mp4';
  return 'file';
}

async function downloadToStaging(
  socket: WASocket,
  msg: WAMessage,
  messageId: string,
  config: BridgeConfig,
): Promise<string> {
  const buffer = (await downloadMediaMessage(
    msg,
    'buffer',
    {},
    { logger, reuploadRequest: socket.updateMediaMessage },
  )) as Buffer;

  const dir = path.join(config.stagingDir, messageId);
  await mkdir(dir, { recursive: true });
  const filePath = path.join(dir, guessFileName(msg));
  await writeFile(filePath, buffer);
  return filePath;
}

// contextInfo is nested per content type in WhatsApp's protobuf schema, not
// on the message envelope itself — only these types can carry it.
function getContextInfo(message: WAMessage) {
  const content = message.message;
  return (
    content?.extendedTextMessage?.contextInfo ??
    content?.imageMessage?.contextInfo ??
    content?.videoMessage?.contextInfo ??
    content?.documentMessage?.contextInfo ??
    content?.audioMessage?.contextInfo ??
    undefined
  );
}

type QuotedInfo = Pick<
  IncomingEvent,
  'quoted_text' | 'quoted_media_path' | 'quoted_media_mime_type' | 'quoted_media_type_label'
>;

const NO_QUOTE: QuotedInfo = {
  quoted_text: null,
  quoted_media_path: null,
  quoted_media_mime_type: null,
  quoted_media_type_label: null,
};

// WhatsApp's reply/quote feature: contextInfo.quotedMessage
// embeds the quoted content's own metadata (and, for media, everything
// needed to re-download it) directly in this message's event — no lookup
// against anything the bridge saw earlier is needed.
async function resolveQuotedContext(
  socket: WASocket,
  msg: WAMessage,
  config: BridgeConfig,
): Promise<QuotedInfo> {
  const contextInfo = getContextInfo(msg);
  const quoted = contextInfo?.quotedMessage;
  if (!quoted) return NO_QUOTE;

  const quotedText = quoted.conversation ?? quoted.extendedTextMessage?.text;
  if (quotedText) {
    return { ...NO_QUOTE, quoted_text: quotedText };
  }

  const label = quoted.imageMessage
    ? 'image'
    : quoted.videoMessage
      ? 'video'
      : quoted.documentMessage
        ? 'document'
        : quoted.audioMessage
          ? 'voice message'
          : undefined;
  if (!label) return NO_QUOTE; // e.g. a quoted sticker/poll — ignore rather than guess

  try {
    const quotedMsg: WAMessage = {
      key: {
        remoteJid: msg.key.remoteJid,
        id: contextInfo?.stanzaId ?? undefined,
        participant: contextInfo?.participant ?? undefined,
        // Self-chat: every message here is "from me" either way.
        fromMe: true,
      },
      message: quoted,
    };
    const buffer = (await downloadMediaMessage(
      quotedMsg,
      'buffer',
      {},
      { logger, reuploadRequest: socket.updateMediaMessage },
    )) as Buffer;

    const dir = path.join(config.stagingDir, `quoted-${randomUUID()}`);
    await mkdir(dir, { recursive: true });
    const filePath = path.join(dir, guessFileName(quotedMsg));
    await writeFile(filePath, buffer);

    const mimeType =
      quoted.imageMessage?.mimetype ??
      quoted.videoMessage?.mimetype ??
      quoted.documentMessage?.mimetype ??
      quoted.audioMessage?.mimetype ??
      null;

    return {
      quoted_text: null,
      quoted_media_path: filePath,
      quoted_media_mime_type: mimeType,
      quoted_media_type_label: label,
    };
  } catch (err) {
    logger.warn({ err }, 'failed to download quoted media; falling back to a text descriptor');
    return { ...NO_QUOTE, quoted_media_type_label: label };
  }
}

async function handleMessage(
  socket: WASocket,
  msg: WAMessage,
  me: OwnIdentity,
  config: BridgeConfig,
  bus: SocketBus,
): Promise<void> {
  if (!msg.message || !msg.key.remoteJid) return;

  const remoteJid = jidNormalizedUser(msg.key.remoteJid);
  logger.debug(
    { remoteJid, me, fromMe: msg.key.fromMe, participant: msg.key.participant },
    'messages.upsert entry',
  );
  // WhatsApp's newer identity system addresses a self-chat message under
  // either the phone-number JID or the LID JID depending on the client that
  // sent it — the phone app addresses it via LID, Desktop/Web via the
  // phone-number JID — so accept either.
  const isSelfChat = Boolean(me.pn) && (remoteJid === me.pn || remoteJid === me.lid);
  if (!isSelfChat) return; // not the self-chat: ignore entirely

  // Defense-in-depth: confirm WhatsApp itself attributes
  // this message to the account owner. `participant` is unreliable here —
  // WhatsApp sends `""` (not absent) for self-chat messages — so `fromMe`
  // is the robust signal.
  if (!msg.key.fromMe) {
    logger.warn({ remoteJid }, 'ignoring non-self message that reached the self-chat filter');
    return;
  }

  const contentType = getContentType(msg.message);
  const messageId = msg.key.id ?? randomUUID();
  const timestamp = new Date().toISOString();
  logger.debug({ contentType, messageKeys: Object.keys(msg.message) }, 'resolved content type');

  if (contentType === 'conversation' || contentType === 'extendedTextMessage') {
    const text = msg.message.conversation ?? msg.message.extendedTextMessage?.text ?? '';
    if (!text) {
      logger.warn({ contentType, message: msg.message }, 'text message resolved to empty text');
      return;
    }
    const quoted = await resolveQuotedContext(socket, msg, config);
    await bus.publishIncoming({
      message_id: messageId,
      timestamp,
      sender_jid: config.allowedSenderJid,
      type: 'text',
      text,
      media_path: null,
      media_mime_type: null,
      ...quoted,
    });
    logger.info({ messageId, textLength: text.length }, 'published incoming text message');
    return;
  }

  if (contentType === 'audioMessage' && msg.message.audioMessage?.ptt) {
    const mediaPath = await downloadToStaging(socket, msg, messageId, config);
    const quoted = await resolveQuotedContext(socket, msg, config);
    await bus.publishIncoming({
      message_id: messageId,
      timestamp,
      sender_jid: config.allowedSenderJid,
      type: 'voice',
      text: null,
      media_path: mediaPath,
      media_mime_type: msg.message.audioMessage.mimetype ?? null,
      ...quoted,
    });
    return;
  }

  const isFileAttachment =
    contentType === 'imageMessage' ||
    contentType === 'videoMessage' ||
    contentType === 'documentMessage' ||
    contentType === 'audioMessage';

  if (isFileAttachment) {
    const mediaPath = await downloadToStaging(socket, msg, messageId, config);
    const caption =
      msg.message.imageMessage?.caption ??
      msg.message.videoMessage?.caption ??
      msg.message.documentMessage?.caption ??
      null;
    const mimeType =
      msg.message.imageMessage?.mimetype ??
      msg.message.videoMessage?.mimetype ??
      msg.message.documentMessage?.mimetype ??
      msg.message.audioMessage?.mimetype ??
      null;
    const quoted = await resolveQuotedContext(socket, msg, config);
    await bus.publishIncoming({
      message_id: messageId,
      timestamp,
      sender_jid: config.allowedSenderJid,
      type: 'attachment',
      text: caption,
      media_path: mediaPath,
      media_mime_type: mimeType,
      ...quoted,
    });
    return;
  }

  logger.debug({ contentType }, 'ignoring unsupported message type');
}

interface OwnIdentity {
  pn: string;
  lid: string;
}

/**
 * Opens the auth state in `authDir` with every credentials save and key
 * operation tracked, so `tracker.drain()` can wait for all of them.
 */
async function openTrackedAuthState(authDir: string) {
  const { state, saveCreds } = await useAtomicMultiFileAuthState(authDir);
  const tracker = new AuthWriteTracker((err, kind) => {
    logger.error({ err }, `failed to save WhatsApp auth ${kind}`);
  });
  const auth = { creds: state.creds, keys: tracker.wrapKeys(state.keys) };
  return { auth, tracker, saveCreds };
}

export async function startBridge(config: BridgeConfig, bus: SocketBus): Promise<void> {
  await mkdir(config.authDir, { recursive: true });
  await mkdir(config.stagingDir, { recursive: true });
  await mkdir(config.outgoingStagingDir, { recursive: true });

  const { auth, tracker, saveCreds } = await openTrackedAuthState(config.authDir);
  const { version } = await fetchLatestBaileysVersion();

  let socket: WASocket | undefined;
  let me: OwnIdentity = { pn: '', lid: '' };
  let shuttingDown = false;
  const reconnectBackoff = new ReconnectBackoff();

  // Ends the WhatsApp connection, waits for pending auth state writes, closes
  // the orchestrator socket, and exits: 0 when every write finished, 1 when
  // a write failed or the drain timed out.
  const shutdown = async (reason: string): Promise<void> => {
    if (shuttingDown) return;
    shuttingDown = true;
    logger.info({ reason }, 'shutting down');
    let exitCode = 0;
    try {
      await socket?.end(undefined);
      await tracker.drain();
    } catch (err) {
      logger.error({ err }, 'auth state writes did not complete before exit');
      exitCode = 1;
    }
    await bus.close().catch(() => undefined);
    process.exit(exitCode);
  };

  process.once('SIGTERM', () => void shutdown('SIGTERM'));
  process.once('SIGINT', () => void shutdown('SIGINT'));

  const connect = (): void => {
    if (shuttingDown) return;
    const current = makeWASocket({ auth, logger, version });
    socket = current;

    current.ev.on('creds.update', () => tracker.saveCreds(saveCreds));

    current.ev.on('connection.update', (update) => {
      const { connection, lastDisconnect, qr } = update;
      if (shuttingDown) return;

      if (qr) {
        logger.info('scan this QR code with WhatsApp on your phone (Linked Devices):');
        qrcodeTerminal.generate(qr, { small: true });
      }

      if (connection === 'open') {
        me = {
          pn: current.user?.id ? jidNormalizedUser(current.user.id) : '',
          lid: current.user?.lid ? jidNormalizedUser(current.user.lid) : '',
        };
        logger.info({ me }, 'connected to WhatsApp');
        reconnectBackoff.onConnectionOpen();
      } else if (connection === 'close') {
        reconnectBackoff.onConnectionClosed();
        const statusCode = (lastDisconnect?.error as Boom | undefined)?.output?.statusCode;
        const shouldReconnect = statusCode !== DisconnectReason.loggedOut;
        logger.warn({ statusCode, shouldReconnect }, 'connection closed');
        if (!shouldReconnect) {
          logger.error('logged out — delete the auth directory and re-pair to continue');
          return;
        }
        const delayMs = reconnectBackoff.nextDelayMs();
        if (delayMs === undefined) {
          logger.error(
            `giving up after ${RECONNECT_MAX_ATTEMPTS} failed reconnect attempts without a stable ` +
              'connection; run `systemctl --user start whatsapp-bridge.service` once the issue clears',
          );
          void shutdown('reconnect attempts exhausted');
          return;
        }
        logger.warn({ delayMs }, 'reconnecting');
        setTimeout(connect, delayMs);
      }
    });

    current.ev.on('messages.upsert', ({ messages, type }) => {
      if (type !== 'notify') return; // ignore historical-sync replays, only live messages
      for (const msg of messages) {
        handleMessage(current, msg, me, config, bus).catch((err: unknown) => {
          logger.error({ err, messageId: msg.key.id }, 'failed to handle incoming message');
        });
      }
    });
  };

  connect();

  // Registered once, outside connect(), so reconnects don't stack up
  // duplicate subscriptions; it always sends through the latest socket.
  await bus.onOutgoing(async (event) => {
    if (!me.pn || !socket) {
      logger.warn('dropping outgoing message: not yet connected to WhatsApp');
      return;
    }
    if (event.media_path) {
      const buffer = await readFile(event.media_path);
      const mimetype = event.media_mime_type ?? 'application/octet-stream';
      if (mimetype.startsWith('audio/')) {
        await socket.sendMessage(me.pn, { audio: buffer, mimetype, ptt: true });
      } else {
        await socket.sendMessage(me.pn, {
          document: buffer,
          mimetype,
          fileName: event.media_filename ?? 'file',
          caption: event.caption ?? undefined,
        });
      }
      if (event.delete_after_send) {
        await unlink(event.media_path).catch((err: unknown) => {
          logger.warn({ err, path: event.media_path }, 'failed to clean up staged outgoing media');
        });
      }
      return;
    }
    for (const chunk of splitText(event.text ?? '')) {
      await socket.sendMessage(me.pn, { text: chunk });
    }
  });
}

const PAIR_MAX_CONNECT_ATTEMPTS = 5;
const PAIR_RETRY_DELAY_MS = 2_000;

/** Phone-number JID (`<digits>@s.whatsapp.net`, no device suffix) of a linked account. */
export function phoneJidOf(user: { id: string; phoneNumber?: string } | undefined): string | undefined {
  if (!user) return undefined;
  const candidate = isLidUser(user.id) ? user.phoneNumber : user.id;
  return candidate ? jidNormalizedUser(candidate) : undefined;
}

/**
 * Connects to WhatsApp (showing a QR code on stderr when the auth state is
 * not paired yet), closes the socket without logging out, waits for every
 * pending auth state write, then prints `PAIRED <jid>` as the only stdout
 * line and resolves. Rejects, without printing, on a fatal close, a failed
 * auth state write, or a drain that does not finish in time.
 */
export async function runPairing(config: BridgeConfig): Promise<void> {
  await mkdir(config.authDir, { recursive: true });
  const { auth, tracker, saveCreds } = await openTrackedAuthState(config.authDir);
  const { version } = await fetchLatestBaileysVersion();

  const jid = await new Promise<string>((resolve, reject) => {
    let attempts = 0;
    let settled = false;

    // Ends the socket (no events follow), then waits for pending writes.
    const finish = async (socket: WASocket, outcome: () => string): Promise<void> => {
      settled = true;
      try {
        await socket.end(undefined); // never logout(), which unpairs
        await tracker.drain();
        resolve(outcome());
      } catch (err) {
        reject(err);
      }
    };

    const connect = (): void => {
      const socket = makeWASocket({ auth, logger, version });
      socket.ev.on('creds.update', () => tracker.saveCreds(saveCreds));

      socket.ev.on('connection.update', (update) => {
        const { connection, lastDisconnect, qr } = update;
        if (settled) return;

        if (qr) {
          logger.info('scan this QR code with WhatsApp on your phone (Linked Devices):');
          qrcodeTerminal.generate(qr, { small: true }, (code: string) => {
            process.stderr.write(`${code}\n`);
          });
        }

        if (connection === 'open') {
          const pn = phoneJidOf(socket.user);
          void finish(socket, () => {
            if (!pn) throw new Error('connected, but WhatsApp did not expose a phone-number JID');
            return pn;
          });
        } else if (connection === 'close') {
          const statusCode = (lastDisconnect?.error as Boom | undefined)?.output?.statusCode;
          logger.warn({ statusCode }, 'connection closed before pairing completed');
          attempts += 1;
          if (statusCode === DisconnectReason.loggedOut || attempts >= PAIR_MAX_CONNECT_ATTEMPTS) {
            void finish(socket, () => {
              throw new Error(
                statusCode === DisconnectReason.loggedOut
                  ? 'WhatsApp logged this device out; delete the auth directory and pair again'
                  : `could not connect to WhatsApp (last status code: ${statusCode})`,
              );
            });
            return;
          }
          setTimeout(connect, PAIR_RETRY_DELAY_MS);
        }
      });
    };

    connect();
  });

  process.stdout.write(`PAIRED ${jid}\n`);
}
