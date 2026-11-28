// The registry client (CONTRACTS §11.7.2) against a fake registry: no network.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createRegistry, isPublicHost, parseChallenge, parsePin, RegistryError } from '../src/registry.js';
import { compareVersions, isMajor, majorOf, mediaApps } from '../src/media-updates.js';
import { fakeRegistry } from './helpers/registry.js';

const D = (c) => `sha256:${c.repeat(64)}`;

test('parsePin: registry host, repository, tag, digest; Docker Hub and its library/ prefix', () => {
  assert.deepEqual(parsePin(`lscr.io/linuxserver/sonarr:4.0.17.2952-ls312@${D('a')}`), {
    registry: 'lscr.io',
    repository: 'linuxserver/sonarr',
    repo: 'lscr.io/linuxserver/sonarr',
    tag: '4.0.17.2952-ls312',
    digest: D('a'),
  });
  assert.deepEqual(parsePin(`fallenbagel/jellyseerr:2.7.3@${D('b')}`), {
    registry: 'registry-1.docker.io',
    repository: 'fallenbagel/jellyseerr',
    repo: 'fallenbagel/jellyseerr',
    tag: '2.7.3',
    digest: D('b'),
  });
  assert.equal(parsePin(`nginx:1.27@${D('c')}`).repository, 'library/nginx');
  assert.equal(parsePin(`nginx:1.27@${D('c')}`).repo, 'nginx', 'the repo as pinned stays as pinned');
  assert.equal(parsePin(`docker.io/nginx:1.27@${D('c')}`).registry, 'registry-1.docker.io');
  assert.equal(parsePin(`docker.io/nginx:1.27@${D('c')}`).repository, 'library/nginx');
  assert.equal(parsePin(`localhost:5000/team/app:1@${D('c')}`).registry, 'localhost:5000');
  assert.equal(parsePin('ghcr.io/team/app').tag, null);
  for (const bad of ['', 'a b', `x:1@sha256:${'z'.repeat(64)}`, 'UPPER/Case:1', '//x', 'x:bad tag', 'x'.repeat(600)]) {
    assert.throws(() => parsePin(bad), RegistryError, bad);
  }
});

test('parseChallenge reads realm, service and scope', () => {
  assert.deepEqual(parseChallenge('Bearer realm="https://ghcr.io/token",service="ghcr.io",scope="repository:linuxserver/sonarr:pull"'), {
    realm: 'https://ghcr.io/token',
    service: 'ghcr.io',
    scope: 'repository:linuxserver/sonarr:pull',
  });
  assert.equal(parseChallenge('Basic realm="x"'), null);
  assert.equal(parseChallenge(null), null);
});

