import { runPairing, startBridge } from './baileys.js';
import { loadConfig } from './config.js';
import { SocketBus } from './socket.js';

async function main(): Promise<void> {
  if (process.argv.includes('--pair')) {
    await runPairing(loadConfig(undefined, { requireSender: false }));
    process.exit(0);
  }
  const config = loadConfig();
  const bus = new SocketBus(config.socketPath);
  await startBridge(config, bus);
}

main().catch((err: unknown) => {
  // eslint-disable-next-line no-console
  console.error('whatsapp-bridge failed to start:', err);
  process.exit(1);
});
