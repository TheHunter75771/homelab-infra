// A stand-in for apps/dashboard/server, speaking its wire format: serves public/ (with the server's CSP)
// and fakes the §11.3 API over the fixtures. Used by the smoke tests and `npm run dev`. Never deployed.

import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { SCENARIOS, LOG_LINES } from './fixtures.mjs';

export const PUBLIC_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../public');

// The server's CSP (apps/dashboard/server/src/http/common.js): no inline scripts or styles.
export const CSP =
  "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self'; " +
  "manifest-src 'self'; worker-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'";

export const TYPES = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.woff2': 'font/woff2',
  '.webmanifest': 'application/manifest+json; charset=utf-8',
  '.txt': 'text/plain; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
};

const iso = (ms) => new Date(ms).toISOString();

export function createFakeApi({ scenario = 'normal', now = () => Date.now(), tickMs = 10000, pingMs = 15000, logEveryMs = 1500, stepMs = 700, publicDir = PUBLIC_DIR } = {}) {
  let name = scenario;
  let summary = SCENARIOS[name](now());
  let down = false; // backend unreachable: every /api request has its socket destroyed
  let hang = false; // /api/summary never answers (the loading state)
  let proxyError = 0; // 502/503/504 for every request: tailscale serve is up, the dashboard behind it isn't
  let apiError = 0; // the same, for /api/* only (the page itself already loaded or came from the cache)
  const streams = new Set();
  const actions = new Map();
  let settings = {
    thresholds: { diskWarnPct: 90, backupMaxAgeH: 36 },
    subscriptions: (summary.subscriptions || []).map(({ key, name: n, date }) => ({ key, name: n, date })),
    integrations: { jellyfin: { configured: true, last4: 'c0de' }, jellyseerr: { configured: false, last4: null } },
  };
  const requests = [];

  const hashOf = () => crypto.createHash('sha1').update(JSON.stringify({ ...summary, generatedAt: null })).digest('hex').slice(0, 16);

  function sseSend(res, event, data, id) {
    let out = `event: ${event}\n`;
    if (id) out += `id: ${id}\n`;
    res.write(`${out}data: ${JSON.stringify(data)}\n\n`);
  }

  function broadcast() {
    for (const s of streams) if (s.kind === 'events') sseSend(s.res, 'summary', summary, hashOf());
  }

  function setScenario(next) {
    if (!SCENARIOS[next]) return false;
    name = next;
    summary = SCENARIOS[next](now());
    settings.subscriptions = (summary.subscriptions || []).map(({ key, name: n, date }) => ({ key, name: n, date }));
    broadcast();
    return true;
  }

  function mutate(fn) {
    fn(summary);
    summary = { ...summary, generatedAt: iso(now()) };
    broadcast();
  }

  const json = (res, status, body, headers = {}) => {
    res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store', 'Content-Security-Policy': CSP, ...headers });
    res.end(JSON.stringify(body));
  };

  const openSse = (res, kind) => {
    res.writeHead(200, { 'Content-Type': 'text/event-stream; charset=utf-8', 'Cache-Control': 'no-store', Connection: 'keep-alive', 'Content-Security-Policy': CSP });
    res.write('retry: 10000\n\n');
    const s = { res, kind, timers: [] };
    streams.add(s);
    s.timers.push(setInterval(() => res.write(': ping\n\n'), pingMs));
    res.on('close', () => {
      s.timers.forEach((t) => {
        clearInterval(t);
        clearTimeout(t);
      });
      streams.delete(s);
    });
    return s;
  };

  async function readBody(req) {
    const chunks = [];
    for await (const c of req) chunks.push(c);
    const text = Buffer.concat(chunks).toString('utf8');
    return text.trim() ? JSON.parse(text) : {};
  }

  // The server's guards (checkWriteGuards), in its order.
  function guard(req, res) {
    if (req.headers['x-homelab'] !== '1') return json(res, 403, { error: 'missing X-Homelab: 1 header' }), false;
    const site = req.headers['sec-fetch-site'];
    if (site !== undefined && site !== 'same-origin' && site !== 'none') return json(res, 403, { error: 'cross-site request refused' }), false;
    if (String(req.headers['content-type'] || '').split(';')[0].trim().toLowerCase() !== 'application/json') {
      return json(res, 415, { error: 'Content-Type must be application/json' }), false;
    }
    return true;
  }

  const appOf = (n) => summary.apps.find((a) => a.name === n);

  function startAction(kind, appName, steps, finish) {
    const id = crypto.randomBytes(8).toString('hex');
    const a = { id, kind, app: appName, state: 'running', steps: [], error: null, detail: null, startedAt: iso(now()), finishedAt: null, plan: steps, finish };
    actions.set(id, a);
    if (appName) mutate(() => Object.assign(appOf(appName), { action: { id, kind, step: null } }));
    let i = 0;
    const next = () => {
      if (i < steps.length) {
        const st = { step: steps[i], at: iso(now()) };
        a.steps.push(st);
        if (appName && appOf(appName)) mutate(() => (appOf(appName).action = { id, kind, step: st.step }));
        for (const s of streams) if (s.kind === `action:${id}`) sseSend(s.res, 'step', st);
        i += 1;
        setTimeout(next, stepMs);
        return;
      }
      const r = finish();
      a.state = r.ok ? 'done' : 'failed';
      a.error = r.error || null;
      a.detail = r.detail || null;
      a.finishedAt = iso(now());
      if (appName && appOf(appName)) mutate(() => (appOf(appName).action = null));
      for (const s of streams) {
        if (s.kind === `action:${id}`) {
          sseSend(s.res, 'end', { state: a.state, error: a.error, detail: a.detail });
          s.res.end();
        }
      }
    };
    setTimeout(next, 10);
    return a;
  }

  const view = (a) => ({ id: a.id, kind: a.kind, app: a.app, state: a.state, steps: a.steps, error: a.error, detail: a.detail, startedAt: a.startedAt, finishedAt: a.finishedAt });

  const setRunning = (target, healthy = true) =>
    Object.assign(target, {
      status: healthy ? 'healthy' : 'unhealthy',
      stoppedReason: null,
      state: 'running',
      health: target.name === 'wol-relay' ? null : healthy ? 'healthy' : 'unhealthy',
      startedAt: iso(now()),
      locks: { ...target.locks, restart: null, stop: null, start: 'Already running' },
    });

  async function api(req, res, url) {
    const p = url.pathname;
    const m = req.method;
    requests.push(`${m} ${p}`);
    if (m !== 'GET' && m !== 'HEAD' && !guard(req, res)) return;
    let body = {};
    if (m !== 'GET' && m !== 'HEAD') {
      try {
        body = await readBody(req);
      } catch {
        return json(res, 400, { error: 'body is not valid JSON' });
      }
    }

    if (m === 'GET' && p === '/api/summary') {
      if (hang) return; // never answer
      const etag = `"${hashOf()}"`;
      if (req.headers['if-none-match'] === etag) {
        res.writeHead(304, { ETag: etag });
        return res.end();
      }
      return json(res, 200, summary, { ETag: etag, 'Cache-Control': 'no-cache' });
    }
    if (m === 'GET' && p === '/api/events') {
      const s = openSse(res, 'events');
      sseSend(res, 'summary', summary, hashOf());
      s.timers.push(setInterval(() => sseSend(res, 'tick', { generatedAt: iso(now()) }, hashOf()), tickMs));
      return;
    }
    let mm = /^\/api\/apps\/([A-Za-z0-9_.-]+)\/logs$/.exec(p);
    if (m === 'GET' && mm) {
      const target = appOf(mm[1]);
      if (!target) return json(res, 404, { error: 'no such app' });
      if (summary.docker?.state !== 'active') return json(res, 409, { error: target.locks?.logs || "Docker isn't running." });
      const s = openSse(res, 'logs');
      const base = now() - 8 * 3600e3;
      const tail = Math.min(Number(url.searchParams.get('tail') || 500), 2000);
      sseSend(res, 'lines', LOG_LINES.slice(-tail).map(([level, msg], i) => ({ ts: iso(base + i * 600e3), level, msg, stream: 'stdout' })));
      if (url.searchParams.get('follow') === '1') {
        let i = 0;
        s.timers.push(
          setInterval(() => {
            i += 1;
            const warn = i % 7 === 3;
            sseSend(res, 'lines', [{ ts: iso(now()), level: warn ? 'WRN' : 'INF', msg: warn ? '[WRN] Queue item stalled for 30 s' : 'GET /api/v3/health 200 4ms', stream: 'stdout' }]);
          }, logEveryMs),
        );
      } else {
        sseSend(res, 'end', { reason: 'ended' });
        res.end();
      }
      return;
    }
    mm = /^\/api\/apps\/([A-Za-z0-9_.-]+)\/([a-z]+)$/.exec(p);
    if (m === 'POST' && mm) {
      const [, n, verb] = mm;
      const target = appOf(n);
      if (!['restart', 'stop', 'start', 'update'].includes(verb)) return json(res, 404, { error: 'unknown action' });
      if (!target) return json(res, 404, { error: 'no such app' });
      if (verb === 'update' && summary.access !== 'tailnet') return json(res, 403, { error: 'Only available on the tailnet' });
      if (target.action) return json(res, 409, { error: 'Busy' });
      if (target.locks?.[verb]) return json(res, 409, { error: target.locks[verb] });
      let a;
      if (verb === 'stop') {
        a = startAction('stop', n, ['stopping'], () => {
          mutate(() => Object.assign(target, { status: 'stopped', stoppedReason: 'intentional', state: 'exited', health: null, startedAt: null, locks: { ...target.locks, restart: 'Stopped. Start it instead', stop: 'Already stopped', start: null } }));
          return { ok: true, detail: `${target.label} stopped. It stays down until you start it.` };
        });
      } else if (verb === 'update') {
        a = startAction('update', n, ['pulling', 'recreating', 'waiting-health'], () => {
          mutate(() => {
            setRunning(target);
            Object.assign(target, { image: target.pinned, tag: target.pinnedTag, updateAvailable: false, locks: { ...target.locks, update: 'Already on the pinned version' } });
          });
          return { ok: true, detail: `${target.label} updated and healthy` };
        });
      } else {
        a = startAction(verb, n, [verb === 'restart' ? 'restarting' : 'starting', 'waiting-health'], () => {
          mutate(() => setRunning(target));
          return { ok: true, detail: `${target.label} is healthy again` };
        });
      }
      return json(res, 202, { ok: true, action: view(a) });
    }
    mm = /^\/api\/actions\/([A-Za-z0-9_.-]+)$/.exec(p);
    if (m === 'GET' && mm) {
      const a = actions.get(mm[1]);
      if (!a) return json(res, 404, { error: 'no such action' });
      openSse(res, `action:${a.id}`);
      for (const st of a.steps) sseSend(res, 'step', st);
      if (a.state !== 'running') {
        sseSend(res, 'end', { state: a.state, error: a.error, detail: a.detail });
        res.end();
      }
      return;
    }
    if (m === 'POST' && p === '/api/gate/rerun') {
      if (summary.access !== 'tailnet') return json(res, 403, { error: 'Only available on the tailnet' });
      if (!summary.gate.canRerun) return json(res, 409, { error: summary.gate.rerunLock || 'not available' });
      const a = startAction('gate-rerun', null, ['requested', 'running'], () => ({
        ok: false,
        error: `Check failed: the drive is still not verified. Next automatic retry in ${summary.gate.retryMinutes} min.`,
      }));
      return json(res, 202, { ok: true, action: view(a) });
    }
    if (m === 'GET' && p === '/api/push/devices') return json(res, 200, { devices: summary.push?.devices || [] });
    mm = /^\/api\/push\/devices\/([A-Za-z0-9_.-]+)$/.exec(p);
    if (m === 'DELETE' && mm) {
      if (!(summary.push?.devices || []).some((d) => d.id === mm[1])) return json(res, 404, { error: 'no such device' });
      mutate((s) => {
        s.push = { ...s.push, devices: s.push.devices.filter((d) => d.id !== mm[1]) };
      });
      return json(res, 200, { ok: true, removed: true, devices: summary.push.devices });
    }
    if (m === 'GET' && p === '/api/vapid-public-key') return json(res, 200, { publicKey: 'BKxdemo-not-a-real-key' });
    if (m === 'POST' && p === '/api/test') {
      const n = summary.push?.devices?.length || 0;
      return json(res, 200, { ok: true, total: n, sent: n, failed: 0, removed: 0, devices: summary.push?.devices || [] });
    }
    if (m === 'POST' && p === '/api/subscribe') return json(res, 201, { ok: true, created: true, device: { id: 'aaaabbbbccccdddd', label: body.label || 'device' }, devices: summary.push?.devices || [] });
    if (m === 'POST' && p === '/api/unsubscribe') return json(res, 200, { ok: true, removed: false, devices: summary.push?.devices || [] });
    if (m === 'GET' && p === '/api/settings') return json(res, 200, settings);
    if (m === 'PUT' && p === '/api/settings') {
      const unknown = Object.keys(body).filter((k) => !['thresholds', 'subscriptions', 'integrations'].includes(k));
      if (unknown.length) return json(res, 400, { error: `unknown setting(s): ${unknown.join(', ')}` });
      const next = structuredClone(settings);
      if (body.thresholds) Object.assign(next.thresholds, body.thresholds);
      if (body.subscriptions) next.subscriptions = body.subscriptions.map((s) => ({ key: s.key || s.name.toLowerCase().replace(/[^a-z0-9]+/g, '-'), name: s.name, date: s.date }));
      if (body.integrations) {
        for (const [field, v] of Object.entries(body.integrations)) {
          const appName = field.replace(/ApiKey$/, '');
          if (!(appName in next.integrations)) return json(res, 400, { error: `unknown integration field ${field}` });
          next.integrations[appName] = v ? { configured: true, last4: String(v).slice(-4) } : { configured: false, last4: null };
        }
      }
      settings = next;
      mutate((s) => {
        s.settings = { thresholds: { ...settings.thresholds } };
        const today = iso(now()).slice(0, 10);
        s.subscriptions = settings.subscriptions.map((x) => ({ ...x, daysLeft: Math.round((Date.parse(`${x.date}T00:00:00Z`) - Date.parse(`${today}T00:00:00Z`)) / 86400000) }));
      });
      return json(res, 200, settings);
    }
    return json(res, 404, { error: 'not found' });
  }

  function serveStatic(req, res, url) {
    let rel = decodeURIComponent(url.pathname);
    if (rel === '/') rel = '/index.html';
    const file = path.normalize(path.join(publicDir, rel));
    if (!file.startsWith(publicDir + path.sep) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) {
      res.writeHead(404, { 'Content-Type': 'text/plain' });
      return res.end('not found');
    }
    const type = TYPES[path.extname(file)] || 'application/octet-stream';
    const buf = fs.readFileSync(file);
    const tag = `"${crypto.createHash('sha1').update(buf).digest('hex').slice(0, 16)}"`;
    const headers = { 'Content-Type': type, 'Cache-Control': 'no-cache', ETag: tag, 'Content-Security-Policy': CSP, 'X-Content-Type-Options': 'nosniff' };
    if (req.headers['if-none-match'] === tag) {
      res.writeHead(304, headers);
      return res.end();
    }
    res.writeHead(200, { ...headers, 'Content-Length': buf.length });
    res.end(req.method === 'HEAD' ? undefined : buf);
  }

  const server = http.createServer((req, res) => {
    const url = new URL(req.url, 'http://localhost');
    if (url.pathname.startsWith('/__dev/')) {
      if (url.pathname === '/__dev/scenario') return json(res, setScenario(url.searchParams.get('name')) ? 200 : 404, { scenario: name });
      if (url.pathname === '/__dev/proxy-error') {
        proxyError = Number(url.searchParams.get('status')) || 0;
        if (proxyError) for (const s of streams) s.res.destroy();
        return json(res, 200, { proxyError });
      }
      if (url.pathname === '/__dev/down') {
        down = url.searchParams.get('on') === '1';
        if (down) for (const s of streams) s.res.destroy();
        return json(res, 200, { down });
      }
      return json(res, 404, {});
    }
    if (proxyError) {
      res.writeHead(proxyError, { 'Content-Type': 'text/html; charset=utf-8' });
      return res.end(`<html><body>${proxyError} from the proxy</body></html>`);
    }
    if (url.pathname.startsWith('/api/') && apiError) {
      res.writeHead(apiError, { 'Content-Type': 'text/html; charset=utf-8' });
      return res.end(`<html><body>${apiError} from the proxy</body></html>`);
    }
    if (url.pathname.startsWith('/api/')) {
      if (down) return req.socket.destroy();
      api(req, res, url).catch((err) => json(res, 500, { error: String(err.message || err) }));
      return;
    }
    serveStatic(req, res, url);
  });

  return {
    server,
    listen(port = 0, host = '127.0.0.1') {
      return new Promise((resolve) => server.listen(port, host, () => resolve(`http://${host}:${server.address().port}`)));
    },
    close() {
      for (const s of streams) s.res.destroy();
      server.closeAllConnections?.();
      return new Promise((resolve) => server.close(() => resolve()));
    },
    setScenario,
    setDown(v) {
      down = Boolean(v);
      if (down) for (const s of streams) s.res.destroy();
    },
    setHang(v) {
      hang = Boolean(v);
    },
    setApiError(code) {
      apiError = Number(code) || 0;
      if (apiError) for (const s of streams) s.res.destroy();
    },
    setProxyError(code) {
      proxyError = Number(code) || 0;
      if (proxyError) for (const s of streams) s.res.destroy();
    },
    get summary() {
      return summary;
    },
    get requests() {
      return requests;
    },
    get settings() {
      return settings;
    },
    mutate,
  };
}
