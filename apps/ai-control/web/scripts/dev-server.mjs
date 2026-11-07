#!/usr/bin/env node
// Local preview only: serves public/ with the server's CSP and a fake API built from the synthetic test
// fixtures. Never deployed. Usage: node scripts/dev-server.mjs [--port 8095] [--scenario normal|gaming|pcoff|down|hostile]
// The scenario can also be switched at runtime: GET /__scenario?name=gaming. --port 0 takes any free port (the log
// line says which).
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import * as fx from '../test/fixtures.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', 'public');
const arg = (n, d) => (process.argv.includes(n) ? process.argv[process.argv.indexOf(n) + 1] : d);
const PORT = Number(arg('--port', 8095));
let scenario = arg('--scenario', 'normal');

const CSP = "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'";
const TYPES = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml', '.webmanifest': 'application/manifest+json', '.json': 'application/json' };

const TS_KEYS = new Set(['ts', 'time', 'since', 'modified', 'expires_at', 'next', 'last']);
// The scenario name comes from the query string, so it's looked up in a Map: a name like "constructor" or
// "__proto__" is just unknown (the normal summary), never an Object.prototype member that then gets called.
const SUMMARIES = new Map([['normal', fx.normal], ['gaming', fx.gamingCpu], ['pcoff', fx.pcOff], ['down', fx.everythingDown], ['hostile', fx.hostileSummary]]);

function scen() {
  const shift = (s) => {
    // fixtures are pinned to a fixed date; move them to now so relative times read naturally
    const delta = Date.now() - fx.NOW;
    const walk = (v, k) => {
      if (typeof v === 'number' && TS_KEYS.has(k) && v > 1e9 && v < 1e11) return v + delta / 1000;
      if (Array.isArray(v)) return v.map((x) => walk(x));
      if (v && typeof v === 'object') return Object.fromEntries(Object.entries(v).map(([kk, x]) => [kk, walk(x, kk)]));
      return v;
    };
    return walk(s);
  };
  const H = scenario === 'hostile';
  const wrap = (v) => shift(H ? fx.hostile(v) : v);
  const summary = SUMMARIES.get(scenario) || fx.normal;
  const hist = scenario === 'gaming' ? fx.history({ busy: 70, share: 0, available: false }) : fx.history();
  return {
    summary: shift(summary()),
    history: wrap(hist),
    requests: wrap(fx.requests()),
    logs: wrap(Array.from({ length: 60 }, (_, i) => `2026-10-26T19:${String(i).padStart(2, '0')}:00Z ollama[100]: ${i % 7 === 0 ? 'error: slow response' : 'request done in 2.1 s'}`)),
    sudo: wrap([...fx.sudoFlagged(), ...fx.sudo()]),
    sessions: wrap(fx.sessionsReply(2)),
    replay: wrap(fx.replay(true)),
    replayCmd: wrap({ ...fx.replay(false), output: null }),
    logSources: H ? fx.hostile(fx.logSources()).map((x, i) => ({ ...x, id: `${x.id}${i}` })) : fx.logSources(),
    localai: wrap(fx.localai()),
  };
}

function json(res, code, body) {
  res.writeHead(code, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store', 'Content-Security-Policy': CSP });
  res.end(JSON.stringify(body));
}

const server = http
  .createServer((req, res) => {
    const url = new URL(req.url, 'http://localhost');
    const p = url.pathname;
    if (p === '/__scenario') {
      scenario = url.searchParams.get('name') || 'normal';
      return json(res, 200, { scenario });
    }
    if (scenario === 'offline' && p.startsWith('/api/')) {
      res.writeHead(502);
      return res.end();
    }
    const d = scen();
    if (p === '/api/summary') return json(res, 200, d.summary);
    if (p === '/api/gate/history') return json(res, 200, d.history);
    if (p === '/api/requests') return json(res, 200, d.requests);
    if (p === '/api/logs') return json(res, 200, d.logs);
    if (p === '/api/audit/sudo') return json(res, 200, d.sudo);
    if (p === '/api/audit/sessions') return json(res, 200, d.sessions);
    if (p === '/api/audit/replay') return json(res, 200, url.searchParams.get('output') === '1' ? d.replay : d.replayCmd);
    if (p === '/api/logs/sources') return json(res, 200, d.logSources);
    if (p === '/api/audit/localai') return json(res, 200, d.localai);
    if (p === '/api/events') {
      res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-store' });
      res.write('event: summary\ndata: {}\n\n');
      const t = setInterval(() => res.write('event: summary\ndata: {}\n\n'), 15000);
      req.on('close', () => clearInterval(t));
      return;
    }
    if (p.startsWith('/api/')) return json(res, 404, { error: 'not found' });
    const file = path.join(ROOT, p === '/' ? 'index.html' : p);
    if (!file.startsWith(ROOT) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) {
      res.writeHead(404);
      return res.end();
    }
    res.writeHead(200, { 'Content-Type': TYPES[path.extname(file)] || 'application/octet-stream', 'Content-Security-Policy': CSP });
    fs.createReadStream(file).pipe(res);
  })
  .listen(PORT, '127.0.0.1', () => console.log(`AI Control preview on http://127.0.0.1:${server.address().port} (scenario ${scenario})`));
