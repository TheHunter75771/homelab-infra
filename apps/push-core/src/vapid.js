// Loads ${PUSH_DIR}/vapid.json (contract 4): {"publicKey": b64url, "privateKey": b64url, "subject": str}.
// Re-read when the file changes on disk (content, inode or permissions), so a key rotation needs no restart.
import nodeFs from 'node:fs';

export class VapidError extends Error {
  constructor(message, reason) {
    super(message);
    this.name = 'VapidError';
    this.reason = reason; // 'missing' | 'unreadable' | 'invalid'
  }
}

const B64URL = /^[A-Za-z0-9_-]+$/;

function decodeB64url(value) {
  if (typeof value !== 'string' || !B64URL.test(value)) return null;
  return Buffer.from(value, 'base64url');
}

// Messages never include key material.
export function parseVapid(text) {
  let obj;
  try {
    obj = JSON.parse(text);
  } catch {
    throw new VapidError('vapid.json is not valid JSON', 'invalid');
  }
  if (!obj || typeof obj !== 'object' || Array.isArray(obj)) {
    throw new VapidError('vapid.json must be a JSON object', 'invalid');
  }
  const { publicKey, privateKey, subject } = obj;
  const pub = decodeB64url(publicKey);
  if (!pub || pub.length !== 65 || pub[0] !== 0x04) {
    throw new VapidError('vapid.json publicKey must be a base64url uncompressed P-256 point (65 bytes)', 'invalid');
  }
  const priv = decodeB64url(privateKey);
  if (!priv || priv.length !== 32) {
    throw new VapidError('vapid.json privateKey must be a base64url P-256 private key (32 bytes)', 'invalid');
  }
  if (typeof subject !== 'string' || !/^(mailto:|https:\/\/)\S+$/.test(subject)) {
    throw new VapidError('vapid.json subject must be a mailto: or https:// URL', 'invalid');
  }
  return Object.freeze({ publicKey, privateKey, subject });
}

export function createVapidSource({ file, fs = nodeFs }) {
  let cache = null;
  return {
    file,
    get() {
      let st;
      try {
        st = fs.statSync(file);
      } catch (err) {
        if (err.code === 'ENOENT') throw new VapidError(`${file} is missing`, 'missing');
        throw new VapidError(`${file} cannot be read (${err.code})`, 'unreadable');
      }
      // ctime covers chmod/chown, so a permission regression surfaces in /healthz right away.
      const unchanged =
        cache &&
        cache.mtimeMs === st.mtimeMs &&
        cache.ctimeMs === st.ctimeMs &&
        cache.size === st.size &&
        cache.ino === st.ino;
      if (unchanged) return cache.value;
      let text;
      try {
        text = fs.readFileSync(file, 'utf8');
      } catch (err) {
        throw new VapidError(`${file} cannot be read (${err.code})`, 'unreadable');
      }
      const value = parseVapid(text);
      cache = { mtimeMs: st.mtimeMs, ctimeMs: st.ctimeMs, size: st.size, ino: st.ino, value };
      return value;
    },
  };
}
