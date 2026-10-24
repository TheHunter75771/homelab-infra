// Fake summaries for tests and the dev server, in the server's wire format (apps/dashboard/server,
// schema 1), checked against the real dumps in test/server-summaries/ (npm run dump-summaries).
// All names are placeholders; nothing here identifies a real host.

import fs from 'node:fs';

const H = 3600e3;
const D = 24 * H;
const iso = (ms) => new Date(ms).toISOString();
const day = (ms) => iso(ms).slice(0, 10);

export const PINNED = {
  cloudflared: 'cloudflare/cloudflared:2026.5.0@sha256:59bab8d3aceec09bf6bdb07d6beca0225ca5cd7ab79436a87ea97978fe1dc4f9',
  jellyfin: 'lscr.io/linuxserver/jellyfin:10.11.9ubu2404-ls33@sha256:155fb51c745d1a2c28c8b300612bc728a5dd06097bbc62b8b1c76d174eb20366',
  jellyseerr: 'fallenbagel/jellyseerr:2.7.3@sha256:4538137bc5af902dece165f2bf73776d9cf4eafb6dd714670724af8f3eb77764',
  prowlarr: 'lscr.io/linuxserver/prowlarr:2.3.5.5327-ls147@sha256:c9fe528f34b1fd3715438b6f6d6991d64e2965f2c055db36398bc66a0e7eab01',
  radarr: 'lscr.io/linuxserver/radarr:6.1.1.10360-ls303@sha256:079e48870584baf2a3e7e43e7ba6d3c834555931851a59c82c51cc792d285caf',
  sabnzbd: 'lscr.io/linuxserver/sabnzbd:5.0.3-ls255@sha256:6b392fce45ed587ba7213259fbce6f9f725b157b746b36ec53d3f08ee415602e',
  sonarr: 'lscr.io/linuxserver/sonarr:4.0.17.2952-ls312@sha256:0b5c4803f92456fb9b65bae8375716ea120b4ea17b3cced7da32b63f0085782b',
  'wol-relay': 'homelab/wol-relay:2026.07',
};
const META = {
  cloudflared: { label: 'cloudflared', purpose: 'Public tunnel for /watch', open: null, stack: 'edge', mediaGated: false },
  jellyfin: { label: 'Jellyfin', purpose: 'Streams the library to TVs, phones and the web', open: { port: 8444, path: '/watch' }, stack: 'media', mediaGated: true },
  jellyseerr: { label: 'Jellyseerr', purpose: 'Takes requests for new movies and shows', open: { port: 8443, path: '/' }, stack: 'media', mediaGated: true },
  prowlarr: { label: 'Prowlarr', purpose: 'Manages the indexers for Sonarr and Radarr', open: { port: 8444, path: '/prowlarr' }, stack: 'media', mediaGated: true },
  radarr: { label: 'Radarr', purpose: 'Finds and grabs movies', open: { port: 8444, path: '/radarr' }, stack: 'media', mediaGated: true },
  sabnzbd: { label: 'SABnzbd', purpose: 'Downloads from Usenet', open: { port: 8444, path: '/sabnzbd' }, stack: 'media', mediaGated: true },
  sonarr: { label: 'Sonarr', purpose: 'Finds and grabs TV episodes', open: { port: 8444, path: '/sonarr' }, stack: 'media', mediaGated: true },
  'wol-relay': { label: 'wol-relay', purpose: 'Wakes the gaming PC from the tailnet', open: null, stack: 'utils', mediaGated: false },
};
export const NAMES = Object.keys(META);
const HELD = 'Held by the boot gate until the media drive is back.';
const DOCKER_DOWN = "Docker isn't running.";
const tagOf = (ref) => {
  const s = (ref || '').split('@')[0];
  const i = s.lastIndexOf(':');
  return i > s.lastIndexOf('/') ? s.slice(i + 1) : 'latest';
};

