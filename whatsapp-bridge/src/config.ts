// Reads the same config.yaml the Python orchestrator uses (SPEC.md §8),
// plus a `bridge` section for settings specific to this process.

import { existsSync, readFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { parse } from 'yaml';

export interface BridgeConfig {
  allowedSenderJid: string;
  redisHost: string;
  redisPort: number;
  incomingChannel: string;
  outgoingChannel: string;
  authDir: string;
  stagingDir: string;
}

const DEFAULT_CONFIG_PATH =
  process.env.CLAUDE_ORCHESTRATOR_CONFIG ??
  path.join(os.homedir(), '.config', 'claude-orchestrator', 'config.yaml');

function expandHome(value: string): string {
  if (value === '~') return os.homedir();
  if (value.startsWith('~/')) return path.join(os.homedir(), value.slice(2));
  return value;
}

export function loadConfig(configPath: string = DEFAULT_CONFIG_PATH): BridgeConfig {
  if (!existsSync(configPath)) {
    throw new Error(
      `config file not found at ${configPath}. See config/config.example.yaml.`,
    );
  }

  const raw = parse(readFileSync(configPath, 'utf-8')) ?? {};

  const allowedSenderJid = raw?.whatsapp?.allowed_sender_jid;
  if (!allowedSenderJid) {
    throw new Error('config.whatsapp.allowed_sender_jid is required');
  }

  const redis = raw?.redis ?? {};
  const bridge = raw?.bridge ?? {};

  return {
    allowedSenderJid,
    redisHost: redis.host ?? '127.0.0.1',
    redisPort: redis.port ?? 6379,
    incomingChannel: redis.incoming_channel ?? 'whatsapp:incoming',
    outgoingChannel: redis.outgoing_channel ?? 'whatsapp:outgoing',
    authDir: expandHome(
      bridge.auth_dir ?? path.join(os.homedir(), '.local', 'share', 'whatsapp-bridge', 'auth'),
    ),
    stagingDir: expandHome(
      bridge.staging_dir ?? path.join(os.tmpdir(), 'whatsapp-bridge', 'incoming'),
    ),
  };
}
