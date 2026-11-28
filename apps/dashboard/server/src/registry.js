// Registry client (CONTRACTS §11.7.2): the newest stable release of a pinned image, looked up anonymously.
//
// latest(pin, channel = 'latest'):
//   1. GET  /v2/<repo>/manifests/<channel>   (Accept: OCI index, Docker list, OCI manifest, Docker v2)
//      digest = Docker-Content-Digest, which must equal sha256 of the body
//   2. an index: the linux/amd64 entry's manifest (content-addressed: sha256 of its body = its digest)
//   3. GET  /v2/<repo>/blobs/<config digest>  -> label org.opencontainers.image.version
//   4. HEAD /v2/<repo>/manifests/<version>    -> Docker-Content-Digest must equal the digest from step 1
//   -> {version, digest, pin: "<repo as pinned>:<version>@<digest>"}
//
// Auth: anonymous Bearer from the WWW-Authenticate challenge (the realm must be https:// on a public host name),
// cached per scope until expires_in, at most 300 s. Redirect targets must be public host names too. HTTPS only, every hop: redirects (blob GETs go to a CDN) are followed here, at most 5, each one
// https, and the bearer is only ever sent to the registry's own origin. 15 s per request (body included); body caps
// 1 MiB for manifests and 2 MiB for config blobs.
//
// Errors are RegistryError with a short reason meant for the browser: never a URL, a header or a token.
// The server is unprivileged and the helper only pulls by digest, so Docker verifies whatever comes back here.
import { createHash } from 'node:crypto';

export const MANIFEST_CAP = 1024 * 1024;
export const CONFIG_CAP = 2 * 1024 * 1024;
export const REQUEST_TIMEOUT_MS = 15_000;
export const VERSION_LABEL = 'org.opencontainers.image.version';
export const TAG_RE = /^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$/;
const DIGEST_RE = /^sha256:[0-9a-f]{64}$/;
const MAX_REDIRECTS = 5;
export const TOKEN_TTL_CAP_S = 300;

// Token realms and redirect targets must be public names: no IP literals, no localhost, no private-only
// suffixes (mDNS, split DNS, tailnet names) and no single-label hosts.
const PRIVATE_SUFFIXES = ['.localhost', '.local', '.internal', '.lan', '.home.arpa', '.ts.net', '.intranet', '.corp'];
export function isPublicHost(hostname) {
  const h = String(hostname ?? '').toLowerCase().replace(/\.$/, '');
  if (!h || h === 'localhost' || !h.includes('.')) return false;
  if (h.startsWith('[') || h.includes(':')) return false; // IPv6 literal
  if (/^[0-9.]+$/.test(h)) return false; // IPv4 literal (the URL parser normalises hex/octal forms)
  return !PRIVATE_SUFFIXES.some((s) => h.endsWith(s));
}
const DOCKER_HUB = 'registry-1.docker.io';
const DOCKER_HUB_ALIASES = new Set(['docker.io', 'index.docker.io', 'registry-1.docker.io']);

const OCI_INDEX = 'application/vnd.oci.image.index.v1+json';
const DOCKER_LIST = 'application/vnd.docker.distribution.manifest.list.v2+json';
const OCI_MANIFEST = 'application/vnd.oci.image.manifest.v1+json';
const DOCKER_MANIFEST = 'application/vnd.docker.distribution.manifest.v2+json';
export const MANIFEST_ACCEPT = [OCI_INDEX, DOCKER_LIST, OCI_MANIFEST, DOCKER_MANIFEST].join(', ');
const INDEX_TYPES = new Set([OCI_INDEX, DOCKER_LIST]);

export class RegistryError extends Error {
  constructor(reason) {
    super(reason);
    this.name = 'RegistryError';
    this.reason = reason;
  }
}