// One app entry as the server builds it (collectors/apps.js), matching the real dumps:
// - Docker not active (bad night, Docker down): no `ps`, so state, health, image, startedAt and
//   restartCount are null and the tag comes from the pin.
// - media-lost: the container exists, `exited`, with its image; status held, stoppedReason gate.
// Running images carry their digest like the pins (the helper reports the running ref).
export function app(name, now, { status = 'healthy', state = 'running', health = 'healthy', image, startedAt, restartCount = 0, noPs = false, stoppedReason = null } = {}) {
  const pinned = PINNED[name];
  const RADARR_OLD = `lscr.io/linuxserver/radarr:6.1.0.10293-ls302@sha256:${'b'.repeat(64)}`;
  const img = noPs ? null : image === undefined ? (name === 'radarr' ? RADARR_OLD : pinned) : image;
  const running = !noPs && state === 'running';
  const digest = (r) => (r && r.includes('@') ? r.split('@')[1] : null);
  const updateAvailable = Boolean(img) && (digest(img) && digest(pinned) ? digest(img) !== digest(pinned) : img !== pinned);
  const held = status === 'held';
  const blocked = held ? HELD : noPs ? DOCKER_DOWN : null;
  return {
    name,
    ...META[name],
    status,
    stoppedReason: stoppedReason ?? (held ? 'gate' : noPs ? 'docker-down' : status === 'stopped' ? 'intentional' : null),
    state: noPs ? null : state,
    health: noPs || !running || name === 'wol-relay' ? null : health,
    startedAt: running ? (startedAt ?? iso(now - (3 * D + 8 * H))) : null,
    restartCount: noPs ? null : restartCount,
    image: img,
    tag: tagOf(img || pinned),
    pinned,
    pinnedTag: tagOf(pinned),
    updateAvailable,
    localBuild: name === 'wol-relay',
    action: null,
    locks: {
      restart: blocked ?? (!running ? 'Stopped. Start it instead' : null),
      stop: blocked ?? (!running ? 'Already stopped' : null),
      start: blocked ?? (running ? 'Already running' : null),
      update: blocked ?? (updateAvailable ? null : 'Already on the pinned version'),
      logs: noPs ? (held ? HELD : DOCKER_DOWN) : null,
    },
  };
}

function samples(now, { days = 40, start = 1.36e12 - 39 * 6e9, perDay = 6e9 } = {}) {
  const out = [];
  for (let i = days - 1; i >= 0; i -= 1) {
    const used = Math.round(start + (days - 1 - i) * perDay + Math.sin(i * 1.7) * 2.5e9);
    out.push({ date: day(now - i * D), used, total: 4e12 });
  }
  return out;
}

const addDays = (d, n) => day(Date.parse(`${d}T12:00:00Z`) + n * D);

function boxDays(now, partial = {}) {
  const out = [];
  for (let i = 29; i >= 0; i -= 1) out.push({ date: day(now - i * D), pct: partial[i] ?? 100 });
  return out;
}

// Month keys in the fixtures' zone (UTC), whatever the test process's TZ.
export function monthsBack(now, n) {
  const d = new Date(now);
  const out = [];
  for (let i = n - 1; i >= 0; i -= 1) {
    const m = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth() - i, 1));
    out.push(`${m.getUTCFullYear()}-${String(m.getUTCMonth() + 1).padStart(2, '0')}`);
  }
  return out;
}

const ev = (ts, event, detail, waitSeconds = 0) => ({ ts, event, detail, waitSeconds });

function olderGroups(now) {
  return [
    {
      boot: '77b0',
      current: false,
      startedAt: iso(now - 4 * D - 8 * H),
      events: [
        ev(iso(now - 4 * D - 8 * H + 340e3), 'retry-pass', 'Mounted on retry 2 after power-cycle', 300),
        ev(iso(now - 4 * D - 8 * H + 38e3), 'powercycle', 'USB port 2 power-cycled', 30),
        ev(iso(now - 4 * D - 8 * H + 2e3), 'fail', 'sdb not present after 30 s', 30),
      ],
    },
    {
      boot: '5a12',
      current: false,
      startedAt: iso(now - 12 * D),
      events: [ev(iso(now - 12 * D + 70e3), 'pass', 'fsck clean, mounted'), ev(iso(now - 12 * D + 48e3), 'repair', 'fsck fixed 2 errors on sdb1')],
    },
  ];
}

