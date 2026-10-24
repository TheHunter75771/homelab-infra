// Access is decided server-side, never by the client (CONTRACTS §11.3, ADR 0014).
//
// v2.0: remote_enabled=false, so every request is "tailnet" (the only way in is `tailscale serve`).
// The remote path is built but off. When enabled (needs its own ADR + tunnel route + Access app):
//   - a request that came through Cloudflare (Cf-Ray / Cf-Connecting-Ip present) must carry a valid
//     Cf-Access-Jwt-Assertion (RS256, aud = configured audience, iss = https://<team domain>, not expired),
//     and is "remote"; without a valid token it is refused (403)
//   - anything else came through tailscale serve and is "tailnet"
import { createPublicKey, verify as cryptoVerify } from 'node:crypto';

export class AccessDenied extends Error {
  constructor(message) {
    super(message);
    this.name = 'AccessDenied';
  }
}

const b64json = (part) => JSON.parse(Buffer.from(part, 'base64url').toString('utf8'));

export function verifyAccessJwt(token, { keys, audience, issuer, now = Date.now(), skewSec = 60 }) {
  const parts = String(token).split('.');
  if (parts.length !== 3) throw new AccessDenied('malformed token');
  let header;
  let payload;
  try {
    header = b64json(parts[0]);
    payload = b64json(parts[1]);
  } catch {
    throw new AccessDenied('malformed token');
  }
  if (header.alg !== 'RS256') throw new AccessDenied('unexpected algorithm');
  const jwk = (keys ?? []).find((k) => k.kid === header.kid);
  if (!jwk) throw new AccessDenied('unknown signing key');
  const ok = cryptoVerify('RSA-SHA256', Buffer.from(`${parts[0]}.${parts[1]}`), createPublicKey({ key: jwk, format: 'jwk' }), Buffer.from(parts[2], 'base64url'));
  if (!ok) throw new AccessDenied('bad signature');
  const aud = Array.isArray(payload.aud) ? payload.aud : [payload.aud];
  if (!audience || !aud.includes(audience)) throw new AccessDenied('wrong audience');
  if (payload.iss !== issuer) throw new AccessDenied('wrong issuer');
  const t = now / 1000;
  if (!Number.isFinite(payload.exp) || payload.exp < t - skewSec) throw new AccessDenied('expired');
  if (Number.isFinite(payload.nbf) && payload.nbf > t + skewSec) throw new AccessDenied('not yet valid');
  return payload;
}

export function createAccess({ cfg, fetchImpl = globalThis.fetch, clock = () => Date.now() }) {
  let keyCache = null; // {keys, at}

  async function signingKeys() {
    if (keyCache && clock() - keyCache.at < 3_600_000) return keyCache.keys;
    const res = await fetchImpl(`https://${cfg.accessJwt.teamDomain}/cdn-cgi/access/certs`, { signal: AbortSignal.timeout(5000) });
    if (!res.ok) throw new AccessDenied('cannot fetch Access signing keys');
    const body = await res.json();
    keyCache = { keys: Array.isArray(body?.keys) ? body.keys : [], at: clock() };
    return keyCache.keys;
  }

  return {
    // -> 'tailnet' | 'remote'; throws AccessDenied
    async decide(req) {
      if (!cfg.remoteEnabled) return 'tailnet';
      const h = req.headers;
      const viaCloudflare = h['cf-ray'] !== undefined || h['cf-connecting-ip'] !== undefined;
      if (!viaCloudflare) return 'tailnet';
      const token = h['cf-access-jwt-assertion'];
      if (!token || !cfg.accessJwt.teamDomain || !cfg.accessJwt.audience) throw new AccessDenied('Cloudflare Access token required');
      verifyAccessJwt(token, {
        keys: await signingKeys(),
        audience: cfg.accessJwt.audience,
        issuer: `https://${cfg.accessJwt.teamDomain}`,
        now: clock(),
      });
      return 'remote';
    },
  };
}