// "lscr.io/linuxserver/sonarr:4.0.17.2952-ls312@sha256:<hex>" ->
//   {registry: "lscr.io", repository: "linuxserver/sonarr", repo: "lscr.io/linuxserver/sonarr", tag, digest}
// `repo` is the repository exactly as pinned (what the helper's set-pin compares against).
export function parsePin(pin) {
  if (typeof pin !== 'string' || !pin || pin.length > 512 || /\s/.test(pin)) throw new RegistryError('not an image reference');
  let rest = pin;
  let digest = null;
  const at = rest.indexOf('@');
  if (at !== -1) {
    digest = rest.slice(at + 1);
    rest = rest.slice(0, at);
    if (!DIGEST_RE.test(digest)) throw new RegistryError('the pin has no sha256 digest');
  }
  let tag = null;
  const slash = rest.lastIndexOf('/');
  const colon = rest.lastIndexOf(':');
  if (colon > slash) {
    tag = rest.slice(colon + 1);
    rest = rest.slice(0, colon);
    if (!TAG_RE.test(tag)) throw new RegistryError('the pin has a bad tag');
  }
  const repo = rest;
  const parts = repo.split('/');
  if (parts.some((p) => !p)) throw new RegistryError('not an image reference');
  let registry;
  let repository;
  const first = parts[0];
  if (parts.length > 1 && (first.includes('.') || first.includes(':') || first === 'localhost')) {
    registry = first.toLowerCase();
    repository = parts.slice(1).join('/');
    if (DOCKER_HUB_ALIASES.has(registry)) {
      registry = DOCKER_HUB;
      if (!repository.includes('/')) repository = `library/${repository}`;
    }
  } else {
    registry = DOCKER_HUB;
    repository = parts.length === 1 ? `library/${repo}` : repo;
  }
  if (!/^[a-z0-9]+(?:[._-][a-z0-9]+)*(?:\/[a-z0-9]+(?:[._-][a-z0-9]+)*)*$/.test(repository)) {
    throw new RegistryError('not an image reference');
  }
  if (!/^[a-z0-9.-]+(?::\d{1,5})?$/.test(registry)) throw new RegistryError('not an image reference');
  return { registry, repository, repo, tag, digest };
}

const sha256 = (buf) => `sha256:${createHash('sha256').update(buf).digest('hex')}`;

// WWW-Authenticate: Bearer realm="https://…",service="…",scope="…"
export function parseChallenge(header) {
  const h = String(header ?? '');
  const m = h.match(/^\s*Bearer\s+(.*)$/i);
  if (!m) return null;
  const params = {};
  for (const p of m[1].matchAll(/([A-Za-z]+)="([^"]*)"/g)) params[p[1].toLowerCase()] = p[2];
  return params.realm ? params : null;
}

function isHttps(u) {
  try {
    return new URL(u).protocol === 'https:';
  } catch {
    return false;
  }
}

