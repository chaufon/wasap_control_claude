import { startBridge } from './baileys.js';
import { loadConfig } from './config.js';
import { RedisBus } from './redis.js';

async function main(): Promise<void> {
  const config = loadConfig();
  const bus = new RedisBus(config);
  await startBridge(config, bus);
}

main().catch((err: unknown) => {
  // eslint-disable-next-line no-console
  console.error('whatsapp-bridge failed to start:', err);
  process.exit(1);
});