function media(now) {
  const checks = [
    { key: 'download-client', label: 'Download client reachable', status: 'ok', detail: 'OK' },
    { key: 'library-folders', label: 'Library folders present', status: 'ok', detail: 'OK' },
    { key: 'usenet-login', label: 'Usenet login', status: 'ok', detail: 'OK' },
    { key: 'sonarr-health', label: 'Sonarr health', status: 'ok', detail: 'OK' },
    { key: 'radarr-health', label: 'Radarr health', status: 'ok', detail: 'OK' },
  ];
  return {
    jellyfin: {
      state: 'ok',
      message: null,
      sessions: [{ title: 'Arrival (2016)', user: 'Guest 1', device: 'Living room TV', positionSeconds: 4400, durationSeconds: 6960, paused: false, mode: 'direct-play' }],
    },
    sabnzbd: {
      state: 'ok',
      message: null,
      paused: false,
      speedBytesPerSec: 38.2e6,
      total: 3,
      items: [
        { name: 'Show.S02E05.1080p.WEB', pct: 72, etaSeconds: 130, status: 'Downloading' },
        { name: 'Show.S02E06.1080p.WEB', pct: 12, etaSeconds: 360, status: 'Downloading' },
        { name: 'Film.2019.2160p.BluRay', pct: 0, etaSeconds: null, status: 'Queued' },
      ],
      loginProblem: null,
    },
    jellyseerr: {
      state: 'ok',
      message: null,
      pending: 2,
      requests: [
        { title: 'Dune: Part Two (2024)', requestedBy: 'Guest 1', createdAt: iso(now - 2 * H) },
        { title: 'Shōgun · Season 1', requestedBy: 'Guest 2', createdAt: iso(now - 26 * H) },
      ],
    },
    prowlarr: {
      state: 'ok',
      message: null,
      indexers: [
        { name: 'Indexer A', status: 'ok', detail: 'OK' },
        { name: 'Indexer B', status: 'warn', detail: '912 / 1000 API calls today' },
        { name: 'Indexer C', status: 'ok', detail: 'OK' },
      ],
    },
    sonarr: { state: 'ok', message: null, health: [] },
    radarr: { state: 'ok', message: null, health: [] },
    checks,
  };
}

function heldMedia(names = Object.keys(META).filter((n) => META[n].mediaGated)) {
  const label = { jellyfin: 'Jellyfin', sabnzbd: 'SABnzbd', jellyseerr: 'Jellyseerr', prowlarr: 'Prowlarr', sonarr: 'Sonarr', radarr: 'Radarr' };
  const m = {};
  for (const n of ['jellyfin', 'sabnzbd', 'jellyseerr', 'prowlarr', 'sonarr', 'radarr']) {
    m[n] = names.includes(n) ? { state: 'held', message: `${label[n]} is held by the gate.` } : { state: 'ok', message: null };
  }
  m.checks = [
    { key: 'download-client', label: 'Download client reachable', status: 'idle', detail: 'Held by gate' },
    { key: 'library-folders', label: 'Library folders present', status: 'fail', detail: 'Missing: drive offline' },
    { key: 'usenet-login', label: 'Usenet login', status: 'idle', detail: 'Unknown' },
    { key: 'sonarr-health', label: 'Sonarr health', status: 'idle', detail: 'Sonarr is held by the gate.' },
    { key: 'radarr-health', label: 'Radarr health', status: 'idle', detail: 'Radarr is held by the gate.' },
  ];
  return m;
}

