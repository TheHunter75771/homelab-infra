// Fixture host trees and fake media APIs for the five scenarios the handoff designs:
// normal (a normal day), bad-night (drive failed after a power-cycle, Docker held), clean (clean start),
// one-app-down (Radarr exited + SABnzbd's API timing out), docker-inactive (gate ok but docker.service down).
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createECDH, randomBytes } from 'node:crypto';
import webpush from 'web-push';
import { buildConfig } from '../../src/config.js';
import { container, ALL_APPS } from './fake-helper.js';

export const NOW = Date.parse('2026-10-24T14:08:00Z');
export const TZ = 'UTC';
export const KEYS = {
  sonarr: 'a'.repeat(31) + '1',
  radarr: 'b'.repeat(31) + '2',
  prowlarr: 'c'.repeat(31) + '3',
  sabnzbd: 'd'.repeat(31) + '4',
  jellyfin: 'e'.repeat(31) + '5',
  jellyseerr: 'MTY5ODc2NTQzMjEwOTg3NjU0MzIx',
};
const BOOT = (p) => `${p}0000-0000-0000-0000-000000000000`;
export const BOOTS = { current: '4e1c', prev: 'b2c3', older: 'a91f', oct20: '77b0', oct12: '5a12' };

export function tempDir(t, prefix = 'dash-') {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), prefix));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  return dir;
}

export function fakeSubscription(host = 'fcm.googleapis.com') {
  const ecdh = createECDH('prime256v1');
  ecdh.generateKeys();
  return {
    endpoint: `https://${host}/fcm/send/${randomBytes(24).toString('base64url')}`,
    expirationTime: null,
    keys: { p256dh: ecdh.getPublicKey().toString('base64url'), auth: randomBytes(16).toString('base64url') },
  };
}

const ev = (ts, boot, event, detail, wait = 0) => JSON.stringify({ ts, boot_id: BOOT(boot), event, detail, wait_seconds: wait });

const GATE_HISTORY = {
  normal: [
    ev('2026-10-12T06:00:48Z', '5a12', 'repair', 'fsck fixed 2 errors on sdb1'),
    ev('2026-10-12T06:01:10Z', '5a12', 'pass', 'fsck clean, mounted'),
    ev('2026-10-20T03:16:02Z', '77b0', 'fail', 'sdb not present after 30 s', 30),
    ev('2026-10-20T03:16:38Z', '77b0', 'powercycle', 'USB port 2 power-cycled', 30),
    ev('2026-10-20T03:21:40Z', '77b0', 'retry-pass', 'Mounted on retry 2 after power-cycle', 300),
    ev('2026-10-21T06:01:22Z', '4e1c', 'pass', 'fsck clean, mounted'),
  ],
  'bad-night': [
    ev('2026-10-20T03:16:02Z', '77b0', 'fail', 'sdb not present after 30 s', 30),
    ev('2026-10-20T03:21:40Z', '77b0', 'retry-pass', 'Mounted on retry 2', 300),
    ev('2026-10-21T06:01:00Z', 'a91f', 'pass', 'fsck clean, mounted'),
    ev('2026-10-24T02:31:00Z', 'a91f', 'media-lost', '/media dropped: I/O errors on sdb'),
    ev('2026-10-24T02:36:41Z', 'b2c3', 'fail', 'sdb not present after 90 s', 90),
    ev('2026-10-24T02:38:16Z', 'b2c3', 'powercycle', 'USB port 2 power-cycled', 30),
    ev('2026-10-24T02:41:03Z', '4e1c', 'fail', 'Gave up; docker stays down', 30),
  ],
  clean: [ev('2026-10-24T13:00:22Z', '4e1c', 'pass', 'fsck clean, mounted')],
};

