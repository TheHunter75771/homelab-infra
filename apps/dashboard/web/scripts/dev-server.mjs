#!/usr/bin/env node
// Local preview: serves public/ with a fake API. Not for deployment.
//   npm run dev -- --port 8791 --scenario bad
// Switch at runtime: /__dev/scenario?name=normal|bad|media-lost|clean|sab|troubled|rehearsal|remote
// Backend down: /__dev/down?on=1 (and on=0 to bring it back).

import { createFakeApi } from '../test/fake-api.mjs';

const arg = (k, d) => {
  const i = process.argv.indexOf(`--${k}`);
  return i > -1 ? process.argv[i + 1] : d;
};
const fake = createFakeApi({ scenario: arg('scenario', 'normal') });
const url = await fake.listen(Number(arg('port', 8791)), '127.0.0.1');
console.log(`dashboard preview on ${url}`);