export function normal(now) {
  const bootedAt = now - (3 * D + 8 * H);
  const today = day(now);
  const months = monthsBack(now, 6);
  const subs = [
    { key: 'usenet-provider', name: 'Usenet provider', date: addDays(today, 8) },
    { key: 'indexer-a', name: 'Indexer A', date: addDays(today, 23) },
    { key: 'indexer-b', name: 'Indexer B', date: addDays(today, 129) },
  ];
  return {
    schema: 1,
    generatedAt: iso(now),
    server: { name: 'demo-host', version: '2.0.0', timeZone: 'UTC' },
    access: 'tailnet',
    rehearsal: false,
    helper: { reachable: true, error: null },
    docker: { state: 'active' },
    gate: {
      status: 'ok',
      line: 'Drive found, checked and mounted at /media (Oct 21 06:01). Apps released.',
      since: null,
      powercycled: false,
      attempts: 0,
      mediaStopped: false,
      mountPath: '/media',
      currentBoot: 'a91f',
      lastPassAt: iso(bootedAt + 82e3),
      retryMinutes: 5,
      nextRetryAt: null,
      canRerun: false,
      rerunLock: 'The drive is verified and mounted. A re-run only acts when it has failed or been lost.',
      history: [{ boot: 'a91f', current: true, startedAt: iso(bootedAt + 82e3), events: [ev(iso(bootedAt + 82e3), 'pass', 'fsck clean, mounted')] }, ...olderGroups(now)],
      action: null,
    },
    apps: NAMES.map((n) => app(n, now, n === 'sabnzbd' ? { startedAt: iso(now - D - 2 * H), restartCount: 1 } : {})),
    host: {
      name: 'demo-host',
      load: [0.42, 0.38, 0.35],
      memTotal: 15.5 * 1024 ** 3,
      memUsed: 5.1 * 1024 ** 3,
      rootTotal: 500e9,
      rootUsed: 205e9,
      rootUsedPct: 41,
      bootedAt: iso(bootedAt),
      rebootRequired: false,
      rebootPackages: [],
    },
    drive: {
      mountPath: '/media',
      mounted: true,
      stale: false,
      lastKnownAt: null,
      error: null,
      total: 4e12,
      used: 1.36e12,
      free: 2.64e12,
      usedPct: 34,
      warnPct: 90,
      warn: false,
      split: { tv: 812e9, movies: 486e9, downloads: 62e9, computedAt: iso(now - 9 * H) },
      samples: samples(now),
      forecast: {
        method: 'least-squares',
        windowDays: 30,
        samplesInWindow: 30,
        slopeBytesPerDay: 6e9,
        full: { date: addDays(today, 440), inDays: 440, reached: false },
        at90: { date: addDays(today, 374), inDays: 374, reached: false },
        at97: { date: addDays(today, 420), inDays: 420, reached: false },
      },
      forecastReason: null,
      samplesInWindow: 30,
    },
    backups: { maxAgeHours: 36, lastPull: iso(now - 17 * H), status: 'ok', lastOk: iso(now - 3 * H), archive: 'appdata-demo.tar', sizeBytes: 18.4e9, lastRunOk: true },
    uptime: {
      box: {
        source: 'boots',
        pct30d: 99.95,
        days: boxDays(now, { 25: 99.72, 4: 99.03 }),
        downs: [
          { start: iso(now - 4 * D - 8 * H - 240e3), end: iso(now - 4 * D - 8 * H), seconds: 240, reason: 'Power-cycle by the boot gate' },
          { start: iso(now - 25 * D), end: iso(now - 25 * D + 14 * 60e3), seconds: 840, reason: 'Reboot or power loss' },
        ],
      },
      apps: NAMES.map((n) => ({ name: n, months: months.map((month, i) => ({ month, pct: META[n].mediaGated ? [100, 99.4, 100, 98.6, 99.5, 99.7][i] : [100, 100, 100, 99.9, 100, 100][i] })) })),
      driveFailures: months.map((month, i) => ({ month, count: [0, 1, 0, 2, 1, 1][i] })),
    },
    media: media(now),
    push: {
      devices: [
        { id: '1111aaaa2222bbbb', label: 'iPhone', created: iso(now - 24 * D) },
        { id: '3333cccc4444dddd', label: 'MacBook · Safari', created: iso(now - 22 * D) },
        { id: '5555eeee6666ffff', label: 'test', created: iso(now - 19 * D) },
      ],
      storeError: false,
      alerts: [
        { ts: iso(now - 4 * D - 8 * H + 341e3), title: 'Media drive back', body: 'Mounted on retry 2. Apps released.', tag: 'bootgate', delivered: 3, failed: 0, state: 'resolved' },
        { ts: iso(now - 4 * D - 8 * H), title: 'Media drive failed', body: 'Gate gave up after a power-cycle. Docker held.', tag: 'bootgate', delivered: 3, failed: 0, state: 'resolved' },
        { ts: iso(now - 5 * D), title: 'Test from homelab', body: 'Sent from the dashboard.', tag: 'test', delivered: 3, failed: 0, state: 'info' },
        { ts: iso(now - 6 * D), title: 'sonarr crashed and restarted', body: 'Exited (137) and came back on its own.', tag: 'alertfloor:sonarr', delivered: 3, failed: 0, state: 'resolved', note: 'Recovered on its own' },
      ],
      alertsAvailable: true,
    },
    subscriptions: subs.map((s) => ({ ...s, daysLeft: Math.round((Date.parse(`${s.date}T00:00:00Z`) - Date.parse(`${today}T00:00:00Z`)) / D), remindOn: addDays(s.date, -7), reminderDue: false, reminded: false })),
    settings: { thresholds: { diskWarnPct: 90, backupMaxAgeH: 36 } },
    links: {
      runbooks: [
        { title: 'Failed boot gate', url: 'https://example.org/runbooks/failed-boot-gate.md' },
        { title: 'Restore from scratch', url: 'https://example.org/runbooks/restore-from-scratch.md' },
      ],
    },
    issues: [],
  };
}

