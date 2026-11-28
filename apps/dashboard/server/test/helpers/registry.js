// A fake OCI registry behind an injectable fetch (no network): the v2 manifest/blob API with an anonymous Bearer
// challenge, a token endpoint, blob redirects to a "CDN" origin, and switches for the failure cases.
//   const reg = fakeRegistry();
//   const img = reg.push('lscr.io', 'linuxserver/sonarr', { version: '4.0.20.3014-ls7', tags: ['latest', '4.0.20.3014-ls7'] });
//   createRegistry({ fetch: reg.fetch })
import { createHash } from 'node:crypto';

const sha = (buf) => `sha256:${createHash('sha256').update(buf).digest('hex')}`;
const OCI_INDEX = 'application/vnd.oci.image.index.v1+json';
const OCI_MANIFEST = 'application/vnd.oci.image.manifest.v1+json';

// Realms as the real registries answer them: lscr.io and ghcr.io -> ghcr.io's token service; Docker Hub -> its own.
export const REALMS = {
  'lscr.io': { realm: 'https://ghcr.io/token', service: 'ghcr.io' },
  'ghcr.io': { realm: 'https://ghcr.io/token', service: 'ghcr.io' },
  'registry-1.docker.io': { realm: 'https://auth.docker.io/token', service: 'registry.docker.io' },
};

