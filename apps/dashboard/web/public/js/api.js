// HTTP + SSE against the dashboard server (CONTRACTS §11.3). Same origin only. Every non-GET sends
// X-Homelab: 1 and a JSON body (the server's CSRF guard). SSE is read with fetch streaming, so the
// same code runs in every browser and in node:test, and reconnects are under our control.

export class ApiError extends Error {
  constructor(status, message, data) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.data = data;
  }
}

function withTimeout(signal, ms) {
  const ctl = new AbortController();
  const timer = ms ? setTimeout(() => ctl.abort(new DOMException('timed out', 'TimeoutError')), ms) : null;
  const onAbort = () => ctl.abort(signal.reason);
  if (signal) {
    if (signal.aborted) ctl.abort(signal.reason);
    else signal.addEventListener('abort', onAbort, { once: true });
  }
  return {
    signal: ctl.signal,
    done() {
      if (timer) clearTimeout(timer);
      if (signal) signal.removeEventListener('abort', onAbort);
    },
  };
}

async function readJson(res) {
  const text = await res.text();
  if (!text) return {};
  try {
    return JSON.parse(text);
  } catch {
    return { error: text.slice(0, 200) };
  }
}

// GET JSON. Resolves { status, data, etag }; a 304 has data null. Throws ApiError on other statuses
// and TypeError/AbortError when the server can't be reached.
export async function getJSON(path, { signal, etag, timeoutMs = 8000 } = {}) {
  const t = withTimeout(signal, timeoutMs);
  try {
    const headers = { Accept: 'application/json' };
    if (etag) headers['If-None-Match'] = etag;
    const res = await fetch(path, { headers, cache: 'no-store', credentials: 'same-origin', signal: t.signal });
    const generatedAt = res.headers.get('X-Generated-At');
    if (res.status === 304) return { status: 304, data: null, etag, generatedAt };
    const data = await readJson(res);
    if (!res.ok) throw new ApiError(res.status, data.error || `Server answered ${res.status}`, data);
    return { status: res.status, data, etag: res.headers.get('ETag'), generatedAt };
  } finally {
    t.done();
  }
}

export async function send(method, path, body = {}, { signal, timeoutMs = 20000 } = {}) {
  const t = withTimeout(signal, timeoutMs);
  try {
    const res = await fetch(path, {
      method,
      headers: { 'Content-Type': 'application/json', 'X-Homelab': '1', Accept: 'application/json' },
      body: JSON.stringify(body ?? {}),
      cache: 'no-store',
      credentials: 'same-origin',
      signal: t.signal,
    });
    const data = await readJson(res);
    if (!res.ok) throw new ApiError(res.status, data.error || data.message || `Server answered ${res.status}`, data);
    return data;
  } finally {
    t.done();
  }
}

export const post = (path, body, opts) => send('POST', path, body, opts);
export const put = (path, body, opts) => send('PUT', path, body, opts);
export const del = (path, body, opts) => send('DELETE', path, body, opts);

// Incremental text/event-stream parser (WHATWG rules: \n, \r\n or \r line ends; data lines joined
// with \n; a blank line dispatches; lines starting with ":" are comments). Comments are reported
// as { type: 'comment' } so heartbeats count as contact.
export function createSSEParser(onEvent) {
  let buf = '';
  let data = [];
  let type = '';
  let id = null;
  let sawCR = false;
  function line(l) {
    if (l === '') {
      if (data.length) onEvent({ type: type || 'message', data: data.join('\n'), id });
      data = [];
      type = '';
      return;
    }
    if (l.startsWith(':')) {
      onEvent({ type: 'comment', data: l.slice(1).trim(), id });
      return;
    }
    const i = l.indexOf(':');
    const field = i === -1 ? l : l.slice(0, i);
    let value = i === -1 ? '' : l.slice(i + 1);
    if (value.startsWith(' ')) value = value.slice(1);
    if (field === 'data') data.push(value);
    else if (field === 'event') type = value;
    else if (field === 'id' && !value.includes('\0')) id = value;
  }
  return {
    push(chunk) {
      let text = chunk;
      if (sawCR && text.startsWith('\n')) text = text.slice(1);
      sawCR = false;
      buf += text;
      let m;
      const re = /\r\n|\r|\n/g;
      let start = 0;
      while ((m = re.exec(buf))) {
        if (m[0] === '\r' && m.index === buf.length - 1) {
          sawCR = true; // might be the first half of \r\n split across chunks
        }
        line(buf.slice(start, m.index));
        start = m.index + m[0].length;
      }
      buf = buf.slice(start);
    },
    end() {
      if (buf) line(buf);
      buf = '';
      line('');
    },
  };
}

// Read an SSE stream until it ends. onOpen fires when the response headers arrive.
// Resolves when the server closes the stream; rejects on HTTP errors or network failure.
export async function streamSSE(path, { signal, onEvent, onOpen, lastEventId } = {}) {
  const headers = { Accept: 'text/event-stream' };
  if (lastEventId) headers['Last-Event-ID'] = lastEventId;
  const res = await fetch(path, {
    headers,
    cache: 'no-store',
    credentials: 'same-origin',
    signal,
  });
  if (!res.ok) {
    const data = await readJson(res).catch(() => ({}));
    throw new ApiError(res.status, data.error || `Server answered ${res.status}`, data);
  }
  if (!res.body || typeof res.body.getReader !== 'function') throw new ApiError(0, 'streaming is not supported here');
  onOpen?.(res);
  const parser = createSSEParser((ev) => onEvent?.(ev));
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      parser.push(decoder.decode(value, { stream: true }));
    }
    parser.push(decoder.decode());
    parser.end();
  } finally {
    try {
      reader.releaseLock();
    } catch {
      // already released
    }
  }
}

export function parseJSON(text) {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

export const appPath = (name, verb = '') => `/api/apps/${encodeURIComponent(name)}${verb ? `/${verb}` : ''}`;
