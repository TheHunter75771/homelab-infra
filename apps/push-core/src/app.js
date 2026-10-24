// HTTP handler. No framework: a fixed route table plus an in-memory map of the static PWA files.
import nodeFs from 'node:fs';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { StoreCorruptError, StoreFullError, ValidationError, toDevice } from './store.js';
import { VapidError } from './vapid.js';
import { buildPayload, sendToAll, TEST_MESSAGE } from './sender.js';

export const MAX_BODY_BYTES = 8 * 1024;
export const DEFAULT_TEST_COOLDOWN_MS = 10_000;
const DEVICES_PLACEHOLDER = '__DEVICES_JSON__';

const TYPES = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.webmanifest': 'application/manifest+json; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.png': 'image/png',
  '.svg': 'image/svg+xml',
  '.ico': 'image/x-icon',
  '.txt': 'text/plain; charset=utf-8',
};

const SECURITY_HEADERS = {
  'Content-Security-Policy':
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; " +
    "manifest-src 'self'; worker-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; " +
    "frame-ancestors 'none'",
  'X-Content-Type-Options': 'nosniff',
  'Referrer-Policy': 'no-referrer',
  'X-Frame-Options': 'DENY',
  'Cross-Origin-Opener-Policy': 'same-origin',
  'Cross-Origin-Resource-Policy': 'same-origin',
};

class HttpError extends Error {
  constructor(status, message, headers = {}) {
    super(message);
    this.status = status;
    this.headers = headers;
  }
}

function loadStatic(publicDir) {
  const files = new Map();
  for (const rel of nodeFs.readdirSync(publicDir, { recursive: true })) {
    const relPath = String(rel);
    if (relPath.split(path.sep).some((part) => part.startsWith('.'))) continue;
    const abs = path.join(publicDir, relPath);
    if (!nodeFs.statSync(abs).isFile()) continue;
    const body = nodeFs.readFileSync(abs);
    const ext = path.extname(relPath).toLowerCase();
    files.set(`/${relPath.split(path.sep).join('/')}`, {
      body,
      type: TYPES[ext] ?? 'application/octet-stream',
      etag: `"${createHash('sha256').update(body).digest('hex').slice(0, 20)}"`,
    });
  }
  return files;
}

// JSON inside <script type="application/json">: neutralise anything that could close the element.
function scriptSafeJson(value) {
  return JSON.stringify(value)
    .replace(/</g, '\\u003c')
    .replace(/>/g, '\\u003e')
    .replace(/&/g, '\\u0026')
    .replace(/\u2028/g, '\\u2028')
    .replace(/\u2029/g, '\\u2029');
}

function send(res, status, body, headers = {}) {
  res.writeHead(status, { ...SECURITY_HEADERS, 'Content-Length': Buffer.byteLength(body), ...headers });
  res.end(body);
}

function sendJson(res, status, obj, headers = {}) {
  send(res, status, JSON.stringify(obj), {
    'Content-Type': 'application/json; charset=utf-8',
    'Cache-Control': 'no-store',
    ...headers,
  });
}

// CSRF guard + body limits for every POST. Order: header (403), Sec-Fetch-Site (403), type (415), size (413).
function checkPostGuards(req) {
  if (req.headers['x-homelab'] !== '1') throw new HttpError(403, 'missing X-Homelab: 1 header');
  const site = req.headers['sec-fetch-site'];
  if (site !== undefined && site !== 'same-origin' && site !== 'none') {
    throw new HttpError(403, 'cross-site request refused');
  }
  const [mediaType, ...params] = String(req.headers['content-type'] ?? '').split(';');
  if (mediaType.trim().toLowerCase() !== 'application/json') {
    throw new HttpError(415, 'Content-Type must be application/json');
  }
  const charset = params.map((p) => p.trim().toLowerCase()).find((p) => p.startsWith('charset='));
  if (charset && !['charset=utf-8', 'charset="utf-8"'].includes(charset)) {
    throw new HttpError(415, 'only utf-8 JSON is accepted');
  }
  const length = req.headers['content-length'];
  if (length !== undefined && Number(length) > MAX_BODY_BYTES) {
    throw new HttpError(413, `body exceeds ${MAX_BODY_BYTES} bytes`, { Connection: 'close' });
  }
}

function readJsonBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    let done = false;
    const fail = (err) => {
      if (done) return;
      done = true;
      reject(err);
    };
    req.on('data', (chunk) => {
      if (done) return;
      size += chunk.length;
      if (size > MAX_BODY_BYTES) {
        fail(new HttpError(413, `body exceeds ${MAX_BODY_BYTES} bytes`, { Connection: 'close' }));
        req.resume(); // drain and discard the rest
        return;
      }
      chunks.push(chunk);
    });
    req.on('error', fail);
    req.on('end', () => {
      if (done) return;
      done = true;
      const text = Buffer.concat(chunks).toString('utf8');
      if (text.trim() === '') return resolve({});
      let value;
      try {
        value = JSON.parse(text);
      } catch {
        return reject(new HttpError(400, 'body is not valid JSON'));
      }
      if (!value || typeof value !== 'object' || Array.isArray(value)) {
        return reject(new HttpError(400, 'body must be a JSON object'));
      }
      resolve(value);
    });
  });
}