// Missing at boot after a power-cycle: the gate holds all of Docker, every container is held (CORRECTIONS 2).
export function badNight(now) {
  const s = normal(now);
  const bootedAt = now - (4 * H + 12 * 60e3);
  const t = (min) => iso(bootedAt + min * 60e3);
  s.docker = { state: 'inactive' };
  s.host = { ...s.host, load: [0.21, 0.3, 0.62], memUsed: 2.2 * 1024 ** 3, bootedAt: iso(bootedAt), rebootRequired: true, rebootPackages: ['linux-image-generic'] };
  s.gate = {
    ...s.gate,
    status: 'failed',
    line: 'Media drive missing since 02:31 after a power-cycle. 8 apps held. Retrying every 5 min.',
    since: iso(bootedAt - 4 * 60e3),
    powercycled: true,
    attempts: 1,
    currentBoot: '4e1c',
    nextRetryAt: iso(now + 222e3),
    canRerun: true,
    rerunLock: null,
    history: [
      { boot: '4e1c', current: true, startedAt: t(6), events: [ev(t(6), 'fail', 'Gave up; docker stays down', 30)] },
      { boot: 'b2c3', current: false, startedAt: t(1.4), events: [ev(t(3), 'powercycle', 'USB port 2 power-cycled', 30), ev(t(1.4), 'fail', 'sdb not present after 90 s', 90)] },
      { boot: 'a91f', current: false, startedAt: iso(now - 3 * D), events: [ev(iso(bootedAt - 4 * 60e3), 'media-lost', '/media dropped: I/O errors on sdb', null), ev(iso(now - 3 * D), 'pass', 'fsck clean, mounted')] },
      ...olderGroups(now),
    ],
  };
  s.apps = NAMES.map((n) => app(n, now, { status: 'held', noPs: true }));
  s.drive = { ...s.drive, mounted: false, stale: true, lastKnownAt: iso(now - D) };
  s.media = heldMedia();
  s.backups = { ...s.backups, lastOk: iso(now - 11 * H) };
  s.push.alerts = [
    { ts: t(6), title: 'Media drive failed', body: 'Gate gave up after a power-cycle. Docker held. Retrying every 5 min.', tag: 'bootgate', delivered: 3, failed: 0, state: 'active' },
    { ts: iso(bootedAt - 4 * 60e3), title: 'Media drive lost', body: '/media dropped with I/O errors. Media apps stopped.', tag: 'drive', delivered: 3, failed: 0, state: 'active' },
    { ts: iso(now - 9 * H), title: 'Reboot needed', body: 'Kernel and libc updates are waiting.', tag: null, delivered: 3, failed: 0, state: 'info' },
    ...s.push.alerts,
  ];
  s.uptime.driveFailures = s.uptime.driveFailures.map((f, i, all) => (i === all.length - 1 ? { ...f, count: 2 } : f));
  s.issues = [{ severity: 'fail', text: 'Media drive failed after a power-cycle. 8 apps held.', target: 'drive' }];
  return s;
}

