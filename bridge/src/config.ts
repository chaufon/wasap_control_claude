// Reads the same config.yaml the Python orchestrator uses,
// plus a `bridge` section for settings specific to this process.

import { existsSync, readFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { parse } from 'yaml';
import { defaultSocketPath } from './socket.js';

export interface BridgeConfig {
  allowedSenderJid: string;
  socketPath: string;
  authDir: string;
  stagingDir: string;
  outgoingStagingDir: string;
}

const DEFAULT_CONFIG_PATH =
  process.env.CLAUDE_ORCHESTRATOR_CONFIG ??
  path.join(os.homedir(), '.config', 'claude-orchestrator', 'config.yaml');

function expandHome(value: string): string {
  if (value === '~') return os.homedir();
  if (value.startsWith('~/')) return path.join(os.homedir(), value.slice(2));
  return value;
}

const PLACEHOLDER_SENDER_JID = 'REPLACE_ME@s.whatsapp.net';

export interface LoadConfigOptions {
  /** When false, a missing or placeholder `allowed_sender_jid` yields an empty string. */
  requireSender: boolean;
}

export function loadConfig(
  configPath: string = DEFAULT_CONFIG_PATH,
  options: LoadConfigOptions = { requireSender: true },
): BridgeConfig {
  if (!existsSync(configPath)) {
    throw new Error(
      `config file not found at ${configPath}. See deploy/config.example.yaml.`,
    );
  }

  const raw = parse(readFileSync(configPath, 'utf-8')) ?? {};

  let allowedSenderJid: string = raw?.whatsapp?.allowed_sender_jid ?? '';
  if (!options.requireSender && allowedSenderJid === PLACEHOLDER_SENDER_JID) {
    allowedSenderJid = '';
  }
  if (options.requireSender && !allowedSenderJid) {
    throw new Error('config.whatsapp.allowed_sender_jid is required');
  }

  const transport = raw?.transport ?? {};
  const bridge = raw?.bridge ?? {};

  return {
    allowedSenderJid,
    socketPath: expandHome(transport.socket_path ?? defaultSocketPath()),
    authDir: expandHome(
      bridge.auth_dir ?? path.join(os.homedir(), '.local', 'share', 'whatsapp-bridge', 'auth'),
    ),
    stagingDir: expandHome(
      bridge.staging_dir ?? path.join(os.tmpdir(), 'whatsapp-bridge', 'incoming'),
    ),
    outgoingStagingDir: expandHome(
      bridge.outgoing_staging_dir ?? path.join(os.tmpdir(), 'whatsapp-bridge', 'outgoing'),
    ),
  };
}