export function createRegistry({ fetch = globalThis.fetch, clock = () => Date.now(), timeoutMs = REQUEST_TIMEOUT_MS, log } = {}) {
  const tokens = new Map(); // "<registry> <scope>" -> {token, expiresAt}

  // One request with a deadline over headers and body. Returns {status, headers, body (Buffer|null)}.
  async function send(url, { method = 'GET', headers = {}, cap = MANIFEST_CAP, origin }) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs);
    try {
      let current = url;
      for (let hop = 0; ; hop++) {
        if (!isHttps(current)) throw new RegistryError('the registry redirected to a non-https address');
        if (hop > 0 && !isPublicHost(new URL(current).hostname)) throw new RegistryError('the registry redirected to a private address');
        const sameOrigin = new URL(current).origin === origin;
        const h = { ...headers };
        if (!sameOrigin) delete h.Authorization; // a CDN never gets the bearer
        let res;
        try {
          res = await fetch(current, { method, headers: h, redirect: 'manual', signal: ctrl.signal });
        } catch (err) {
          if (ctrl.signal.aborted) throw new RegistryError(`the registry didn't answer within ${Math.max(1, Math.ceil(timeoutMs / 1000))} s`);
          if (err instanceof RegistryError) throw err;
          throw new RegistryError('the registry is unreachable');
        }
        if ([301, 302, 303, 307, 308].includes(res.status)) {
          const loc = res.headers.get('location');
          await res.body?.cancel?.().catch(() => {});
          if (!loc || hop >= MAX_REDIRECTS) throw new RegistryError('the registry redirected too often');
          current = new URL(loc, current).toString();
          continue;
        }
        const body = method === 'HEAD' ? null : await readCapped(res, cap, ctrl);
        if (method === 'HEAD') await res.body?.cancel?.().catch(() => {});
        return { status: res.status, headers: res.headers, body };
      }
    } catch (err) {
      if (err instanceof RegistryError) throw err;
      if (ctrl.signal.aborted) throw new RegistryError(`the registry didn't answer within ${Math.max(1, Math.ceil(timeoutMs / 1000))} s`);
      throw new RegistryError('the registry is unreachable');
    } finally {
      clearTimeout(timer);
    }
  }

  async function readCapped(res, cap, ctrl) {
    const declared = Number(res.headers.get('content-length'));
    if (Number.isFinite(declared) && declared > cap) {
      await res.body?.cancel?.().catch(() => {});
      throw new RegistryError('the registry answer is too large');
    }
    if (!res.body) return Buffer.alloc(0);
    const chunks = [];
    let size = 0;
    try {
      for await (const chunk of res.body) {
        size += chunk.length;
        if (size > cap) {
          ctrl.abort();
          throw new RegistryError('the registry answer is too large');
        }
        chunks.push(Buffer.from(chunk));
      }
    } catch (err) {
      if (err instanceof RegistryError) throw err;
      if (ctrl.signal.aborted) throw new RegistryError(`the registry didn't answer within ${Math.max(1, Math.ceil(timeoutMs / 1000))} s`);
      throw new RegistryError('the registry answer was cut off');
    }
    return Buffer.concat(chunks);
  }

  async function fetchToken(p, challenge) {
    if (!isHttps(challenge.realm)) throw new RegistryError('the registry asked for a non-https login');
    if (!isPublicHost(new URL(challenge.realm).hostname)) throw new RegistryError('the registry asked for a login on a private address');
    const scope = challenge.scope || `repository:${p.repository}:pull`;
    const key = `${p.registry} ${scope}`;
    const u = new URL(challenge.realm);
    if (challenge.service) u.searchParams.set('service', challenge.service);
    u.searchParams.set('scope', scope);
    const r = await send(u.toString(), { headers: { Accept: 'application/json' }, cap: 64 * 1024, origin: u.origin });
    if (r.status !== 200) throw new RegistryError(`the registry login answered ${r.status}`);
    let data;
    try {
      data = JSON.parse(r.body.toString('utf8'));
    } catch {
      throw new RegistryError('the registry login sent no token');
    }
    const token = typeof data?.token === 'string' && data.token ? data.token : typeof data?.access_token === 'string' ? data.access_token : null;
    if (!token) throw new RegistryError('the registry login sent no token');
    // the token spec's default is 60 s; never kept longer than 5 min, whatever the registry says
    const ttl = Math.min(TOKEN_TTL_CAP_S, Number.isFinite(data.expires_in) && data.expires_in > 0 ? data.expires_in : 60);
    tokens.set(key, { token, expiresAt: clock() + ttl * 1000 });
    tokens.set(`${p.registry} repository:${p.repository}:pull`, tokens.get(key));
    return token;
  }

  function cachedToken(p) {
    const t = tokens.get(`${p.registry} repository:${p.repository}:pull`);
    if (!t) return null;
    if (clock() >= t.expiresAt) {
      tokens.delete(`${p.registry} repository:${p.repository}:pull`);
      return null;
    }
    return t.token;
  }

  // A registry API call with the anonymous token dance: one retry after a 401 challenge.
  async function api(p, pathPart, { method = 'GET', accept, cap }) {
    const origin = `https://${p.registry}`;
    const url = `${origin}/v2/${p.repository}/${pathPart}`;
    const headers = accept ? { Accept: accept } : {};
    for (let attempt = 0; attempt < 2; attempt++) {
      const token = cachedToken(p);
      const r = await send(url, { method, headers: token ? { ...headers, Authorization: `Bearer ${token}` } : headers, cap, origin });
      if (r.status !== 401) return r;
      if (attempt === 1) break;
      const challenge = parseChallenge(r.headers.get('www-authenticate'));
      if (!challenge) throw new RegistryError('the registry refused access');
      tokens.delete(`${p.registry} repository:${p.repository}:pull`);
      await fetchToken(p, challenge);
    }
    throw new RegistryError('the registry refused access');
  }

  function json(body, what) {
    try {
      const v = JSON.parse(body.toString('utf8'));
      if (v && typeof v === 'object') return v;
    } catch {
      // fall through
    }
    throw new RegistryError(`the ${what} isn't valid JSON`);
  }

  const statusReason = (status, what) => (status === 404 ? `${what} not found` : `the registry answered ${status}`);

  async function latest(pin, channel = 'latest') {
    const p = parsePin(pin);
    if (!TAG_RE.test(channel)) throw new RegistryError('bad channel');
    // 1. the channel's manifest (usually an index)
    const top = await api(p, `manifests/${channel}`, { accept: MANIFEST_ACCEPT, cap: MANIFEST_CAP });
    if (top.status !== 200) throw new RegistryError(statusReason(top.status, `the ${channel} tag was`));
    const digest = top.headers.get('docker-content-digest');
    if (!digest || !DIGEST_RE.test(digest)) throw new RegistryError('the registry sent no digest');
    if (sha256(top.body) !== digest) throw new RegistryError("the manifest doesn't match its digest");
    let manifest = json(top.body, 'manifest');
    const type = manifest.mediaType ?? String(top.headers.get('content-type') ?? '').split(';')[0].trim();
    let platformDigest = digest;
    // 2. an index: the linux/amd64 image
    if (INDEX_TYPES.has(type) || Array.isArray(manifest.manifests)) {
      const entry = (manifest.manifests ?? []).find(
        (m) => m?.platform?.os === 'linux' && m?.platform?.architecture === 'amd64' && (!m.platform.variant || m.platform.variant === 'v1') && DIGEST_RE.test(m?.digest ?? ''),
      );
      if (!entry) throw new RegistryError('no linux/amd64 image');
      const sub = await api(p, `manifests/${entry.digest}`, { accept: MANIFEST_ACCEPT, cap: MANIFEST_CAP });
      if (sub.status !== 200) throw new RegistryError(statusReason(sub.status, 'the amd64 image was'));
      if (sha256(sub.body) !== entry.digest) throw new RegistryError("the amd64 manifest doesn't match its digest");
      manifest = json(sub.body, 'manifest');
      platformDigest = entry.digest;
      if (Array.isArray(manifest.manifests)) throw new RegistryError('nested image index');
    }
    // 3. the config blob's version label
    const cfgDigest = manifest?.config?.digest;
    if (typeof cfgDigest !== 'string' || !DIGEST_RE.test(cfgDigest)) throw new RegistryError('the manifest has no config');
    if (Number.isFinite(manifest.config.size) && manifest.config.size > CONFIG_CAP) throw new RegistryError('the image config is too large');
    const blob = await api(p, `blobs/${cfgDigest}`, { cap: CONFIG_CAP });
    if (blob.status !== 200) throw new RegistryError(statusReason(blob.status, 'the image config was'));
    if (sha256(blob.body) !== cfgDigest) throw new RegistryError("the image config doesn't match its digest");
    const config = json(blob.body, 'image config');
    if ((config.os && config.os !== 'linux') || (config.architecture && config.architecture !== 'amd64')) {
      throw new RegistryError('no linux/amd64 image');
    }
    const version = config?.config?.Labels?.[VERSION_LABEL];
    if (typeof version !== 'string' || !version) throw new RegistryError('the image has no version label');
    if (!TAG_RE.test(version)) throw new RegistryError('the version label is not a valid tag');
    // 4. the version tag must name the same image
    const head = await api(p, `manifests/${version}`, { method: 'HEAD', accept: MANIFEST_ACCEPT });
    if (head.status !== 200) throw new RegistryError(statusReason(head.status, `the ${version} tag was`));
    if (head.headers.get('docker-content-digest') !== digest) throw new RegistryError(`the ${version} tag points to a different image`);
    log?.debug?.('registry lookup', { repo: p.repo, version });
    return { version, digest, platformDigest, pin: `${p.repo}:${version}@${digest}` };
  }

  return { latest, parsePin, tokenCount: () => tokens.size };
}
