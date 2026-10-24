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

// Every line passes through a redactor on its way out: credential shapes by default (apikey=, API Key:, tokens,
// bearer), and once the dashboard is built, also every configured key (setRedactor). Redaction runs on the whole
// serialized line, so no field (an error message, a stack) can carry a key past it.
export function createLogger({ level = 'info', write, redact = redactCredentials } = {}) {
  const min = LEVELS[level] ?? LEVELS.info;
  const out = write ?? ((line) => process.stdout.write(`${line}\n`));
  let redactor = redact;
  const emit = (lvl, msg, fields) => {
    if (LEVELS[lvl] < min) return;
    out(redactor(JSON.stringify({ ts: new Date().toISOString(), level: lvl, msg, ...fields })));
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