const NOTIFY = {
  normal: [
    { ts: '2026-10-19T20:04:00Z', title: 'Test push', body: 'Sent from the dashboard.', tag: 'test', delivered: 3, failed: 0 },
    { ts: '2026-10-20T03:12:00Z', title: 'Media drive lost', body: 'media apps stopped', tag: 'bootgate', delivered: 3, failed: 0 },
    { ts: '2026-10-20T03:21:00Z', title: 'Media drive back', body: 'Verified again; docker running.', tag: 'bootgate', delivered: 3, failed: 0 },
  ],
  'bad-night': [
    { ts: '2026-10-20T03:12:00Z', title: 'Media drive lost', body: 'media apps stopped', tag: 'bootgate', delivered: 3, failed: 0 },
    { ts: '2026-10-20T03:21:00Z', title: 'Media drive back', body: 'Verified again.', tag: 'bootgate', delivered: 3, failed: 0 },
    { ts: '2026-10-24T02:31:00Z', title: 'Media drive lost', body: 'media apps stopped', tag: 'bootgate', delivered: 3, failed: 0 },
    { ts: '2026-10-24T02:41:00Z', title: 'Media drive failed', body: 'Gave up after a power-cycle.', tag: 'bootgate', delivered: 3, failed: 0 },
  ],
};

function write(root, rel, content, mode) {
  const p = path.join(root, rel);
  fs.mkdirSync(path.dirname(p), { recursive: true });
  fs.writeFileSync(p, content);
  if (mode !== undefined) fs.chmodSync(p, mode);
  return p;
}

// statfs results for the fake filesystem: media 4.0 TB, root 100 GB.
export function makeStatfs({ mediaUsed = 1.36e12, mediaTotal = 4e12, rootPct = 41, mediaHang = false } = {}) {
  const bsize = 4096;
  const fsOf = (total, used) => {
    const blocks = Math.round(total / bsize);
    const bfree = blocks - Math.round(used / bsize);
    return { type: 0xef53, bsize, blocks, bfree, bavail: bfree, files: 0, ffree: 0 };
  };
  const calls = [];
  const statfs = async (p) => {
    calls.push(p);
    if (p.endsWith('/media')) {
      if (mediaHang) return new Promise(() => {});
      return fsOf(mediaTotal, mediaUsed);
    }
    return fsOf(100e9, (100e9 * rootPct) / 100);
  };
  statfs.calls = calls;
  return statfs;
}

// Fake `systemctl show` for the retry timer / service.
export function makeRun({ nextRetryAt = NOW + 222_000, retryStates = ['inactive'] } = {}) {
  const calls = [];
  let i = 0;
  const run = async (cmd, args) => {
    calls.push([cmd, ...args]);
    if (args.includes('NextElapseUSecRealtime')) {
      return { code: 0, stdout: `NextElapseUSecRealtime=@${Math.floor(nextRetryAt / 1000)}\nNextElapseUSecMonotonic=0\n` };
    }
    if (args.includes('ActiveState')) {
      const st = retryStates[Math.min(i, retryStates.length - 1)];
      i += 1;
      return { code: 0, stdout: `ActiveState=${st}\n` };
    }
    return { code: 1, stdout: '' };
  };
  run.calls = calls;
  return run;
}

const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