// Lost while running: the watcher stopped the media stack only.
export function mediaLost(now) {
  const s = normal(now);
  const lostAt = now - 20 * 60e3;
  s.gate = {
    ...s.gate,
    status: 'media-lost',
    line: 'Media drive lost at 13:48. Media apps stopped (6 held). Retrying every 5 min.',
    since: iso(lostAt),
    nextRetryAt: iso(now + 95e3),
    canRerun: true,
    rerunLock: null,
    history: [{ ...s.gate.history[0], events: [ev(iso(lostAt), 'media-lost', '/media dropped: I/O errors on sdb', null), ...s.gate.history[0].events] }, ...s.gate.history.slice(1)],
  };
  s.apps = NAMES.map((n) => (META[n].mediaGated ? app(n, now, { status: 'held', state: 'exited' }) : app(n, now)));
  s.drive = { ...s.drive, mounted: false, stale: true, lastKnownAt: iso(lostAt) };
  s.media = heldMedia();
  s.push.alerts = [{ ts: iso(lostAt), title: 'Media drive lost', body: 'Media drive lost; media apps stopped.', tag: 'bootgate', delivered: 3, failed: 0, state: 'active' }, ...s.push.alerts];
  s.issues = [{ severity: 'fail', text: 'Media drive lost at 13:48. 6 apps held.', target: 'drive' }];
  return s;
}

// Docker isn't running while the gate is fine (someone stopped it, or it failed). With live-restore the
// containers may still serve, so the server reports them unknown (CONTRACTS §11.3, amended).
export function dockerDown(now) {
  const s = normal(now);
  s.docker = { state: 'inactive' };
  s.apps = NAMES.map((n) => app(n, now, { status: 'unknown', noPs: true }));
  s.media = heldMedia([]);
  const label = { jellyfin: 'Jellyfin', sabnzbd: 'SABnzbd', jellyseerr: 'Jellyseerr', prowlarr: 'Prowlarr', sonarr: 'Sonarr', radarr: 'Radarr' };
  for (const n of Object.keys(label)) s.media[n] = { state: 'unknown', message: `${label[n]}'s state is unknown.` };
  s.media.checks = s.media.checks.map((c) => ({ ...c, status: 'idle', detail: c.key === 'library-folders' ? 'OK' : 'Unknown' }));
  s.issues = [{ severity: 'fail', text: "Docker isn't running.", target: 'apps' }];
  return s;
}

export function clean(now) {
  const s = normal(now);
  const bootedAt = now - 2 * D;
  s.host = { ...s.host, bootedAt: iso(bootedAt) };
  s.gate = { ...s.gate, currentBoot: 'a91f', lastPassAt: iso(bootedAt + 82e3), history: [{ boot: 'a91f', current: true, startedAt: iso(bootedAt + 82e3), events: [ev(iso(bootedAt + 82e3), 'pass', 'fsck clean, mounted')] }] };
  s.apps = NAMES.map((n) => app(n, now, { image: PINNED[n], startedAt: iso(bootedAt + 2 * 60e3) }));
  s.drive = {
    ...s.drive,
    used: 16e9,
    free: 3.984e12,
    usedPct: 0.4,
    split: { tv: 0, movies: 0, downloads: 16e9, computedAt: iso(now - H) },
    samples: samples(now, { days: 3, start: 15e9, perDay: 0.5e9 }),
    forecast: null,
    forecastReason: 'not-enough-samples',
    samplesInWindow: 3,
  };
  s.backups = { maxAgeHours: 36, lastPull: null, status: 'none', lastOk: null, archive: null, sizeBytes: null, lastRunOk: null };
  s.uptime = { box: { source: 'boots', pct30d: 100, days: boxDays(now).map((d, i) => (i < 28 ? { ...d, pct: null } : d)), downs: [] }, apps: NAMES.map((n) => ({ name: n, months: monthsBack(now, 6).map((month, i) => ({ month, pct: i === 5 ? 100 : null })) })), driveFailures: monthsBack(now, 6).map((month) => ({ month, count: 0 })) };
  s.media = media(now);
  s.media.jellyfin.sessions = [];
  s.media.sabnzbd = { ...s.media.sabnzbd, items: [], total: 0, speedBytesPerSec: 0 };
  s.media.jellyseerr = { state: 'unconfigured', message: 'Add the Jellyseerr API key in Settings.' };
  s.media.prowlarr.indexers = [];
  s.push = { devices: [], storeError: false, alerts: [], alertsAvailable: true };
  s.subscriptions = [];
  s.links = { runbooks: [] };
  s.issues = [];
  return s;
}

