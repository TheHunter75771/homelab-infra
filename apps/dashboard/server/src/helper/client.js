// Client for the root helper `homelab-dockerctl` (CONTRACTS §11.2; module H's
// roles/dashboard/files/homelab-dockerctl/homelab_dockerctl.py) on /run/homelab-dockerctl.sock.
//
// Wire protocol (one request per connection), as the helper implements it:
//   request : one JSON object on one line (<= 4 KiB), only the fields the op accepts
//             {"op": "ps"} | {"op": "logs", "name": "sonarr", "tail": 500, "follow": true, "since": "<RFC 3339>"}
//   unary   : {"ok": true, "op": OP, "data": {...}}  |  {"ok": false, "error": {"code": CODE, "message": TEXT}}
//   stream  : first line {"ok": true, "op": OP, "stream": true, ...} (or the error line above), then one JSON
//             event per line ({"type": "line" | "step" | "output", ...}), always ending with
//             {"type": "end", "ok": BOOL, "reason": TEXT, "message"?: TEXT}. Closing our side cancels a stream.
//
// The hard rule (ADR 0013): no Docker operation is sent unless a fresh `docker-state` says "active". Touching the
// Docker socket while docker.service is down would socket-activate Docker and re-run the boot gate. The helper
// enforces the same rule; the dashboard never relies on that alone.
import net from 'node:net';

export const UNARY_OPS = new Set(['ps', 'start', 'stop', 'restart', 'docker-state', 'last-pull', 'disk-split', 'boots']);
export const STREAM_OPS = new Set(['logs', 'update']);
export const DOCKER_OPS = new Set(['ps', 'start', 'stop', 'restart', 'logs', 'update']);
const MAX_LINE_BYTES = 1024 * 1024;

export class HelperError extends Error {
  constructor(code, message) {
    super(message);
    this.name = 'HelperError';
    this.code = code; // unavailable | timeout | protocol | the helper's code (held-by-gate, busy, failed, ...)
  }
}

export class DockerInactiveError extends Error {
  constructor(state) {
    super(`docker.service is ${state}`);
    this.name = 'DockerInactiveError';
    this.state = state;
  }
}

function connectError(err) {
  if (['ENOENT', 'ECONNREFUSED', 'EACCES', 'ENOTSOCK'].includes(err.code)) {
    return new HelperError('unavailable', `helper socket unavailable (${err.code})`);
  }
  return new HelperError('unavailable', err.message);
}

function helperError(error, op) {
  const code = typeof error?.code === 'string' ? error.code : 'refused';
  const message = typeof error?.message === 'string' ? error.message : `helper refused ${op}`;
  return code === 'docker-inactive' ? new DockerInactiveError('inactive') : new HelperError(code, message);
}

// Line splitter for a socket: calls onLine(obj) per JSON line.
function lineReader(socket, onLine, onBad) {
  let buf = '';
  socket.setEncoding('utf8');
  socket.on('data', (chunk) => {
    buf += chunk;
    if (buf.length > MAX_LINE_BYTES && !buf.includes('\n')) {
      onBad(new HelperError('protocol', 'helper line too long'));
      return;
    }
    let i;
    while ((i = buf.indexOf('\n')) !== -1) {
      const line = buf.slice(0, i);
      buf = buf.slice(i + 1);
      if (!line.trim()) continue;
      let obj;
      try {
        obj = JSON.parse(line);
      } catch {
        onBad(new HelperError('protocol', 'helper sent invalid JSON'));
        return;
      }
      onLine(obj);
    }
  });
}

export function createHelperClient({ socketPath, timeoutMs = 10_000, connect = net.createConnection } = {}) {
  function request(op, args = {}, { timeout = timeoutMs } = {}) {
    if (!UNARY_OPS.has(op)) return Promise.reject(new HelperError('protocol', `unknown unary op ${op}`));
    return new Promise((resolve, reject) => {
      let settled = false;
      const socket = connect({ path: socketPath });
      const finish = (err, value) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        socket.destroy();
        if (err) reject(err);
        else resolve(value);
      };
      const timer = setTimeout(() => finish(new HelperError('timeout', `helper ${op} timed out after ${timeout} ms`)), timeout);
      socket.on('connect', () => socket.write(`${JSON.stringify({ op, ...args })}\n`));
      socket.on('error', (err) => finish(connectError(err)));
      lineReader(
        socket,
        (msg) => {
          if (msg.ok === true && msg.data !== undefined) finish(null, msg.data);
          else if (msg.ok === false) finish(helperError(msg.error, op));
          else finish(new HelperError('protocol', 'helper reply lacks "ok"/"data"'));
        },
        (err) => finish(err),
      );
      socket.on('close', () => finish(new HelperError('protocol', `helper closed without answering ${op}`)));
    });
  }

  // Resolves with the end event ({type: "end", ok, reason, message?}); {ok: true, aborted: true} when `signal`
  // aborted (that closes the connection, which cancels the stream on the helper side); {ok: false, reason:
  // "closed"} when the helper went away without an end event. Rejects only if the stream never started.
  // `flow`, if given, gets pause()/resume(): pausing stops reading the socket, so the helper (not this process)
  // holds the backlog while a slow client catches up.
  function stream(op, args, { onItem, onStart, signal, timeout = 0, flow } = {}) {
    if (!STREAM_OPS.has(op)) return Promise.reject(new HelperError('protocol', `unknown stream op ${op}`));
    return new Promise((resolve, reject) => {
      let settled = false;
      let started = false;
      const socket = connect({ path: socketPath });
      if (flow) {
        flow.pause = () => !settled && socket.pause();
        flow.resume = () => !settled && socket.resume();
      }
      let timer = null;
      const finish = (err, value) => {
        if (settled) return;
        settled = true;
        if (timer) clearTimeout(timer);
        signal?.removeEventListener('abort', onAbort);
        socket.destroy();
        if (err) reject(err);
        else resolve(value);
      };
      const onAbort = () => finish(null, { type: 'end', ok: true, reason: 'aborted', aborted: true });
      if (signal?.aborted) return onAbort();
      signal?.addEventListener('abort', onAbort, { once: true });
      if (timeout > 0) timer = setTimeout(() => finish(null, { type: 'end', ok: false, reason: 'timeout', message: `helper ${op} timed out` }), timeout);
      socket.on('connect', () => socket.write(`${JSON.stringify({ op, ...args })}\n`));
      socket.on('error', (err) => (started ? finish(null, { type: 'end', ok: false, reason: 'closed' }) : finish(connectError(err))));
      lineReader(
        socket,
        (msg) => {
          if (!started) {
            if (msg.ok === true && msg.stream === true) {
              started = true;
              onStart?.(msg);
            } else if (msg.ok === false) finish(helperError(msg.error, op));
            else finish(new HelperError('protocol', 'helper stream did not start'));
            return;
          }
          if (msg.type === 'end') finish(null, { ...msg, ok: msg.ok === true });
          else onItem?.(msg);
        },
        (err) => (started ? finish(null, { type: 'end', ok: false, reason: 'protocol', message: err.message }) : finish(err)),
      );
      socket.on('close', () =>
        started ? finish(null, { type: 'end', ok: false, reason: 'closed' }) : finish(new HelperError('protocol', `helper closed without answering ${op}`)),
      );
    });
  }

  return { request, stream, socketPath };
}

