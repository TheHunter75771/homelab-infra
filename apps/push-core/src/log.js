// JSON-lines logger to stdout. Never pass endpoints, keys or request bodies as fields:
// use deviceId()/redactEndpoint() instead.
import { createHash } from 'node:crypto';

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

export function createLogger({ level = 'info', write } = {}) {
  const min = LEVELS[level] ?? LEVELS.info;
  const out = write ?? ((line) => process.stdout.write(`${line}\n`));
  const emit = (lvl, msg, fields) => {
    if (LEVELS[lvl] < min) return;
    out(JSON.stringify({ ts: new Date().toISOString(), level: lvl, msg, ...fields }));
  };
  return {
    debug: (msg, fields) => emit('debug', msg, fields),
    info: (msg, fields) => emit('info', msg, fields),
    warn: (msg, fields) => emit('warn', msg, fields),
    error: (msg, fields) => emit('error', msg, fields),
  };
}

export const silentLogger = createLogger({ write: () => {} });