// Fake media APIs. `hang`: apps whose API never answers; `httpError`: app -> status.
export function makeFetch({ scenario = 'normal', hang = [], httpError = {}, indexerFailing = false } = {}) {
  const calls = [];
  const fetchImpl = async (url, init = {}) => {
    const u = new URL(url);
    const h = init.headers ?? {};
    calls.push({ url: u.pathname + u.search, headers: h });
    const app = { 8096: 'jellyfin', 8080: 'sabnzbd', 5055: 'jellyseerr', 9696: 'prowlarr', 8989: 'sonarr', 7878: 'radarr' }[u.port];
    if (hang.includes(app)) {
      return new Promise((_, reject) => init.signal?.addEventListener('abort', () => reject(init.signal.reason), { once: true }));
    }
    if (httpError[app]) return json({ message: 'nope' }, httpError[app]);
    const keyOk =
      app === 'jellyfin'
        ? h.Authorization === `MediaBrowser Token="${KEYS.jellyfin}"`
        : app === 'sabnzbd'
          ? u.searchParams.get('apikey') === KEYS.sabnzbd
          : h['X-Api-Key'] === KEYS[app];
    if (!keyOk) return json({ error: 'unauthorized' }, 401);
    const clean = scenario === 'clean';
    switch (app) {
      case 'jellyfin':
        return json(
          clean
            ? []
            : [
                {
                  UserName: 'Guest 1',
                  DeviceName: 'Living room TV',
                  NowPlayingItem: { Name: 'Arrival', ProductionYear: 2016, RunTimeTicks: 6960e7 },
                  PlayState: { PositionTicks: 4400e7, PlayMethod: 'DirectPlay', IsPaused: false },
                },
                { UserName: 'Guest 2', DeviceName: 'Phone' },
              ],
        );
      case 'sabnzbd':
        if (u.searchParams.get('mode') === 'warnings') return json({ warnings: [] });
        return json({
          queue: {
            paused: false,
            kbpersec: clean ? '0' : '39116.8',
            noofslots_total: clean ? 0 : 3,
            slots: clean
              ? []
              : [
                  { filename: 'Show.S02E05.1080p.WEB', percentage: '72', timeleft: '0:02:10', status: 'Downloading' },
                  { filename: 'Film.2019.2160p.BluRay', percentage: '0', timeleft: '0:00:00', status: 'Queued' },
                ],
          },
        });
      case 'jellyseerr':
        if (u.pathname.endsWith('/request/count')) return json({ pending: clean ? 0 : 2, approved: 5 });
        if (u.pathname.endsWith('/api/v1/request')) {
          return json({
            results: clean
              ? []
              : [
                  { id: 1, type: 'movie', media: { tmdbId: 693134, mediaType: 'movie' }, requestedBy: { displayName: 'Guest 1' }, createdAt: '2026-10-24T12:08:00Z' },
                  { id: 2, type: 'tv', media: { tmdbId: 126308, mediaType: 'tv' }, seasons: [{ seasonNumber: 1 }], requestedBy: { displayName: 'Guest 2' }, createdAt: '2026-10-23T12:00:00Z' },
                ],
          });
        }
        if (u.pathname.endsWith('/movie/693134')) return json({ title: 'Dune: Part Two', releaseDate: '2024-02-27' });
        if (u.pathname.endsWith('/tv/126308')) return json({ name: 'Shōgun' });
        return json({}, 404);
      case 'prowlarr':
        if (u.pathname.endsWith('/indexer')) {
          return json([
            { id: 1, name: 'Indexer A', enable: true, fields: [] },
            { id: 2, name: 'Indexer B', enable: true, fields: [{ name: 'baseSettings.queryLimit', value: 1000 }] },
            { id: 3, name: 'Indexer C', enable: true, fields: [] },
          ]);
        }
        if (u.pathname.endsWith('/indexerstatus')) {
          return json(indexerFailing ? [{ indexerId: 3, initialFailure: '2026-10-24T11:02:00Z', mostRecentFailure: '2026-10-24T14:00:00Z', disabledTill: '2026-10-24T15:00:00Z' }] : []);
        }
        if (u.pathname.endsWith('/indexerstats')) return json({ indexers: [{ indexerId: 2, numberOfQueries: 912 }] });
        return json({}, 404);
      case 'sonarr':
      case 'radarr':
        return json([]);
      default:
        return json({}, 404);
    }
  };
  fetchImpl.calls = calls;
  return fetchImpl;
}

export function helperFor(scenario, now = NOW) {
  const iso = (ms) => new Date(ms).toISOString();
  // The helper's `boots` format: journalctl --list-boots, 32-hex ids, ISO first/last entries.
  const b = (index, id, first, last) => ({ index, boot_id: `${id}${'0'.repeat(28)}`, first_entry: first, last_entry: last });
  const boots = {
    normal: [
      b(-2, '5a12', '2026-10-12T06:00:00Z', '2026-10-20T03:12:00Z'),
      b(-1, '77b0', '2026-10-20T03:16:00Z', '2026-10-21T05:59:00Z'),
      b(0, '4e1c', '2026-10-21T06:00:00Z', iso(now)),
    ],
    'bad-night': [
      b(-2, 'a91f', '2026-10-21T06:00:00Z', '2026-10-24T02:33:00Z'),
      b(-1, 'b2c3', '2026-10-24T02:35:00Z', '2026-10-24T02:38:20Z'),
      b(0, '4e1c', '2026-10-24T02:40:00Z', iso(now)),
    ],
    clean: [b(0, '4e1c', '2026-10-24T13:00:00Z', iso(now))],
  };
  const s = scenario === 'one-app-down' || scenario === 'docker-inactive' ? 'normal' : scenario;
  const base = { boots: boots[s] ?? boots.normal };
  if (scenario === 'bad-night') {
    return { ...base, dockerState: 'inactive', split: { sizes: { tv: 812e9, movies: 486e9, downloads: 62e9 }, computed_at: '2026-10-24T03:00:00Z', cached: true, stale: true, reason: 'gate status failed' } };
  }
  if (scenario === 'docker-inactive') return { ...base, dockerState: 'inactive' };
  if (scenario === 'one-app-down') {
    return { ...base, containers: ALL_APPS.map((n) => (n === 'radarr' ? container(n, { state: 'exited', health: null, restart_count: 3 }) : container(n))) };
  }
  if (scenario === 'clean') return { ...base, lastPull: null, split: { sizes: { tv: 0, movies: 0, downloads: 16e9 }, computed_at: '2026-10-24T13:30:00Z', cached: false, stale: false } };
  return base;
}

