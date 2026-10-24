// Starts a full dashboard (real HTTP server, real helper client over the fake helper socket) for a scenario.
import http from 'node:http';
import path from 'node:path';
import { createDashboard } from '../../src/dashboard.js';
import { createLogger } from '../../src/log.js';
import { startFakeHelper } from './fake-helper.js';
import { buildHost, helperFor, makeFetch, makeRun, makeStatfs, NOW, writeWeb } from './host.js';

export function captureLogger() {
  const lines = [];
  const log = createLogger({ level: 'debug', write: (line) => lines.push(line) });
  return { log, lines, text: () => lines.join('\n') };
}

// A clock that starts at NOW and advances with real time (so polling loops and deadlines still work).
export function anchoredClock(start = NOW) {
  const t0 = Date.now();
  return () => start + (Date.now() - t0);
}

export async function startDashboard(t, scenario = 'normal', opts = {}) {
  const helper = await startFakeHelper(t, { ...helperFor(scenario), ...(opts.helper ?? {}) });
  const host = buildHost(t, scenario, { helperSocket: helper.socketPath, ...(opts.host ?? {}) });
  if (opts.web !== false) writeWeb(opts.host?.webDir ?? path.join(host.root, 'web'));
  const captured = captureLogger();
  const fetchImpl = opts.fetch ?? makeFetch({ scenario, hang: scenario === 'one-app-down' ? ['sabnzbd'] : [], ...(opts.media ?? {}) });
  const statfs = opts.statfs ?? makeStatfs(scenario === 'clean' ? { mediaUsed: 16e9 } : {});
  const run = opts.run ?? makeRun(opts.runOptions);
  const clock = opts.clock ?? anchoredClock();
  const dash = createDashboard(host.cfg, {
    log: captured.log,
    clock,
    fetch: fetchImpl,
    statfs,
    run,
    webpush: opts.webpush,
    access: opts.access,
    actionOptions: { pollMs: 20, healthTimeoutMs: 2000, rerunPickupMs: 1500, rerunTimeoutMs: 2000, ...(opts.actionOptions ?? {}) },
    dockerOptions: opts.dockerOptions,
    appOptions: opts.appOptions,
  });
  await new Promise((resolve) => dash.server.listen(0, '127.0.0.1', resolve));
  t.after(async () => {
    dash.stop();
    dash.server.closeAllConnections();
    await new Promise((resolve) => dash.server.close(resolve));
  });
  if (opts.start !== false) await dash.start({ background: false });
  const base = `http://127.0.0.1:${dash.server.address().port}`;
  return { dash, base, port: dash.server.address().port, helper, host, fetch: fetchImpl, statfs, run, clock, ...captured };
}

export const WRITE_HEADERS = { 'Content-Type': 'application/json', 'X-Homelab': '1' };

export async function request(base, method, p, { body, headers = WRITE_HEADERS } = {}) {
  const res = await fetch(`${base}${p}`, {
    method,
    headers,
    body: body === undefined ? undefined : typeof body === 'string' ? body : JSON.stringify(body),
  });
  const text = await res.text();
  let json = null;
  try {
    json = JSON.parse(text);
  } catch {
    // not JSON
  }
  return { res, status: res.status, json, text, headers: res.headers };
}

export const getJson = (base, p, headers = {}) => request(base, 'GET', p, { headers });
export const postJson = (base, p, body = {}, headers = WRITE_HEADERS) => request(base, 'POST', p, { body, headers });

// Raw request so tests control every header (fetch adds its own).
export function rawRequest(port, { method = 'POST', path: p, headers = {}, chunks = [] }) {
  return new Promise((resolve, reject) => {
    const req = http.request({ host: '127.0.0.1', port, method, path: p, headers }, (res) => {
      const body = [];
      res.on('data', (c) => body.push(c));
      res.on('end', () => resolve({ status: res.statusCode, headers: res.headers, body: Buffer.concat(body).toString() }));
    });
    req.on('error', reject);
    for (const c of chunks) req.write(c);
    req.end();
  });
}

// Minimal SSE client: collects {event, id, data} and lets a test await the next event of a type.
export function sse(port, p, headers = {}) {
  const events = [];
  const waiters = [];
  let status = null;
  let resHeaders = null;
  let ended = false;
  let req;
  const ready = new Promise((resolve, reject) => {
    req = http.get({ host: '127.0.0.1', port, path: p, headers: { Accept: 'text/event-stream', ...headers } }, (res) => {
      status = res.statusCode;
      resHeaders = res.headers;
      res.setEncoding('utf8');
      let buf = '';
      res.on('data', (chunk) => {
        buf += chunk;
        let i;
        while ((i = buf.indexOf('\n\n')) !== -1) {
          const block = buf.slice(0, i);
          buf = buf.slice(i + 2);
          const ev = { event: 'message', id: null, data: '', comment: false };
          const dataLines = [];
          for (const line of block.split('\n')) {
            if (line.startsWith(':')) ev.comment = true;
            else if (line.startsWith('event: ')) ev.event = line.slice(7);
            else if (line.startsWith('id: ')) ev.id = line.slice(4);
            else if (line.startsWith('data: ')) dataLines.push(line.slice(6));
            else if (line.startsWith('retry: ')) ev.event = 'retry';
          }
          if (ev.comment || ev.event === 'retry') continue;
          try {
            ev.data = JSON.parse(dataLines.join('\n'));
          } catch {
            ev.data = dataLines.join('\n');
          }
          events.push(ev);
          for (const w of [...waiters]) {
            if (w.type === null || w.type === ev.event) {
              waiters.splice(waiters.indexOf(w), 1);
              w.resolve(ev);
            }
          }
        }
      });
      res.on('end', () => {
        ended = true;
        for (const w of waiters.splice(0)) w.reject(new Error('stream ended'));
      });
      resolve();
    });
    req.on('error', reject);
  });
  return {
    ready,
    events,
    get status() {
      return status;
    },
    get headers() {
      return resHeaders;
    },
    get ended() {
      return ended;
    },
    next(type = null, timeoutMs = 3000) {
      const seen = events.filter((e) => type === null || e.event === type);
      const waiter = {};
      const p = new Promise((resolve, reject) => {
        waiter.resolve = resolve;
        waiter.reject = reject;
      });
      waiter.type = type;
      waiter.seen = seen.length;
      waiters.push(waiter);
      const timer = setTimeout(() => {
        const idx = waiters.indexOf(waiter);
        if (idx !== -1) waiters.splice(idx, 1);
        waiter.reject(new Error(`no ${type ?? 'event'} within ${timeoutMs} ms`));
      }, timeoutMs);
      return p.finally(() => clearTimeout(timer));
    },
    async until(type, pred, timeoutMs = 3000) {
      const hit = events.find((e) => e.event === type && pred(e));
      if (hit) return hit;
      const deadline = Date.now() + timeoutMs;
      for (;;) {
        const left = deadline - Date.now();
        if (left <= 0) throw new Error(`no matching ${type}`);
        const ev = await this.next(type, left);
        if (pred(ev)) return ev;
      }
    },
    close() {
      req.destroy();
    },
  };
}