export function createApp({
  store,
  vapid,
  webpush,
  log,
  publicDir,
  testCooldownMs = DEFAULT_TEST_COOLDOWN_MS,
  clock = () => Date.now(),
}) {
  const files = loadStatic(publicDir);
  const indexTemplate = files.get('/index.html')?.body.toString('utf8');
  if (!indexTemplate || !indexTemplate.includes(DEVICES_PLACEHOLDER)) {
    throw new Error(`public/index.html is missing or lacks ${DEVICES_PLACEHOLDER}`);
  }
  let lastTestAt = -Infinity;

  function devicesOrError() {
    try {
      return { devices: store.devices(), storeError: false };
    } catch (err) {
      if (!(err instanceof StoreCorruptError)) log.error('store read failed', { code: err.code ?? null });
      return { devices: [], storeError: true };
    }
  }

  function renderIndex(req, res) {
    const html = indexTemplate.replace(DEVICES_PLACEHOLDER, scriptSafeJson(devicesOrError()));
    // Node drops the body on HEAD itself, so Content-Length stays truthful.
    send(res, 200, html, {
      'Content-Type': TYPES['.html'],
      'Cache-Control': 'no-store',
    });
  }

  function serveStatic(req, res, file) {
    const headers = { 'Content-Type': file.type, 'Cache-Control': 'no-cache', ETag: file.etag };
    if (req.headers['if-none-match'] === file.etag) {
      res.writeHead(304, { ...SECURITY_HEADERS, ...headers });
      return res.end();
    }
    res.writeHead(200, { ...SECURITY_HEADERS, ...headers, 'Content-Length': file.body.length });
    res.end(req.method === 'HEAD' ? undefined : file.body);
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

  const api = {
    'GET /healthz': (req, res) => {
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

    'GET /api/vapid-public-key': (req, res) => {
      let keys;
      try {
        keys = vapid.get();
      } catch (err) {
        log.error('vapid keys unavailable', { reason: err.reason ?? null, error: err.message });
        throw new HttpError(503, 'VAPID keys are not configured');
      }
      sendJson(res, 200, { publicKey: keys.publicKey });
    },

    'POST /api/subscribe': async (req, res, body) => {
      let result;
      try {
        result = store.subscribe(body.subscription, body.label);
      } catch (err) {
        throw mapStoreError(err);
      }
      const device = toDevice(result.entry);
      log.info(result.created ? 'subscribed' : 'subscription updated', { device: device.id, label: device.label });
      sendJson(res, result.created ? 201 : 200, {
        ok: true,
        created: result.created,
        device,
        devices: devicesOrError().devices,
      });
    },

    'POST /api/unsubscribe': async (req, res, body) => {
      let result;
      try {
        result = store.unsubscribe(body.endpoint);
      } catch (err) {
        throw mapStoreError(err);
      }
      log.info(result.removed ? 'unsubscribed' : 'unsubscribe: not found', {});
      sendJson(res, 200, { ok: true, removed: result.removed, devices: devicesOrError().devices });
    },

    'POST /api/test': async (req, res) => {
      const wait = lastTestAt + testCooldownMs - clock();
      if (wait > 0) {
        throw new HttpError(429, 'wait before sending another test', {
          'Retry-After': String(Math.ceil(wait / 1000)),
        });
      }
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
      if (entries.length === 0) {
        return sendJson(res, 200, { ok: true, total: 0, sent: 0, failed: 0, removed: 0, devices: [] });
      }
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
      sendJson(res, result.sent > 0 ? 200 : 502, {
        ok: result.sent > 0,
        total: result.total,
        sent: result.sent,
        failed: result.failed,
        removed,
        devices: devicesOrError().devices,
      });
    },
  };

  const apiPaths = new Map();
  for (const key of Object.keys(api)) {
    const [method, p] = key.split(' ');
    if (!apiPaths.has(p)) apiPaths.set(p, []);
    apiPaths.get(p).push(method);
    if (method === 'GET') apiPaths.get(p).push('HEAD');
  }

  async function route(req, res) {
    const pathname = new URL(req.url ?? '/', 'http://localhost').pathname;
    const methods = apiPaths.get(pathname);
    if (methods) {
      if (!methods.includes(req.method)) {
        throw new HttpError(405, 'method not allowed', { Allow: methods.join(', ') });
      }
      const handler = api[`${req.method === 'HEAD' ? 'GET' : req.method} ${pathname}`];
      if (req.method === 'POST') {
        checkPostGuards(req);
        const body = await readJsonBody(req);
        return handler(req, res, body);
      }
      return handler(req, res);
    }
    if (pathname === '/' || pathname === '/index.html' || files.has(pathname)) {
      if (req.method !== 'GET' && req.method !== 'HEAD') {
        throw new HttpError(405, 'method not allowed', { Allow: 'GET, HEAD' });
      }
      if (pathname === '/' || pathname === '/index.html') return renderIndex(req, res);
      return serveStatic(req, res, files.get(pathname));
    }
    throw new HttpError(404, 'not found');
  }

  return async function handle(req, res) {
    const started = process.hrtime.bigint();
    try {
      await route(req, res);
    } catch (err) {
      if (err instanceof HttpError) {
        sendJson(res, err.status, { error: err.message }, err.headers);
      } else {
        log.error('unhandled error', { error: err?.message ?? String(err) });
        if (!res.headersSent) sendJson(res, 500, { error: 'internal error' });
        else res.destroy();
      }
    } finally {
      const pathname = new URL(req.url ?? '/', 'http://localhost').pathname;
      if (!(pathname === '/healthz' && res.statusCode === 200)) {
        log.info('request', {
          method: req.method,
          path: pathname.slice(0, 128),
          status: res.statusCode,
          ms: Number((process.hrtime.bigint() - started) / 1000n) / 1000,
        });
      }
    }
  };
}