export function fakeRegistry() {
  const repos = new Map(); // "<host>/<repo>" -> {manifests: Map(ref -> {body, type, digest?}), blobs: Map}
  const calls = [];
  const tokensIssued = [];
  const cdnAuth = []; // Authorization headers the CDN saw (must stay empty)
  const opts = {
    hang: new Set(), // "<host>/<repo>" whose manifest requests never answer
    status: new Map(), // "<host>/<repo>" -> HTTP status for every manifest GET
    wrongDigestHeader: new Set(), // the top-level Docker-Content-Digest doesn't match the body
    huge: new Set(), // the top-level manifest streams past 1 MiB (no Content-Length)
    hugeDeclared: new Set(), // the top-level manifest declares a Content-Length over the cap
    tokenTtl: 300,
    plainHttpRealm: false,
    realmOverride: null, // e.g. "https://127.0.0.1/token"
    redirectBlobsTo: null, // e.g. "https://10.0.0.1" instead of the CDN
  };
  const repo = (host, name) => {
    const key = `${host}/${name}`;
    if (!repos.has(key)) repos.set(key, { manifests: new Map(), blobs: new Map() });
    return repos.get(key);
  };

  // An image: config blob (with or without the version label), amd64 manifest, and an index (unless single).
  function push(host, name, { version, tags = ['latest'], label = true, arches = ['amd64', 'arm64'], single = false, labelValue } = {}) {
    const r = repo(host, name);
    const manifests = [];
    let amd = null;
    for (const arch of arches) {
      const config = Buffer.from(JSON.stringify({ architecture: arch, os: 'linux', config: { Labels: label ? { 'org.opencontainers.image.version': labelValue ?? version } : {} } }));
      const configDigest = sha(config);
      r.blobs.set(configDigest, config);
      const manifest = Buffer.from(JSON.stringify({ schemaVersion: 2, mediaType: OCI_MANIFEST, config: { mediaType: 'application/vnd.oci.image.config.v1+json', digest: configDigest, size: config.length }, layers: [] }));
      const digest = sha(manifest);
      r.manifests.set(digest, { body: manifest, type: OCI_MANIFEST });
      manifests.push({ mediaType: OCI_MANIFEST, digest, size: manifest.length, platform: { architecture: arch, os: 'linux' } });
      if (arch === 'amd64') amd = { digest, configDigest };
    }
    let top;
    if (single) top = { body: r.manifests.get(manifests[0].digest).body, type: OCI_MANIFEST };
    else top = { body: Buffer.from(JSON.stringify({ schemaVersion: 2, mediaType: OCI_INDEX, manifests })), type: OCI_INDEX };
    const digest = sha(top.body);
    r.manifests.set(digest, top);
    for (const t of tags) r.manifests.set(t, top);
    return { digest, platformDigest: amd?.digest ?? null, version, pin: (repoAsPinned) => `${repoAsPinned}:${version}@${digest}` };
  }

  // Point a tag somewhere else (the "version tag mismatch" case).
  function retag(host, name, tag, image) {
    const r = repo(host, name);
    r.manifests.set(tag, r.manifests.get(image.digest));
  }

  const res = (body, status = 200, headers = {}) => new Response(body, { status, headers });

  async function fetchImpl(url, init = {}) {
    const u = new URL(url);
    const headers = Object.fromEntries(Object.entries(init.headers ?? {}).map(([k, v]) => [k.toLowerCase(), v]));
    const method = init.method ?? 'GET';
    calls.push({ method, url, auth: headers.authorization ?? null, redirect: init.redirect });
    if (u.protocol !== 'https:') throw new TypeError('the fake only speaks https');
    // token services
    if (u.pathname === '/token' && (u.host === 'ghcr.io' || u.host === 'auth.docker.io')) {
      const token = `tok-${u.host}-${u.searchParams.get('scope')}-${tokensIssued.length}`;
      tokensIssued.push({ host: u.host, service: u.searchParams.get('service'), scope: u.searchParams.get('scope'), token });
      return res(JSON.stringify({ token, expires_in: opts.tokenTtl }), 200, { 'Content-Type': 'application/json' });
    }
    // the blob CDN
    if (u.host === 'cdn.example') {
      cdnAuth.push(headers.authorization ?? null);
      const [hostRepo, digest] = decodeURIComponent(u.pathname.slice(1)).split('|');
      const body = repos.get(hostRepo)?.blobs.get(digest);
      return body ? res(body, 200, { 'Content-Type': 'application/octet-stream' }) : res('missing', 404);
    }
    const m = u.pathname.match(/^\/v2\/(.+)\/(manifests|blobs)\/([^/]+)$/);
    if (!m) return res('not found', 404);
    const [, name, kind, ref] = m;
    const key = `${u.host}/${name}`;
    const realm = REALMS[u.host] ?? { realm: `https://${u.host}/token`, service: u.host };
    const scope = `repository:${name}:pull`;
    if (!String(headers.authorization ?? '').startsWith(`Bearer tok-${new URL(realm.realm).host}-${scope}-`)) {
      const realmUrl = opts.realmOverride ?? (opts.plainHttpRealm ? realm.realm.replace('https:', 'http:') : realm.realm);
      return res('{"errors":[{"code":"UNAUTHORIZED"}]}', 401, { 'WWW-Authenticate': `Bearer realm="${realmUrl}",service="${realm.service}",scope="${scope}"` });
    }
    const r = repos.get(key);
    if (kind === 'blobs') {
      if (!r?.blobs.has(ref)) return res('blob unknown', 404);
      return res(null, 307, { Location: `${opts.redirectBlobsTo ?? 'https://cdn.example'}/${encodeURIComponent(`${key}|${ref}`)}` });
    }
    if (opts.hang.has(key)) {
      return new Promise((_, reject) => init.signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError'))));
    }
    if (opts.status.has(key)) return res('{"errors":[]}', opts.status.get(key));
    const entry = r?.manifests.get(ref);
    if (!entry) return res('{"errors":[{"code":"MANIFEST_UNKNOWN"}]}', 404);
    const isTag = !ref.startsWith('sha256:');
    let digest = sha(entry.body);
    if (isTag && ref === 'latest' && opts.wrongDigestHeader.has(key)) digest = `sha256:${'f'.repeat(64)}`;
    const h = { 'Content-Type': entry.type, 'Docker-Content-Digest': digest };
    if (method === 'HEAD') return res(null, 200, { ...h, 'Content-Length': String(entry.body.length) });
    if (ref === 'latest' && opts.hugeDeclared.has(key)) return res(entry.body, 200, { ...h, 'Content-Length': String(5 * 1024 * 1024) });
    if (ref === 'latest' && opts.huge.has(key)) {
      let sent = 0;
      const stream = new ReadableStream({
        pull(ctrl) {
          if (sent > 4 * 1024 * 1024) return ctrl.close();
          sent += 64 * 1024;
          ctrl.enqueue(new Uint8Array(64 * 1024));
        },
      });
      return res(stream, 200, h);
    }
    return res(entry.body, 200, h);
  }

  return { fetch: fetchImpl, push, retag, calls, tokensIssued, cdnAuth, opts };
}
