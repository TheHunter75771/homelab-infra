// The HTTP handler: push-core's endpoints (byte-compatible), the dashboard API (CONTRACTS §11.3) and the web build.
import { ActionError, actionView, APP_ACTIONS } from '../actions.js';
import { AccessDenied } from '../access.js';
import { APP_NAME_RE } from '../catalog.js';
import { DockerInactiveError } from '../helper/client.js';
import { buildPayload, sendToAll, TEST_MESSAGE } from '../push/sender.js';
import { StoreCorruptError, StoreFullError, ValidationError, toDevice } from '../push/store.js';
import { VapidError } from '../push/vapid.js';
import { parseLogItem } from '../redact.js';
import { SettingsError } from '../settings.js';
import { forAccess } from '../summary.js';
import { checkWriteGuards, HttpError, openSse, parseTarget, readJsonBody, sendJson, SSE_MAX_BUFFER } from './common.js';

export const DEFAULT_TEST_COOLDOWN_MS = 10_000;
export const MAX_EVENT_STREAMS = 32;
export const MAX_LOG_STREAMS = 8;
const QUIET_PATHS = new Set(['/healthz', '/api/summary', '/api/events']);

// Route table: [method, pattern, handlerName, {sse}]. ":x" segments match [A-Za-z0-9_.-]{1,64}.
const ROUTES = [
  ['GET', '/healthz', 'healthz'],
  ['GET', '/api/vapid-public-key', 'vapidKey'],
  ['POST', '/api/subscribe', 'subscribe'],
  ['POST', '/api/unsubscribe', 'unsubscribe'],
  ['POST', '/api/test', 'test'],
  ['GET', '/api/summary', 'summary'],
  ['GET', '/api/events', 'events', { sse: true }],
  ['GET', '/api/apps/:name/logs', 'logs', { sse: true }],
  ['POST', '/api/apps/:name/:action', 'appAction'],
  ['GET', '/api/actions/:id', 'actionStream', { sse: true }],
  ['POST', '/api/gate/rerun', 'gateRerun'],
  ['GET', '/api/push/devices', 'devices'],
  ['DELETE', '/api/push/devices/:id', 'deleteDevice'],
  ['GET', '/api/settings', 'getSettings'],
  ['PUT', '/api/settings', 'putSettings'],
].map(([method, pattern, handler, opts = {}]) => ({ method, parts: pattern.split('/').slice(1), handler, ...opts }));

const PARAM_RE = /^[A-Za-z0-9_.-]{1,64}$/;

export function matchRoute(method, pathname) {
  const segs = pathname.split('/').slice(1);
  const allowed = new Set();
  for (const r of ROUTES) {
    if (r.parts.length !== segs.length) continue;
    const params = {};
    let ok = true;
    for (let i = 0; i < segs.length; i++) {
      const p = r.parts[i];
      if (p.startsWith(':')) {
        if (!PARAM_RE.test(segs[i])) {
          ok = false;
          break;
        }
        params[p.slice(1)] = segs[i];
      } else if (p !== segs[i]) {
        ok = false;
        break;
      }
    }
    if (!ok) continue;
    allowed.add(r.method);
    if (r.method === 'GET' && !r.sse) allowed.add('HEAD');
    if (r.method === method || (method === 'HEAD' && r.method === 'GET' && !r.sse)) return { route: r, params };
  }
  return { route: null, allowed: [...allowed] };
}

