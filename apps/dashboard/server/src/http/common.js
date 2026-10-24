// Shared HTTP pieces: security headers, errors, JSON responses and the write guards (migrated from push-core).
//
// Guards on every non-GET request (CONTRACTS §11.3, unchanged from push-core), checked in this order:
//   X-Homelab: 1 (403) -> Sec-Fetch-Site same-origin|none|absent (403) -> Content-Type application/json, utf-8
//   (415) -> body <= 8 KB (413, declared or streamed) -> a JSON object (400; an empty body counts as {}).
export const MAX_BODY_BYTES = 8 * 1024;

export const CSP =
  "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self'; " +
  "manifest-src 'self'; worker-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'";

export const SECURITY_HEADERS = {
  'Content-Security-Policy': CSP,
  'X-Content-Type-Options': 'nosniff',
  'Referrer-Policy': 'no-referrer',
  'X-Frame-Options': 'DENY',
  'Cross-Origin-Opener-Policy': 'same-origin',
  'Cross-Origin-Resource-Policy': 'same-origin',
};

export class HttpError extends Error {
  constructor(status, message, headers = {}) {
    super(message);
    this.status = status;
    this.headers = headers;
  }
}

// Request target -> URL. Only origin-form paths are accepted: "/" followed by anything but a second "/", no
// backslash, and percent-escapes that decode. Absolute-form ("http://x/"), "*", "//x" and "/\\x" are 400s.
export function parseTarget(raw) {
  if (typeof raw !== 'string' || !raw.startsWith('/') || raw.startsWith('//') || raw.includes('\\')) {
    throw new HttpError(400, 'bad request target');
  }
  let url;
  try {
    url = new URL(raw, 'http://localhost');
    decodeURIComponent(url.pathname);
  } catch {
    throw new HttpError(400, 'bad request target');
  }
  return url;
}

export function send(res, status, body, headers = {}) {
  res.writeHead(status, { ...SECURITY_HEADERS, 'Content-Length': Buffer.byteLength(body), ...headers });
  res.end(body);
}

export function sendJson(res, status, obj, headers = {}) {
  send(res, status, JSON.stringify(obj), {
    'Content-Type': 'application/json; charset=utf-8',
    'Cache-Control': 'no-store',
    ...headers,
  });
}

// /api/* GETs (CONTRACTS §11.3 Guards, amended): another site must not be able to make the admin's browser drive
// helper calls. Missing, same-origin and none pass; same-site and cross-site get 403. The shell and static files
// are exempt.
export function checkReadGuards(req) {
  const site = req.headers['sec-fetch-site'];
  if (site !== undefined && site !== 'same-origin' && site !== 'none') throw new HttpError(403, 'cross-site request refused');
}

export function checkWriteGuards(req) {
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

export function readJsonBody(req) {
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
        req.resume();
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

// Server-sent events (CONTRACTS §11.3 "Resource limits").
// - Keepalive comments every 15 s keep `tailscale serve` from dropping an idle stream.
// - Backpressure: send() returns false when the socket wants the writer to wait (use onDrain to resume). A
//   client that stops reading is dropped (its response destroyed, its resources released) once more than
//   `maxBuffer` (about 1 MB) is queued for it.
// - Closers registered after the stream closed run at once, and every closer runs exactly once, so a slot or a
//   helper stream can never outlive its client.
export const SSE_MAX_BUFFER = 1024 * 1024;
export function openSse(req, res, { retryMs = 10_000, keepaliveMs = 15_000, maxBuffer = SSE_MAX_BUFFER, log, label = 'sse', registry } = {}) {
  let closed = false;
  const closers = [];
  const entry = { res, label };
  const runClosers = () => {
    for (const fn of closers.splice(0)) {
      try {
        fn();
      } catch {
        // a broken closer must not stop the others
      }
    }
  };
  const cleanup = () => {
    if (closed) return;
    closed = true;
    clearInterval(ka);
    registry?.delete(entry);
    runClosers();
  };
  const drop = () => {
    log?.warn('sse client too slow; dropped', { stream: label, buffered: res.writableLength });
    cleanup();
    res.destroy();
  };
  const write = (text) => {
    if (closed) return false;
    const ok = res.write(text);
    if (res.writableLength > maxBuffer) {
      drop();
      return false;
    }
    return ok;
  };
  res.writeHead(200, {
    ...SECURITY_HEADERS,
    'Content-Type': 'text/event-stream; charset=utf-8',
    'Cache-Control': 'no-store',
    Connection: 'keep-alive',
    'X-Accel-Buffering': 'no',
  });
  registry?.add(entry);
  const ka = setInterval(() => write(': ping\n\n'), keepaliveMs);
  ka.unref();
  req.on('close', cleanup);
  res.on('close', cleanup);
  res.on('error', cleanup);
  if (res.destroyed || req.socket?.destroyed) queueMicrotask(cleanup); // the client left before we got here
  write(`retry: ${retryMs}\n\n`);
  return {
    get closed() {
      return closed;
    },
    // false: closed, dropped, or the socket asked to wait (see onDrain)
    send(event, data, id) {
      let out = `event: ${event}\n`;
      if (id) out += `id: ${id}\n`;
      for (const line of JSON.stringify(data).split('\n')) out += `data: ${line}\n`;
      return write(`${out}\n`);
    },
    onDrain(fn) {
      res.on('drain', fn);
    },
    onClose(fn) {
      if (closed) {
        try {
          fn();
        } catch {
          // ignore
        }
      } else closers.push(fn);
    },
    end() {
      cleanup();
      res.end();
    },
  };
}