test('latest: challenge -> anonymous token -> index -> linux/amd64 -> config label -> HEAD version tag; blobs via the CDN without the bearer', async () => {
  const reg = fakeRegistry();
  const img = reg.push('lscr.io', 'linuxserver/sonarr', { version: '4.0.20.3014-ls7', tags: ['latest', '4.0.20.3014-ls7'] });
  const r = createRegistry({ fetch: reg.fetch });
  const got = await r.latest(`lscr.io/linuxserver/sonarr:4.0.17.2952-ls312@${D('a')}`);
  assert.deepEqual(got, {
    version: '4.0.20.3014-ls7',
    digest: img.digest,
    platformDigest: img.platformDigest,
    pin: `lscr.io/linuxserver/sonarr:4.0.20.3014-ls7@${img.digest}`,
  });
  // lscr.io answers with ghcr.io's token service (as the real one does)
  assert.deepEqual(reg.tokensIssued.map((x) => [x.host, x.service, x.scope]), [['ghcr.io', 'ghcr.io', 'repository:linuxserver/sonarr:pull']]);
  const steps = reg.calls.map((c) => `${c.method} ${new URL(c.url).host}${new URL(c.url).pathname}`);
  assert.deepEqual(steps, [
    'GET lscr.io/v2/linuxserver/sonarr/manifests/latest', // 401 challenge
    'GET ghcr.io/token',
    'GET lscr.io/v2/linuxserver/sonarr/manifests/latest',
    `GET lscr.io/v2/linuxserver/sonarr/manifests/${img.platformDigest}`,
    `GET lscr.io/v2/linuxserver/sonarr/blobs/${steps[4].split('/blobs/')[1]}`,
    steps[5], // the CDN
    'HEAD lscr.io/v2/linuxserver/sonarr/manifests/4.0.20.3014-ls7',
  ]);
  assert.match(steps[5], /^GET cdn\.example\//);
  assert.deepEqual(reg.cdnAuth, [null], 'the bearer never goes to another origin');
  assert.ok(reg.calls.every((c) => c.redirect === 'manual'), 'redirects are followed by the client, hop by hop');
  // the token is cached per scope: a second lookup asks for none
  await r.latest(`lscr.io/linuxserver/sonarr:4.0.17.2952-ls312@${D('a')}`);
  assert.equal(reg.tokensIssued.length, 1);
});

test('latest: tokens expire after expires_in and are fetched again', async () => {
  const reg = fakeRegistry();
  reg.opts.tokenTtl = 60;
  reg.push('ghcr.io', 'team/app', { version: '1.2.3', tags: ['latest', '1.2.3'] });
  let now = 1_000_000;
  const r = createRegistry({ fetch: reg.fetch, clock: () => now });
  await r.latest(`ghcr.io/team/app:1.2.2@${D('a')}`);
  now += 61_000;
  await r.latest(`ghcr.io/team/app:1.2.2@${D('a')}`);
  assert.equal(reg.tokensIssued.length, 2);
});

test('latest: Docker Hub (library/ mapping, auth.docker.io realm)', async () => {
  const reg = fakeRegistry();
  const img = reg.push('registry-1.docker.io', 'library/nginx', { version: '1.29.1', tags: ['latest', '1.29.1'] });
  const got = await createRegistry({ fetch: reg.fetch }).latest(`nginx:1.27.0@${D('a')}`);
  assert.equal(got.pin, `nginx:1.29.1@${img.digest}`, 'pinned as written, without library/');
  assert.deepEqual(reg.tokensIssued.map((x) => [x.host, x.service, x.scope]), [['auth.docker.io', 'registry.docker.io', 'repository:library/nginx:pull']]);
  assert.ok(reg.calls.some((c) => c.url.startsWith('https://registry-1.docker.io/v2/library/nginx/manifests/latest')));
});

test('latest: a single-arch manifest (no index) works; a non-amd64 one does not', async () => {
  const reg = fakeRegistry();
  const img = reg.push('ghcr.io', 'team/one', { version: '3.0.0', tags: ['latest', '3.0.0'], arches: ['amd64'], single: true });
  const r = createRegistry({ fetch: reg.fetch });
  assert.equal((await r.latest(`ghcr.io/team/one:2.0.0@${D('a')}`)).digest, img.digest);
  reg.push('ghcr.io', 'team/arm', { version: '3.0.0', tags: ['latest', '3.0.0'], arches: ['arm64'] });
  await assert.rejects(r.latest(`ghcr.io/team/arm:2.0.0@${D('a')}`), { reason: 'no linux/amd64 image' });
  reg.push('ghcr.io', 'team/armsingle', { version: '3.0.0', tags: ['latest', '3.0.0'], arches: ['arm64'], single: true });
  await assert.rejects(r.latest(`ghcr.io/team/armsingle:2.0.0@${D('a')}`), { reason: 'no linux/amd64 image' });
});

test('latest: refused cases each give a short reason without URLs or tokens', async () => {
  const reg = fakeRegistry();
  const pin = (n) => `ghcr.io/team/${n}:1.0.0@${D('a')}`;
  reg.push('ghcr.io', 'team/mismatch', { version: '2.0.0', tags: ['latest', '2.0.0'] });
  reg.opts.wrongDigestHeader.add('ghcr.io/team/mismatch');
  reg.push('ghcr.io', 'team/nolabel', { version: '2.0.0', tags: ['latest', '2.0.0'], label: false });
  const other = reg.push('ghcr.io', 'team/moved', { version: '2.0.0', tags: ['2.0.0'] });
  reg.push('ghcr.io', 'team/moved', { version: '2.0.0', tags: ['latest'], labelValue: '2.0.0', arches: ['amd64'] });
  reg.retag('ghcr.io', 'team/moved', '2.0.0', other);
  reg.push('ghcr.io', 'team/notag', { version: '2.0.0', tags: ['latest'] });
  reg.push('ghcr.io', 'team/badlabel', { version: '2.0.0', tags: ['latest'], labelValue: 'v 2 (beta)' });
  reg.opts.status.set('ghcr.io/team/down', 503);
  const r = createRegistry({ fetch: reg.fetch });
  const cases = [
    ['mismatch', "the manifest doesn't match its digest"],
    ['nolabel', 'the image has no version label'],
    ['moved', 'the 2.0.0 tag points to a different image'],
    ['notag', 'the 2.0.0 tag was not found'],
    ['badlabel', 'the version label is not a valid tag'],
    ['down', 'the registry answered 503'],
    ['missing', 'the latest tag was not found'],
  ];
  for (const [n, reason] of cases) {
    const err = await r.latest(pin(n)).then(() => null, (e) => e);
    assert.ok(err instanceof RegistryError, n);
    assert.equal(err.reason, reason, n);
    assert.doesNotMatch(err.message, /https?:|tok-|Bearer/i, n);
  }
});

test('latest: a non-https token realm is refused; a fetch failure is "unreachable"', async () => {
  const reg = fakeRegistry();
  reg.push('ghcr.io', 'team/app', { version: '2.0.0', tags: ['latest', '2.0.0'] });
  reg.opts.plainHttpRealm = true;
  await assert.rejects(createRegistry({ fetch: reg.fetch }).latest(`ghcr.io/team/app:1@${D('a')}`), { reason: 'the registry asked for a non-https login' });
  assert.equal(reg.tokensIssued.length, 0);
  const broken = async () => {
    throw new TypeError('fetch failed: getaddrinfo ENOTFOUND https://ghcr.io/v2/x?token=secret');
  };
  const err = await createRegistry({ fetch: broken }).latest(`ghcr.io/team/app:1@${D('a')}`).catch((e) => e);
  assert.equal(err.reason, 'the registry is unreachable');
  assert.doesNotMatch(err.message, /ghcr|secret/);
});

test('latest: per-request timeout; manifest size cap (declared and streamed)', async () => {
  const reg = fakeRegistry();
  reg.push('ghcr.io', 'team/slow', { version: '2.0.0', tags: ['latest', '2.0.0'] });
  reg.opts.hang.add('ghcr.io/team/slow');
  const r = createRegistry({ fetch: reg.fetch, timeoutMs: 100 });
  const t0 = Date.now();
  await assert.rejects(r.latest(`ghcr.io/team/slow:1@${D('a')}`), { reason: "the registry didn't answer within 1 s" });
  assert.ok(Date.now() - t0 < 2000);
  reg.push('ghcr.io', 'team/huge', { version: '2.0.0', tags: ['latest', '2.0.0'] });
  reg.opts.huge.add('ghcr.io/team/huge');
  await assert.rejects(createRegistry({ fetch: reg.fetch }).latest(`ghcr.io/team/huge:1@${D('a')}`), { reason: 'the registry answer is too large' });
  reg.push('ghcr.io', 'team/declared', { version: '2.0.0', tags: ['latest', '2.0.0'] });
  reg.opts.hugeDeclared.add('ghcr.io/team/declared');
  await assert.rejects(createRegistry({ fetch: reg.fetch }).latest(`ghcr.io/team/declared:1@${D('a')}`), { reason: 'the registry answer is too large' });
});

test('latest: a redirect to plain http is refused', async () => {
  const fetchImpl = async (url) => {
    if (url.includes('/manifests/')) return new Response(null, { status: 302, headers: { Location: 'http://cdn.example/x' } });
    return new Response('nope', { status: 404 });
  };
  await assert.rejects(createRegistry({ fetch: fetchImpl }).latest(`ghcr.io/team/app:1@${D('a')}`), { reason: 'the registry redirected to a non-https address' });
});

test('major detection, and which declared apps are checked', () => {
  assert.equal(majorOf('10.11.9ubu2404-ls33'), 10);
  assert.equal(majorOf('12.4ubu2604-ls61'), 12);
  assert.equal(isMajor('10.11.9ubu2404-ls33', '12.4ubu2604-ls61'), true);
  assert.equal(isMajor('4.0.17.2952-ls312', '4.0.20.3014-ls7'), false);
  assert.equal(isMajor('latest', '1.0'), false);
  const declared = [
    { name: 'sonarr', stack: 'media', pinned: `x/sonarr:1@${D('a')}`, local_build: false },
    { name: 'cloudflared', stack: 'edge', pinned: `x/cf:1@${D('a')}`, local_build: false },
    { name: 'wol-relay', stack: 'media', pinned: 'homelab/wol-relay:1', local_build: true },
    { name: 'radarr', stack: 'media', pinned: null, local_build: false },
    { name: 'jellyfin', stack: 'media', pinned: `x/jf:1@${D('a')}`, local_build: false },
  ];
  assert.deepEqual(mediaApps(declared).map((d) => d.name), ['jellyfin', 'sonarr']);
  assert.deepEqual(mediaApps(null), []);
});

test('compareVersions: leading dotted numbers, then the linuxserver build; null when it cannot tell', () => {
  assert.equal(compareVersions('4.0.20.3014-ls7', '4.0.17.2952-ls312'), 1);
  assert.equal(compareVersions('4.0.17.2952-ls312', '4.0.20.3014-ls7'), -1);
  assert.equal(compareVersions('10.11.9ubu2404-ls33', '12.4ubu2604-ls61'), -1);
  assert.equal(compareVersions('1.2', '1.2.0'), null);
  assert.equal(compareVersions('4.0.17.2952-ls300', '4.0.17.2952-ls312'), -1);
  assert.equal(compareVersions('4.0.17.2952-ls312', '4.0.17.2952-ls312'), 0);
  assert.equal(compareVersions('v2.1', '2.0'), 1);
  assert.equal(compareVersions('latest', '1.0'), null);
  assert.equal(compareVersions('nightly-2026', '1.0'), null);
});

test('token lifetime is capped at 300 s whatever expires_in says', async () => {
  const reg = fakeRegistry();
  reg.opts.tokenTtl = 3600;
  reg.push('ghcr.io', 'team/app', { version: '1.2.3', tags: ['latest', '1.2.3'] });
  let now = 1_000_000;
  const r = createRegistry({ fetch: reg.fetch, clock: () => now });
  await r.latest(`ghcr.io/team/app:1.2.2@${D('a')}`);
  now += 299_000;
  await r.latest(`ghcr.io/team/app:1.2.2@${D('a')}`);
  assert.equal(reg.tokensIssued.length, 1, 'still cached at 299 s');
  now += 2000;
  await r.latest(`ghcr.io/team/app:1.2.2@${D('a')}`);
  assert.equal(reg.tokensIssued.length, 2, 'fetched again after 300 s');
});

test('token realms and redirect targets on private hosts are refused', async () => {
  for (const h of ['127.0.0.1', '10.0.0.1', '[::1]', 'localhost', 'registry', 'box.local', 'reg.internal', 'nas.lan', 'box.example.ts.net', 'x.localhost', 'r.home.arpa']) {
    assert.equal(isPublicHost(new URL(`https://${h}/`).hostname), false, h);
  }
  for (const h of ['ghcr.io', 'auth.docker.io', 'pkg-containers.githubusercontent.com', 'cdn.example']) assert.equal(isPublicHost(h), true, h);
  assert.equal(isPublicHost(new URL('https://0x7f.1/').hostname), false, 'hex IPv4 normalised by the URL parser');

  for (const realm of ['https://127.0.0.1/token', 'https://localhost/token', 'https://auth.registry.internal/token']) {
    const reg = fakeRegistry();
    reg.push('ghcr.io', 'team/app', { version: '2.0.0', tags: ['latest', '2.0.0'] });
    reg.opts.realmOverride = realm;
    const err = await createRegistry({ fetch: reg.fetch }).latest(`ghcr.io/team/app:1@${D('a')}`).catch((e) => e);
    assert.equal(err.reason, 'the registry asked for a login on a private address', realm);
    assert.equal(reg.calls.filter((c) => !c.url.startsWith('https://ghcr.io/v2/')).length, 0, `${realm} never contacted`);
  }
  for (const target of ['https://10.0.0.1', 'https://[::1]', 'https://cdn.local', 'https://box.example.ts.net']) {
    const reg = fakeRegistry();
    reg.push('ghcr.io', 'team/app', { version: '2.0.0', tags: ['latest', '2.0.0'] });
    reg.opts.redirectBlobsTo = target;
    const err = await createRegistry({ fetch: reg.fetch }).latest(`ghcr.io/team/app:1@${D('a')}`).catch((e) => e);
    assert.equal(err.reason, 'the registry redirected to a private address', target);
    assert.ok(!reg.calls.some((c) => c.url.startsWith(target)), `${target} never contacted`);
  }
});
