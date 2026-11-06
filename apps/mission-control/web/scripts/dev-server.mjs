#!/usr/bin/env node
// Local preview only: serves public/ with the CSP the server should send, plus a fake API built from the
// synthetic test fixtures. Never deployed. Usage:
//   node scripts/dev-server.mjs [--port 8097] [--scenario normal|broken|down|hostile|slow]
// Switch at runtime: GET /__scenario?name=down
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import * as fx from '../test/fixtures.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', 'public');
const arg = (n, d) => (process.argv.includes(n) ? process.argv[process.argv.indexOf(n) + 1] : d);
const PORT = Number(arg('--port', 8097));
let scenario = arg('--scenario', 'normal');

export const CSP = "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; manifest-src 'self'; worker-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'";
const TYPES = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml', '.png': 'image/png', '.webmanifest': 'application/manifest+json' };

// Fixtures are pinned to a fixed date; move epoch-second fields to now so "x min ago" reads naturally.
function shift(v, k) {
  const delta = (Date.now() - fx.NOW) / 1000;
  if (typeof v === 'number' && (k === 'updated_at' || k === 'generated_at')) return v + delta;
  if (Array.isArray(v)) return v.map((x) => shift(x));
  if (v && typeof v === 'object') return Object.fromEntries(Object.entries(v).map(([kk, x]) => [kk, shift(x, kk)]));
  return v;
}

function json(res, code, body) {
  res.writeHead(code, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store', 'Content-Security-Policy': CSP });
  res.end(JSON.stringify(body));
}

http
  .createServer(async (req, res) => {
    const url = new URL(req.url, 'http://localhost');
    const p = url.pathname;
    if (p === '/__scenario') {
      scenario = url.searchParams.get('name') || 'normal';
      return json(res, 200, { scenario });
    }
    if (p.startsWith('/api/')) {
      if (scenario === 'slow') await new Promise((r) => setTimeout(r, 3000));
      if (scenario === 'down') return json(res, 502, { error: 'bad gateway' });
      if (req.method === 'POST' && p.startsWith('/api/push/')) {
        if (req.headers['x-mission-control'] !== '1') return json(res, 403, { error: 'missing X-Mission-Control' });
        return json(res, 200, { ok: true });
      }
      if (p === '/api/cards') {
        const c = { normal: fx.cards, broken: fx.cardsBroken, hostile: () => fx.hostile(fx.cards()), slow: fx.cards }[scenario] || fx.cards;
        return json(res, 200, shift(c()));
      }
      if (p === '/api/briefing') {
        if (scenario === 'broken') return json(res, 500, { error: 'briefing failed' });
        return json(res, 200, shift(scenario === 'hostile' ? fx.hostile(fx.briefing()) : fx.briefing()));
      }
      return json(res, 404, { error: 'not found' });
    }
    const rel = p === '/' ? 'index.html' : p.slice(1);
    const file = path.resolve(ROOT, rel);
    if (!file.startsWith(ROOT) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) {
      res.writeHead(404);
      return res.end('not found');
    }
    res.writeHead(200, { 'Content-Type': TYPES[path.extname(file)] || 'application/octet-stream', 'Cache-Control': 'no-cache', 'Content-Security-Policy': CSP });
    fs.createReadStream(file).pipe(res);
  })
  .listen(PORT, '127.0.0.1', () => console.log(`Mission Control preview on http://127.0.0.1:${PORT} (scenario ${scenario})`));