// One app's API unreachable: only the Downloads tile and its health row show it.
export function sabDown(now) {
  const s = normal(now);
  s.media.sabnzbd = {
    state: 'error',
    message: "SABnzbd isn't answering",
    detail: 'Timed out after 5 s at 14:19. The container is running, so this may clear on its own.',
    since: iso(now - 4 * 60e3),
  };
  s.media.checks[0] = { key: 'download-client', label: 'Download client reachable', status: 'fail', detail: 'Not answering' };
  s.media.checks[2] = { key: 'usenet-login', label: 'Usenet login', status: 'idle', detail: 'Unknown' };
  s.issues = [{ severity: 'warn', text: "SABnzbd's API isn't answering.", target: 'media', app: 'sabnzbd' }];
  return s;
}

// A messy day: one app down, one unhealthy, the drive at 92 %, a failed backup, a failing indexer.
export function troubled(now) {
  const s = normal(now);
  s.apps = NAMES.map((n) =>
    n === 'radarr' ? app(n, now, { status: 'down', state: 'exited' }) : n === 'sonarr' ? app(n, now, { status: 'unhealthy', health: 'unhealthy' }) : app(n, now),
  );
  s.drive = { ...s.drive, used: 3.68e12, free: 0.32e12, usedPct: 92, warn: true };
  s.backups = { ...s.backups, status: 'failed', lastRunOk: false };
  s.media.prowlarr.indexers[2] = { name: 'Indexer C', status: 'fail', detail: 'Failing since 11:02' };
  s.subscriptions[0] = { ...s.subscriptions[0], date: addDays(day(now), 3), daysLeft: 3 };
  s.issues = undefined; // exercise the client-side fallback derivation
  return s;
}

export const SCENARIOS = {
  normal,
  bad: badNight,
  'docker-held': badNight,
  'media-lost': mediaLost,
  'docker-down': dockerDown,
  clean,
  sab: sabDown,
  troubled,
  rehearsal: (now) => ({ ...normal(now), rehearsal: true }),
  remote: (now) => {
    const s = normal(now);
    return {
      ...s,
      access: 'remote',
      apps: s.apps.map((a) => ({ ...a, locks: { ...a.locks, update: 'Only available on the tailnet' } })),
      gate: { ...s.gate, canRerun: false, rerunLock: 'Only available on the tailnet' },
    };
  },
};

// The real server's summaries for its own test scenarios (scripts/dump-server-summaries.mjs), served as-is.
const REAL_DIR = new URL('./server-summaries/', import.meta.url);
export const REAL = Object.fromEntries(
  fs
    .readdirSync(REAL_DIR)
    .filter((f) => f.endsWith('.json'))
    .map((f) => [f.slice(0, -5), JSON.parse(fs.readFileSync(new URL(f, REAL_DIR), 'utf8'))]),
);
for (const [k, v] of Object.entries(REAL)) SCENARIOS[`real-${k}`] = () => structuredClone(v);

export const LOG_LINES = [
  ['INF', '[INF] Starting up on the pinned image'],
  ['INF', '[INF] Loaded configuration from /config'],
  ['INF', '[INF] Listening on :8080'],
  ['DBG', '[DBG] Health endpoint ready'],
  ['INF', '[INF] Task "Refresh library" started'],
  ['INF', '[INF] Task "Refresh library" finished in 82 s'],
  ['WRN', '[WRN] Slow response from upstream (2.4 s)'],
  ['ERR', '[ERR] Indexer C request failed: HTTP 503'],
  ['INF', '[INF] Retrying in 60 s'],
  ['INF', '[INF] Indexer C request OK'],
  ['DBG', '[DBG] RSS sync: 14 releases, 0 grabbed'],
  ['DBG', '[DBG] RSS sync: 9 releases, 1 grabbed'],
];