// Builds the host tree for a scenario and the matching dashboard config.
export function buildHost(t, scenario = 'normal', { helperSocket = '/nonexistent.sock', webDir, devices = 3, extraConfig = {} } = {}) {
  const root = tempDir(t);
  const s = scenario === 'one-app-down' || scenario === 'docker-inactive' ? 'normal' : scenario;
  const bad = s === 'bad-night';
  const clean = s === 'clean';
  // /proc
  write(root, 'proc/loadavg', bad ? '0.21 0.30 0.62 1/123 4567\n' : '0.42 0.38 0.35 2/345 6789\n');
  write(root, 'proc/meminfo', `MemTotal:       16252928 kB\nMemFree:         1000000 kB\nMemAvailable:   ${bad ? 13946880 : 10905600} kB\n`);
  write(root, 'proc/uptime', `${bad ? 15120 : 288000}.12 1000.00\n`);
  write(root, 'proc/sys/kernel/random/boot_id', `${BOOT(BOOTS.current)}\n`);
  const mediaLine = '120 29 8:17 / /media rw,noatime shared:66 - ext4 /dev/sdb1 rw\n';
  write(root, 'proc/self/mountinfo', `22 1 259:2 / / rw,relatime shared:1 - ext4 /dev/nvme0n1p2 rw\n${bad ? '' : mediaLine}`);
  // gate
  write(root, 'var/lib/bootgate/status', `${bad ? 'failed' : 'ok'}\n`);
  write(root, 'var/lib/bootgate/attempts', `${bad ? 1 : 0}\n`);
  write(root, 'var/lib/bootgate/history.jsonl', `${GATE_HISTORY[s].join('\n')}\n`);
  fs.mkdirSync(path.join(root, 'run/bootgate'), { recursive: true });
  if (bad) {
    write(root, 'run/reboot-required', '*** System restart required ***\n');
    write(root, 'run/reboot-required.pkgs', 'linux-image-7.0.0-12-generic\nlibc6\nlibc6\n');
  }
  // backups
  if (!clean) {
    write(
      root,
      'var/lib/homelab-backup/status.json',
      JSON.stringify({ last_ok: bad ? '2026-10-23T03:10:00Z' : '2026-10-24T03:12:00Z', archive: '/srv/homelab/backups/appdata/appdata-20261024T031200Z.tar', size_bytes: 18.4e9, ok: true }),
    );
  }
  // notifier history
  if (NOTIFY[s]) write(root, 'var/lib/homelab-notify/history.jsonl', `${NOTIFY[s].map((x) => JSON.stringify(x)).join('\n')}\n`);
  // push store
  const pushDir = path.join(root, 'srv/homelab/push');
  fs.mkdirSync(pushDir, { recursive: true });
  const vk = webpush.generateVAPIDKeys();
  const vapid = { publicKey: vk.publicKey, privateKey: vk.privateKey, subject: 'https://example.org/homelab-alerts' };
  write(root, 'srv/homelab/push/vapid.json', JSON.stringify(vapid));
  const subs = [];
  if (!clean) {
    const labels = ['iPhone', 'MacBook · Safari', 'test'];
    for (let i = 0; i < devices; i++) {
      const sub = fakeSubscription(i === 1 ? 'web.push.apple.com' : 'fcm.googleapis.com');
      subs.push({ endpoint: sub.endpoint, keys: sub.keys, label: labels[i] ?? `device ${i}`, created: `2026-09-${String(30 - i).padStart(2, '0')}T12:00:00.000Z` });
    }
    write(root, 'srv/homelab/push/subscriptions.json', `${JSON.stringify(subs, null, 2)}\n`);
  }
  // app config files with API keys
  for (const app of ['sonarr', 'radarr', 'prowlarr']) {
    write(root, `srv/homelab/data/${app}/config.xml`, `<Config>\n  <UrlBase>/${app}</UrlBase>\n  <ApiKey>${KEYS[app]}</ApiKey>\n</Config>\n`);
  }
  write(root, 'srv/homelab/data/sabnzbd/sabnzbd.ini', `[misc]\nurl_base = /sabnzbd\napi_key = ${KEYS.sabnzbd}\nnzb_key = ${'f'.repeat(32)}\n[servers]\napi_key = wrong\n`);
  // dashboard state: settings with Jellyfin/Jellyseerr keys, samples, last-known drive
  const stateDir = path.join(root, 'var/lib/homelab-dashboard');
  fs.mkdirSync(stateDir, { recursive: true });
  write(
    root,
    'var/lib/homelab-dashboard/settings.json',
    JSON.stringify({
      thresholds: { diskWarnPct: 90, backupMaxAgeH: 36 },
      subscriptions: [
        { key: 'indexer-a', name: 'Indexer A', date: '2026-11-16' },
        { key: 'usenet', name: 'Usenet provider', date: '2026-10-29' },
      ],
      integrations: { jellyfinApiKey: KEYS.jellyfin, jellyseerrApiKey: KEYS.jellyseerr },
    }),
    0o600,
  );
  if (!clean) {
    const lines = [];
    for (let d = 40; d >= 1; d--) {
      const date = new Date(NOW - d * 86_400_000).toISOString().slice(0, 10);
      lines.push(JSON.stringify({ date, ts: `${date}T03:00:00Z`, used: 1.36e12 - 6e9 * d, total: 4e12 }));
    }
    write(root, 'var/lib/homelab-dashboard/samples.jsonl', `${lines.join('\n')}\n`);
  }
  if (bad) {
    write(root, 'var/lib/homelab-dashboard/drive-last.json', JSON.stringify({ total: 4e12, used: 1.355e12, free: 2.645e12, usedPct: 33.9, at: '2026-10-24T02:30:00.000Z' }));
  }
  const { config } = buildConfig(
    {
      display_name: 'testhost',
      timezone: TZ,
      state_dir: stateDir,
      push_dir: pushDir,
      data_dir: path.join(root, 'srv/homelab/data'),
      media_mount: '/media',
      root_path: '/',
      proc_dir: path.join(root, 'proc'),
      helper_socket: helperSocket,
      gate_dir: path.join(root, 'var/lib/bootgate'),
      gate_rerun_file: path.join(root, 'run/bootgate/rerun'),
      reboot_required_file: path.join(root, 'run/reboot-required'),
      backup_status_file: path.join(root, 'var/lib/homelab-backup/status.json'),
      notify_history_file: path.join(root, 'var/lib/homelab-notify/history.jsonl'),
      rehearsal_flag: path.join(root, 'etc/homelab/rehearsal'),
      web_dir: webDir ?? path.join(root, 'web'),
      integration_timeout_ms: 150,
      pinned_images: Object.fromEntries(ALL_APPS.map((n) => [n, `example/${n}:1.0.0@sha256:${'a'.repeat(64)}`])),
      ...extraConfig,
    },
    {},
  );
  return { root, cfg: config, vapid, subs, stateDir, pushDir, write: (rel, content, mode) => write(root, rel, content, mode), path: (rel) => path.join(root, rel) };
}

export function writeWeb(dir) {
  fs.mkdirSync(path.join(dir, 'assets'), { recursive: true });
  fs.writeFileSync(path.join(dir, 'index.html'), '<!doctype html><html><head><title>Homelab dashboard</title><script type="module" src="/assets/app.js"></script></head><body></body></html>');
  fs.writeFileSync(path.join(dir, 'sw.js'), "self.addEventListener('push', () => {});\n");
  fs.writeFileSync(path.join(dir, 'assets/app.js'), 'export {};\n');
  fs.writeFileSync(path.join(dir, 'manifest.webmanifest'), '{"name":"Homelab dashboard"}');
  fs.writeFileSync(path.join(dir, 'assets/inter.woff2'), Buffer.from([0x77, 0x4f, 0x46, 0x32]));
}
