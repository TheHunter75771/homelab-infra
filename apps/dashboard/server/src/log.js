// JSON-lines logger to stdout. Never pass endpoints, keys or request bodies as fields:
// use deviceId()/redactEndpoint() instead.
import { createHash } from 'node:crypto';
import { redactCredentials } from './redact.js';

const LEVELS = { debug: 10, info: 20, warn: 30, error: 40 };

// Stable, non-reversible short id for a subscription endpoint. The PWA computes the same
// value in the browser (SHA-256, first 16 hex chars) to recognise "this device".
export function deviceId(endpoint) {
  return createHash('sha256').update(String(endpoint)).digest('hex').slice(0, 16);
}

// Safe-to-log form of an endpoint: push service host plus the device id.
export function redactEndpoint(endpoint) {
  let host = 'invalid-url';
  try {
    host = new URL(String(endpoint)).host || host;
  } catch {
    // keep placeholder
  }
  return `${host}#${deviceId(endpoint)}`;
}

// Shorten free text from push services and strip anything URL-shaped from it.
export function sanitizeText(text, max = 160) {
  if (text === undefined || text === null) return null;
  const s = String(text)
    .replace(/https?:\/\/\S+/gi, '<url>')
    .replace(/[\u0000-\u001f\u007f]+/g, ' ')
    .trim();
  return s.length > max ? `${s.slice(0, max)}…` : s;
}

// Every value passes through a redactor on its way out: credential shapes by default (apikey=, API Key:, tokens,
// bearer), and once the dashboard is built, also every configured key (setRedactor). Redaction runs on each value
// BEFORE JSON.stringify, so the escaping is always JSON's own and every line stays valid JSON. The walk:
//   - fields named like credentials (token, apiKey, password, authorization, ...) are replaced by name
//   - cycle-safe ("[circular]"); past MAX_DEPTH levels a value becomes "[truncated]"
//   - Buffers / typed arrays become "[binary N bytes]" (never their contents), BigInt a string, Error
//     {name, message, stack} (redacted), Date its ISO string, Map an object, Set an array
export const MAX_DEPTH = 32;
const SECRET_KEY_RE =
  /^(?:x[-_]?)?(?:api[-_]?key|apikey|access[-_]?token|refresh[-_]?token|auth[-_]?token|token|secret|client[-_]?secret|password|passwd|passphrase|authorization|cookie|set[-_]?cookie|private[-_]?key|privatekey|session[-_]?id|credentials?)$/i;

function redactValue(value, redactor, depth = 0, seen = new WeakSet()) {
  if (typeof value === 'string') return redactor(value);
  if (typeof value === 'bigint') return value.toString();
  if (value === null || typeof value !== 'object') return value;
  if (depth >= MAX_DEPTH) return '[truncated]';
  if (Buffer.isBuffer(value) || ArrayBuffer.isView(value)) return `[binary ${value.byteLength} bytes]`;
  if (value instanceof ArrayBuffer) return `[binary ${value.byteLength} bytes]`;
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value.toISOString();
  if (seen.has(value)) return '[circular]';
  seen.add(value);
  try {
    if (value instanceof Error) {
      return { name: redactor(String(value.name)), message: redactor(String(value.message)), stack: redactor(String(value.stack ?? '')) };
    }
    if (Array.isArray(value) || value instanceof Set) return [...value].map((v) => redactValue(v, redactor, depth + 1, seen));
    const entries = value instanceof Map ? [...value.entries()].map(([k, v]) => [String(k), v]) : Object.entries(value);
    const out = {};
    for (const [k, v] of entries) out[k] = SECRET_KEY_RE.test(k) && v !== null && v !== undefined ? '[redacted]' : redactValue(v, redactor, depth + 1, seen);
    return out;
  } finally {
    seen.delete(value); // the same object twice (not a cycle) is fine
  }
}

export function createLogger({ level = 'info', write, redact = redactCredentials } = {}) {
  const min = LEVELS[level] ?? LEVELS.info;
  const out = write ?? ((line) => process.stdout.write(`${line}\n`));
  let redactor = redact;
  const emit = (lvl, msg, fields) => {
    if (LEVELS[lvl] < min) return;
    let line;
    try {
      line = JSON.stringify({ ts: new Date().toISOString(), level: lvl, msg: redactor(String(msg)), ...redactValue(fields ?? {}, redactor) });
    } catch (err) {
      // never let a log call throw: say so, without the fields
      line = JSON.stringify({ ts: new Date().toISOString(), level: lvl, msg: redactor(String(msg)), logError: String(err?.message ?? err).slice(0, 200) });
    }
    out(line);
  };
  return {
    debug: (msg, fields) => emit('debug', msg, fields),
    info: (msg, fields) => emit('info', msg, fields),
    warn: (msg, fields) => emit('warn', msg, fields),
    error: (msg, fields) => emit('error', msg, fields),
    setRedactor(fn) {
      redactor = (text) => redactCredentials(fn(text));
    },
    redact: (text) => redactor(String(text)),
  };
}

export const silentLogger = createLogger({ write: () => {} });