export function createApp({
  cfg,
  log,
  store,
  vapid,
  webpush,
  summary,
  actions,
  docker,
  settings,
  reminders,
  access,
  staticFiles,
  secrets,
  refreshSecrets,
  sseMaxBuffer = SSE_MAX_BUFFER,
  testCooldownMs = DEFAULT_TEST_COOLDOWN_MS,
  clock = () => Date.now(),
}) {
  let lastTestAt = -Infinity;
  const slots = { events: 0, logs: 0 };
  const streams = new Set(); // open SSE streams

  // Reserve a stream slot before any await. The release is tied to the response's 'close' (a client that
  // leaves during an await, a dropped slow client, or a normal end) and runs exactly once.
  function reserveSlot(kind, max, res, message) {
    if (slots[kind] >= max) throw new HttpError(503, message);
    slots[kind] += 1;
    let held = true;
    const release = () => {
      if (!held) return;
      held = false;
      slots[kind] -= 1;
    };
    res.on('close', release);
    return release;
  }
  const gone = (req, res) => res.destroyed || res.writableEnded || req.socket?.destroyed === true;

  function devicesOrError() {
    try {
      return { devices: store.devices(), storeError: false };
    } catch (err) {
      if (!(err instanceof StoreCorruptError)) log.error('store read failed', { code: err.code ?? null });
      return { devices: [], storeError: true };
    }
  }

  function mapStoreError(err) {
    if (err instanceof ValidationError) return new HttpError(400, err.message);
    if (err instanceof StoreCorruptError) {
      return new HttpError(503, 'subscription store is unreadable; refusing to write (see server log)');
    }
    if (err instanceof StoreFullError) return new HttpError(409, err.message);
    log.error('store operation failed', { code: err.code ?? null });
    return new HttpError(500, 'could not update the subscription store');
  }

  async function currentSummary() {
    let cur = summary.current();
    const maxAge = 3 * cfg.intervals.core * 1000;
    if (!cur || clock() - Date.parse(cur.summary.generatedAt) > maxAge) {
      await summary.refreshCore();
      cur = summary.current();
    }
    if (!cur) throw new HttpError(503, 'the dashboard is still starting');
    return cur;
  }

  const etagFor = (cur, acc) => (acc === 'tailnet' ? cur.etag : `"${cur.hash}-${acc}"`);

  function knownApp(name) {
    if (!APP_NAME_RE.test(name)) return false;
    if (cfg.apps[name]) return true;
    return (summary.core()?.psList ?? []).some((p) => p?.name === name);
  }

  const handlers = {
    // ---- push-core API, kept as-is ------------------------------------------------------------------------
    healthz(req, res) {
      const checks = { vapid: 'ok', store: 'ok', writable: store.writable() };
      try {
        vapid.get();
      } catch (err) {
        checks.vapid = err instanceof VapidError ? err.reason : 'error';
      }
      try {
        store.list();
      } catch (err) {
        checks.store = err instanceof StoreCorruptError ? 'corrupt' : 'error';
      }
      const ok = checks.vapid === 'ok' && checks.store === 'ok' && checks.writable;
      sendJson(res, ok ? 200 : 503, { ok, checks });
    },

    vapidKey(req, res) {
      let keys;
      try {
        keys = vapid.get();
      } catch (err) {
        log.error('vapid keys unavailable', { reason: err.reason ?? null, error: err.message });
        throw new HttpError(503, 'VAPID keys are not configured');
      }
      sendJson(res, 200, { publicKey: keys.publicKey });
    },

    subscribe(req, res, { body }) {
      let result;
      try {
        result = store.subscribe(body.subscription, body.label);
      } catch (err) {
        throw mapStoreError(err);
      }
      const device = toDevice(result.entry);
      log.info(result.created ? 'subscribed' : 'subscription updated', { device: device.id, label: device.label });
      summary.refreshSoon();
      sendJson(res, result.created ? 201 : 200, { ok: true, created: result.created, device, devices: devicesOrError().devices });
    },

    unsubscribe(req, res, { body }) {
      let result;
      try {
        result = store.unsubscribe(body.endpoint);
      } catch (err) {
        throw mapStoreError(err);
      }
      log.info(result.removed ? 'unsubscribed' : 'unsubscribe: not found', {});
      if (result.removed) summary.refreshSoon();
      sendJson(res, 200, { ok: true, removed: result.removed, devices: devicesOrError().devices });
    },

    // Human-triggered, so exempt from rehearsal (decision 9c): every subscription, plain title.
    async test(req, res) {
      const wait = lastTestAt + testCooldownMs - clock();
      if (wait > 0) throw new HttpError(429, 'wait before sending another test', { 'Retry-After': String(Math.ceil(wait / 1000)) });
      let keys;
      try {
        keys = vapid.get();
      } catch (err) {
        log.error('vapid keys unavailable', { reason: err.reason ?? null, error: err.message });
        throw new HttpError(503, 'VAPID keys are not configured');
      }
      let entries;
      try {
        entries = store.list();
      } catch (err) {
        throw mapStoreError(err);
      }
      if (entries.length === 0) return sendJson(res, 200, { ok: true, total: 0, sent: 0, failed: 0, removed: 0, devices: [] });
      lastTestAt = clock();
      const result = await sendToAll({ entries, payload: buildPayload(TEST_MESSAGE), vapid: keys, webpush, log });
      let removed = 0;
      if (result.gone.length) {
        try {
          removed = store.removeEndpoints(result.gone);
        } catch {
          // logged by the store; the next send retries the removal
        }
      }
      log.info('test push', { total: result.total, sent: result.sent, failed: result.failed, removed });
      if (removed) summary.refreshSoon();
      sendJson(res, result.sent > 0 ? 200 : 502, {
        ok: result.sent > 0,
        total: result.total,
        sent: result.sent,
        failed: result.failed,
        removed,
        devices: devicesOrError().devices,
      });
    },

    // ---- dashboard API ------------------------------------------------------------------------------------
    async summary(req, res, { access: acc }) {
      const cur = await currentSummary();
      const etag = etagFor(cur, acc);
      const headers = { ETag: etag, 'Cache-Control': 'no-cache', 'X-Generated-At': cur.summary.generatedAt };
      if (req.headers['if-none-match'] === etag) {
        res.writeHead(304, headers);
        return res.end();
      }
      sendJson(res, 200, forAccess(cur.summary, acc), headers);
    },

    async events(req, res, { access: acc }) {
      const release = reserveSlot('events', MAX_EVENT_STREAMS, res, 'too many event streams');
      let cur;
      try {
        cur = await currentSummary();
      } catch (err) {
        release();
        throw err;
      }
      if (gone(req, res)) return release(); // the client left while the first summary was being built
      const sse = openSse(req, res, { log, label: 'events', registry: streams, maxBuffer: sseMaxBuffer });
      sse.onClose(release);
      const idFor = (c) => etagFor(c, acc).replace(/"/g, '');
      const id0 = idFor(cur);
      if (req.headers['last-event-id'] === id0) sse.send('tick', { generatedAt: cur.summary.generatedAt }, id0);
      else sse.send('summary', forAccess(cur.summary, acc), id0);
      const off = summary.onUpdate((ev) => {
        const id = idFor({ etag: ev.etag, hash: ev.etag.replace(/"/g, '') });
        if (ev.type === 'summary') sse.send('summary', forAccess(ev.summary, acc), id);
        else sse.send('tick', { generatedAt: ev.generatedAt }, id);
      });
      sse.onClose(off);
    },

    async logs(req, res, { params, query }) {
      const name = params.name;
      if (!knownApp(name)) throw new HttpError(404, 'no such app');
      const tail = query.has('tail') ? Number.parseInt(query.get('tail'), 10) : 500;
      if (!Number.isInteger(tail) || tail < 1 || tail > 2000) throw new HttpError(400, 'tail must be 1-2000');
      const follow = ['1', 'true'].includes(query.get('follow') ?? '');
      const since = query.get('since');
      if (since !== null && Number.isNaN(Date.parse(since))) throw new HttpError(400, 'since must be an ISO time');
      const core = summary.core();
      if (core && core.dockerState !== 'active') {
        throw new HttpError(409, core.gateStatus === 'failed' ? 'Held by the boot gate until the media drive is back.' : "Docker isn't running.");
      }
      const release = reserveSlot('logs', MAX_LOG_STREAMS, res, 'too many log streams');
      refreshSecrets?.(); // keys from the config files, whether or not the app is up
      const sse = openSse(req, res, { log, label: `logs:${name}`, registry: streams, maxBuffer: sseMaxBuffer });
      const ctrl = new AbortController();
      const flow = {}; // pause()/resume() of the helper stream
      let waiting = false;
      let buf = [];
      let flushTimer = null;
      const flush = () => {
        flushTimer = null;
        if (!buf.length) return;
        const ok = sse.send('lines', buf);
        buf = [];
        // backpressure: stop reading from the helper until the browser has caught up
        if (!ok && !sse.closed && !waiting) {
          waiting = true;
          flow.pause?.();
        }
      };
      sse.onDrain(() => {
        if (!waiting) return;
        waiting = false;
        flow.resume?.();
      });
      sse.onClose(() => {
        ctrl.abort();
        if (flushTimer) clearTimeout(flushTimer);
        release();
      });
      const finishWith = (payload) => {
        if (flushTimer) clearTimeout(flushTimer);
        flush();
        if (sse.closed) return;
        sse.send('end', payload);
        sse.end();
      };
      docker
        .logs(name, { tail, follow, since }, {
          signal: ctrl.signal,
          flow,
          onLine: (item) => {
            if (sse.closed) return;
            buf.push(parseLogItem(item, secrets));
            if (buf.length >= 200) {
              if (flushTimer) clearTimeout(flushTimer);
              flush();
            } else flushTimer ??= setTimeout(flush, 100);
          },
        })
        .then((final) => {
          if (final.aborted) return;
          if (final.ok) finishWith({ reason: final.reason === 'max-duration' ? 'max-duration' : 'ended' });
          else finishWith({ reason: 'error', message: String(final.message ?? final.reason ?? 'log stream failed').slice(0, 200) });
        })
        .catch((err) => {
          const message =
            err instanceof DockerInactiveError
              ? "Docker isn't running."
              : err.code === 'held-by-gate' || err.code === 'not-allowed'
                ? String(err.message).slice(0, 200)
                : `Log stream unavailable (${err.code || 'error'}).`;
          finishWith({ reason: 'error', message });
        })
        .catch((err) => log.error('log stream failed', { error: err?.message ?? String(err) }));
    },

    async appAction(req, res, { params, access: acc }) {
      if (!APP_ACTIONS.includes(params.action)) throw new HttpError(404, 'unknown action');
      if (!knownApp(params.name)) throw new HttpError(404, 'no such app');
      const a = await actions.startApp(params.action, params.name, { access: acc });
      sendJson(res, 202, { ok: true, action: actionView(a) });
    },

    async gateRerun(req, res, { access: acc }) {
      const a = await actions.startGateRerun({ access: acc });
      sendJson(res, 202, { ok: true, action: actionView(a) });
    },

    actionStream(req, res, { params }) {
      const a = actions.get(params.id);
      if (!a) throw new HttpError(404, 'no such action');
      if (String(req.headers.accept ?? '').includes('application/json')) return sendJson(res, 200, actionView(a));
      const sse = openSse(req, res, { log, label: 'action', registry: streams, maxBuffer: sseMaxBuffer });
      for (const s of a.steps) if (!['done', 'failed'].includes(s.step)) sse.send('step', s);
      if (a.state !== 'running') {
        sse.send('end', { state: a.state, error: a.error, detail: a.detail });
        return sse.end();
      }
      const off = actions.subscribe(a.id, (ev) => {
        if (ev.type === 'step') sse.send('step', { step: ev.step, at: ev.at });
        else {
          sse.send('end', { state: ev.state, error: ev.error, detail: ev.detail });
          sse.end();
        }
      });
      sse.onClose(off);
    },

    devices(req, res) {
      const { devices, storeError } = devicesOrError();
      if (storeError) throw new HttpError(503, 'subscription store is unreadable (see server log)');
      sendJson(res, 200, { devices });
    },

    deleteDevice(req, res, { params }) {
      let result;
      try {
        result = store.removeById(params.id);
      } catch (err) {
        throw mapStoreError(err);
      }
      if (!result.removed) throw new HttpError(404, 'no such device');
      log.info('device removed', { device: params.id });
      summary.refreshSoon();
      sendJson(res, 200, { ok: true, removed: true, devices: devicesOrError().devices });
    },

    getSettings(req, res) {
      sendJson(res, 200, settings.view());
    },

    putSettings(req, res, { body }) {
      let view;
      try {
        view = settings.update(body);
      } catch (err) {
        if (err instanceof SettingsError) throw new HttpError(400, err.message);
        log.error('settings write failed', { code: err.code ?? null });
        throw new HttpError(500, 'could not save settings');
      }
      log.info('settings saved', {});
      summary.refreshSoon();
      reminders?.check().catch(() => {});
      sendJson(res, 200, view);
    },
  };

  async function route(req, res, pathname, query) {
    const m = matchRoute(req.method, pathname);
    if (!m.route) {
      if (m.allowed.length) throw new HttpError(405, 'method not allowed', { Allow: m.allowed.join(', ') });
      if ((req.method === 'GET' || req.method === 'HEAD') && staticFiles?.handle(req, res, pathname)) return;
      if (!pathname.startsWith('/api/') && req.method !== 'GET' && req.method !== 'HEAD') {
        throw new HttpError(405, 'method not allowed', { Allow: 'GET, HEAD' });
      }
      throw new HttpError(404, 'not found');
    }
    let acc;
    try {
      acc = await access.decide(req);
    } catch (err) {
      if (err instanceof AccessDenied) throw new HttpError(403, 'access denied');
      throw err;
    }
    let body = {};
    if (m.route.method !== 'GET') {
      checkWriteGuards(req);
      body = await readJsonBody(req);
    }
    return handlers[m.route.handler](req, res, { params: m.params, body, query, access: acc });
  }

  async function handle(req, res) {
    const started = process.hrtime.bigint();
    let pathname = '(bad target)';
    try {
      const url = parseTarget(req.url); // 400, never a crash, for "//", "/\\", "/%", absolute-form, "*"
      pathname = url.pathname;
      await route(req, res, pathname, url.searchParams);
    } catch (err) {
      if (err instanceof HttpError) sendJson(res, err.status, { error: err.message }, err.headers);
      else if (err instanceof ActionError) sendJson(res, err.status, { error: err.message });
      else {
        log.error('unhandled error', { error: err?.message ?? String(err) });
        if (!res.headersSent) sendJson(res, 500, { error: 'internal error' });
        else res.destroy();
      }
    } finally {
      const quiet = QUIET_PATHS.has(pathname) && (res.statusCode < 300 || res.statusCode === 304);
      if (!quiet) {
        log.info('request', {
          method: req.method,
          path: pathname.slice(0, 128),
          status: res.statusCode,
          ms: Number((process.hrtime.bigint() - started) / 1000n) / 1000,
        });
      }
    }
  }

  // Stats for tests and diagnostics: slot counters and the most bytes queued for any one SSE client.
  handle.stats = () => ({
    slots: { ...slots },
    streams: streams.size,
    maxBuffered: Math.max(0, ...[...streams].map((e) => e.res.writableLength)),
  });
  return handle;
}
