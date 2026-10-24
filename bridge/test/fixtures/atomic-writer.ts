// Rewrites argv[2] in a loop with large JSON payloads until killed.
import { writeFileAtomic } from '../../src/auth-state.js';

const target = process.argv[2];
const filler = 'x'.repeat(2_000_000);
let n = 0;
process.stdout.write('ready\n');
for (;;) {
  n += 1;
  await writeFileAtomic(target, JSON.stringify({ n, filler }));
}