const str = (v) => (typeof v === 'string' && v ? v : null);

// ps container -> the dashboard's shape
function normContainer(c) {
  return {
    name: c.name,
    state: str(c.state),
    health: str(c.health),
    started_at: str(c.started_at),
    restart_count: Number.isInteger(c.restart_count) ? c.restart_count : null,
    image: str(c.image),
    pinned: str(c.pinned),
    stack: str(c.stack),
  };
}

function normDeclared(d) {
  return { name: d.name, stack: str(d.stack), service: str(d.service), pinned: str(d.pinned), local_build: d.local_build === true };
}

// Typed operations on top of the client. Every Docker op is preceded by its own docker-state request (no
// caching), so the window between "Docker went down" and "we stop asking" is one round trip, not a cache TTL.
export function createDocker({ helper }) {
  async function dockerState() {
    const data = await helper.request('docker-state');
    const state = typeof data === 'string' ? data : data?.state;
    if (typeof state !== 'string') throw new HelperError('protocol', 'docker-state returned no state');
    return state;
  }

  async function guard() {
    const state = await dockerState();
    if (state !== 'active') throw new DockerInactiveError(state);
  }

  // -> {containers: [...], declared: [...] | null}
  async function psFull() {
    await guard();
    const data = await helper.request('ps');
    const list = Array.isArray(data) ? data : data?.containers;
    if (!Array.isArray(list)) throw new HelperError('protocol', 'ps returned no container list');
    const containers = list.filter((c) => c && typeof c.name === 'string').map(normContainer);
    const declared = Array.isArray(data?.declared) ? data.declared.filter((d) => d && typeof d.name === 'string').map(normDeclared) : null;
    return { containers, declared };
  }

  return {
    dockerState,
    psFull,
    ps: async () => (await psFull()).containers,

    async control(op, name) {
      if (!['start', 'stop', 'restart'].includes(op)) throw new HelperError('protocol', `bad op ${op}`);
      await guard();
      return helper.request(op, { name }, { timeout: 200_000 }); // the helper's action_timeout is 180 s
    },

    // onStep gets {step, at, message?, failed_step?}; the final step is "done" or "failed".
    async update(name, { onStep, signal } = {}) {
      await guard();
      return helper.stream('update', { name }, { onItem: (m) => m.type === 'step' && onStep?.(m), signal, timeout: 30 * 60_000 });
    },

    // onLine gets {ts, text}; `flow` gets pause()/resume() (backpressure)
    async logs(name, { tail = 500, follow = false, since = null } = {}, { onLine, signal, flow } = {}) {
      await guard();
      const args = { name, tail, follow };
      if (since) args.since = new Date(since).toISOString();
      return helper.stream('logs', args, { onItem: (m) => m.type === 'line' && onLine?.(m), signal, flow });
    },

    // Not Docker operations: these never touch the Docker socket on the helper side.
    async lastPull() {
      const d = await helper.request('last-pull');
      return { lastPull: str(d?.last_pull), lastAttempt: str(d?.last_attempt) };
    },
    async diskSplit() {
      const d = await helper.request('disk-split', {}, { timeout: 60_000 });
      const s = d?.sizes ?? {};
      return { tv: s.tv ?? null, movies: s.movies ?? null, downloads: s.downloads ?? null, computedAt: str(d?.computed_at), stale: d?.stale === true };
    },
    async boots() {
      const d = await helper.request('boots');
      return (Array.isArray(d?.boots) ? d.boots : []).map((b) => ({ bootId: str(b.boot_id), first: str(b.first_entry), last: str(b.last_entry) }));
    },
  };
}
