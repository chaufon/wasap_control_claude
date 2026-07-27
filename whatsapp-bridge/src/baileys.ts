// WhatsApp connection + self-chat message handling (SPEC.md §4.1, §5).
//
// Only messages in the owner's own "Message Yourself" chat are ever
// forwarded to Redis — every other chat and group is filtered out here,
// before anything reaches the orchestrator.

import { randomUUID } from 'node:crypto';
import { mkdir, readFile, unlink, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { Boom } from '@hapi/boom';
import makeWASocket, {
  DisconnectReason,
  downloadMediaMessage,
  getContentType,
  jidNormalizedUser,
  useMultiFileAuthState,
  type WAMessage,
  type WASocket,
} from '@whiskeysockets/baileys';
import pino from 'pino';
import qrcodeTerminal from 'qrcode-terminal';
import type { BridgeConfig } from './config.js';
import { RedisBus, type IncomingEvent } from './redis.js';

const logger = pino({ level: process.env.LOG_LEVEL ?? 'info' });

const MAX_MESSAGE_LENGTH = 4000;

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

// WhatsApp's reply/quote feature (SPEC.md §5.9/§7.6): contextInfo.quotedMessage
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
        // Self-chat: every message here is "from me" either way (SPEC.md §4.1).
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
  bus: RedisBus,
): Promise<void> {
  if (!msg.message || !msg.key.remoteJid) return;

  const remoteJid = jidNormalizedUser(msg.key.remoteJid);
  logger.debug(
    { remoteJid, me, fromMe: msg.key.fromMe, participant: msg.key.participant },
    'messages.upsert entry',
  );
  // WhatsApp's newer identity system addresses a self-chat message under
  // either the phone-number JID or the LID JID depending on the client that
  // sent it — accept both (confirmed live: phone-sent self-chat messages
  // arrive as `${lid}@lid`, not the `@s.whatsapp.net` address).
  const isSelfChat = Boolean(me.pn) && (remoteJid === me.pn || remoteJid === me.lid);
  if (!isSelfChat) return; // not the self-chat: ignore entirely

  // Defense-in-depth (SPEC.md §4.1, §9): confirm WhatsApp itself attributes
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

export async function startBridge(config: BridgeConfig, bus: RedisBus): Promise<void> {
  await mkdir(config.authDir, { recursive: true });
  await mkdir(config.stagingDir, { recursive: true });
  await mkdir(config.outgoingStagingDir, { recursive: true });

  const { state, saveCreds } = await useMultiFileAuthState(config.authDir);

  let socket: WASocket;
  let me: OwnIdentity = { pn: '', lid: '' };

  const connect = (): void => {
    socket = makeWASocket({ auth: state, logger });

    socket.ev.on('creds.update', saveCreds);

    socket.ev.on('connection.update', (update) => {
      const { connection, lastDisconnect, qr } = update;

      if (qr) {
        logger.info('scan this QR code with WhatsApp on your phone (Linked Devices):');
        qrcodeTerminal.generate(qr, { small: true });
      }

      if (connection === 'open') {
        me = {
          pn: socket.user?.id ? jidNormalizedUser(socket.user.id) : '',
          lid: socket.user?.lid ? jidNormalizedUser(socket.user.lid) : '',
        };
        logger.info({ me }, 'connected to WhatsApp');
      } else if (connection === 'close') {
        const statusCode = (lastDisconnect?.error as Boom | undefined)?.output?.statusCode;
        const shouldReconnect = statusCode !== DisconnectReason.loggedOut;
        logger.warn({ statusCode, shouldReconnect }, 'connection closed');
        if (shouldReconnect) {
          setTimeout(connect, 5000);
        } else {
          logger.error('logged out — delete the auth directory and re-pair to continue');
        }
      }
    });

    socket.ev.on('messages.upsert', ({ messages, type }) => {
      if (type !== 'notify') return; // ignore historical-sync replays, only live messages
      for (const msg of messages) {
        handleMessage(socket, msg, me, config, bus).catch((err: unknown) => {
          logger.error({ err, messageId: msg.key.id }, 'failed to handle incoming message');
        });
      }
    });
  };

  connect();

  // Registered once, outside connect(), so reconnects don't stack up
  // duplicate subscriptions; it always sends through the latest socket.
  await bus.onOutgoing(async (event) => {
    if (!me.pn) {
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
