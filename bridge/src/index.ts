import { runPairing, startBridge } from './baileys.js';
import { ConfigError, exitCodeFor, loadConfig } from './config.js';
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

// A configuration error exits 78, so systemd does not restart the bridge
// (RestartPreventExitStatus=78); any other startup error exits 1.
main().catch((err: unknown) => {
  if (err instanceof ConfigError) {
    // eslint-disable-next-line no-console
    console.error(`whatsapp-bridge configuration error: ${err.message}`);
  } else {
    // eslint-disable-next-line no-console
    console.error('whatsapp-bridge failed to start:', err);
  }
  process.exit(exitCodeFor(err));
});
