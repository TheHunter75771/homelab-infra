// Media integrations, one tile per app with its own timeout: a slow or broken app marks only its own tile.
// Apps that aren't running are never called (no pointless timeouts on a bad night).
//
// tile.state: ok | error | held | down | stopped | unconfigured
// API keys: Sonarr/Radarr/Prowlarr <ApiKey> in config.xml, SABnzbd api_key in sabnzbd.ini (read-only, from
// {{ homelab_data_dir }}); Jellyfin/Jellyseerr from settings. Keys go into request headers (SABnzbd only
// accepts a query parameter), never into logs, errors or responses.
import path from 'node:path';
import { readText } from '../util/fsx.js';
import { shortTime } from '../util/time.js';

const RUNNING = new Set(['healthy', 'unhealthy', 'starting']);
const NAMES = { jellyfin: 'Jellyfin', sabnzbd: 'SABnzbd', jellyseerr: 'Jellyseerr', prowlarr: 'Prowlarr', sonarr: 'Sonarr', radarr: 'Radarr' };

export class IntegrationError extends Error {
  constructor(kind, message, status = null) {
    super(message);
    this.name = 'IntegrationError';
    this.kind = kind; // timeout | http | network | parse
    this.status = status;
  }
}

export function readArrKey(xml) {
  const m = String(xml ?? '').match(/<ApiKey>\s*([A-Za-z0-9]{16,64})\s*<\/ApiKey>/);
  return m ? m[1] : null;
}

export function readSabKey(ini) {
  let section = '';
  for (const raw of String(ini ?? '').split('\n')) {
    const line = raw.trim();
    const sec = line.match(/^\[([^\]]+)\]$/);
    if (sec) {
      section = sec[1].toLowerCase();
      continue;
    }
    const m = section === 'misc' && line.match(/^api_key\s*=\s*([A-Za-z0-9]{8,64})\s*$/);
    if (m) return m[1];
  }
  return null;
}

// SABnzbd timeleft: "M:SS", "H:MM:SS" or, past a day, "D:HH:MM:SS" -> seconds
export function parseSabTimeleft(s) {
  if (typeof s !== 'string' || !/^\d+(:\d+){1,3}$/.test(s)) return null;
  const parts = s.split(':').map(Number);
  const [d, h, m, sec] = [0, 0, 0, 0].slice(parts.length).concat(parts);
  return d * 86_400 + h * 3600 + m * 60 + sec;
}

export function createMedia({ cfg, settings, fetchImpl = globalThis.fetch, secrets, timeZone, clock = () => Date.now() }) {
  const timeoutMs = cfg.integrationTimeoutMs;
  // When each app's current failure streak began: the tile says "since" that time and stays byte-stable while
  // the failure continues, so SSE change detection doesn't fire every 30 s during an outage.
  const failingSince = new Map();

  async function getJson(url, headers = {}) {
    let res;
    try {
      res = await fetchImpl(url, { headers: { Accept: 'application/json', ...headers }, signal: AbortSignal.timeout(timeoutMs), redirect: 'error' });
    } catch (err) {
      if (err?.name === 'TimeoutError' || err?.name === 'AbortError') {
        throw new IntegrationError('timeout', `Timed out after ${Math.max(1, Math.ceil(timeoutMs / 1000))} s`);
      }
      const code = err?.cause?.code || err?.code;
      throw new IntegrationError('network', code === 'ECONNREFUSED' ? 'Not answering (connection refused)' : 'Not answering');
    }
    if (!res.ok) {
      try {
        await res.body?.cancel();
      } catch {
        // ignore
      }
      throw new IntegrationError('http', `HTTP ${res.status}`, res.status);
    }
    try {
      return await res.json();
    } catch {
      throw new IntegrationError('parse', 'Answered with something that is not JSON');
    }
  }

  function keyFor(app) {
    if (app === 'jellyfin' || app === 'jellyseerr') return settings.apiKey(app) || null;
    const file = cfg.integrations[app]?.keyFile;
    if (!file) return null;
    let text = null;
    try {
      text = readText(path.isAbsolute(file) ? file : path.join(cfg.dataDir, file));
    } catch {
      return null; // EACCES: reported as unconfigured
    }
    const key = app === 'sabnzbd' ? readSabKey(text) : readArrKey(text);
    if (key) secrets?.add(key);
    return key;
  }

  // Text copied from an app reaches the browser only redacted (keys, apikey=/API Key:/token values, bearer
  // tokens), and redaction runs before truncation so a cut can't leave part of a key (CONTRACTS §11.3).
  const text = (v, max = 300) => {
    if (v === undefined || v === null) return null;
    const red = secrets ? secrets.redact(String(v)) : String(v);
    return red.length > max ? `${red.slice(0, max)}…` : red;
  };

  // Register the *arr/SABnzbd keys from their config files whether or not the app is running: a crash-looping
  // app prints its own key into the very logs the dashboard shows.
  function registerKeys() {
    for (const app of Object.keys(cfg.integrations)) if (cfg.integrations[app]?.keyFile) keyFor(app);
  }

  const base = (app) => String(cfg.integrations[app]?.baseUrl ?? '').replace(/\/+$/, '');

  const fetchers = {
    async jellyfin(key) {
      const list = await getJson(`${base('jellyfin')}/Sessions?activeWithinSeconds=960`, { Authorization: `MediaBrowser Token="${key}"` });
      if (!Array.isArray(list)) throw new IntegrationError('parse', 'Unexpected answer');
      const sessions = list
        .filter((s) => s?.NowPlayingItem)
        .map((s) => {
          const it = s.NowPlayingItem;
          const episode = it.SeriesName
            ? `${it.SeriesName}${Number.isInteger(it.ParentIndexNumber) && Number.isInteger(it.IndexNumber) ? ` · S${String(it.ParentIndexNumber).padStart(2, '0')}E${String(it.IndexNumber).padStart(2, '0')}` : ''}${it.Name ? ` · ${it.Name}` : ''}`
            : null;
          const method = s.PlayState?.PlayMethod;
          return {
            title: text(episode ?? `${it.Name ?? 'Unknown'}${it.ProductionYear ? ` (${it.ProductionYear})` : ''}`),
            user: text(s.UserName, 100),
            device: text(s.DeviceName, 100),
            positionSeconds: Number.isFinite(s.PlayState?.PositionTicks) ? Math.round(s.PlayState.PositionTicks / 1e7) : null,
            durationSeconds: Number.isFinite(it.RunTimeTicks) ? Math.round(it.RunTimeTicks / 1e7) : null,
            paused: Boolean(s.PlayState?.IsPaused),
            mode: method === 'Transcode' ? 'transcode' : method === 'DirectStream' ? 'direct-stream' : 'direct-play',
          };
        });
      return { sessions };
    },

    async sabnzbd(key) {
      const q = encodeURIComponent(key);
      const data = await getJson(`${base('sabnzbd')}/api?mode=queue&output=json&limit=10&apikey=${q}`);
      const queue = data?.queue;
      if (!queue || typeof queue !== 'object') throw new IntegrationError('parse', 'Unexpected answer');
      let warnings = [];
      try {
        const w = await getJson(`${base('sabnzbd')}/api?mode=warnings&output=json&apikey=${q}`);
        warnings = (Array.isArray(w?.warnings) ? w.warnings : [])
          .map((x) => (typeof x === 'string' ? x : x?.text))
          .filter((t) => typeof t === 'string')
          .slice(-20);
      } catch {
        // warnings are optional; the queue answered
      }
      const kbps = Number.parseFloat(queue.kbpersec);
      return {
        paused: queue.paused === true || queue.paused === 'True',
        speedBytesPerSec: Number.isFinite(kbps) ? Math.round(kbps * 1024) : 0,
        total: Number.parseInt(queue.noofslots_total ?? queue.noofslots ?? 0, 10) || 0,
        items: (Array.isArray(queue.slots) ? queue.slots : []).slice(0, 10).map((s) => ({
          name: text(s.filename ?? '') ?? '',
          pct: Math.max(0, Math.min(100, Number.parseFloat(s.percentage) || 0)),
          etaSeconds: s.status === 'Downloading' ? parseSabTimeleft(s.timeleft) : null,
          status: String(s.status ?? ''),
        })),
        loginProblem: text(warnings.find((t) => /login|authenticat|credential|account|password/i.test(t)) ?? null),
      };
    },

    async jellyseerr(key) {
      const h = { 'X-Api-Key': key };
      const count = await getJson(`${base('jellyseerr')}/api/v1/request/count`, h);
      const list = await getJson(`${base('jellyseerr')}/api/v1/request?take=5&skip=0&filter=pending&sort=added`, h);
      const results = Array.isArray(list?.results) ? list.results : [];
      const requests = await Promise.all(
        results.map(async (r) => {
          const type = r?.type ?? r?.media?.mediaType;
          const tmdb = r?.media?.tmdbId;
          let title = `Request #${r?.id ?? '?'}`;
          if (Number.isInteger(tmdb) && (type === 'movie' || type === 'tv')) {
            try {
              const d = await getJson(`${base('jellyseerr')}/api/v1/${type}/${tmdb}`, h);
              const name = type === 'movie' ? d?.title : d?.name;
              const year = String((type === 'movie' ? d?.releaseDate : d?.firstAirDate) ?? '').slice(0, 4);
              if (name) title = type === 'movie' && year ? `${name} (${year})` : name;
            } catch {
              // keep the fallback title
            }
          }
          const seasons = Array.isArray(r?.seasons) ? r.seasons.map((s) => s?.seasonNumber).filter(Number.isInteger) : [];
          if (type === 'tv' && seasons.length) title += ` · Season${seasons.length > 1 ? 's' : ''} ${seasons.join(', ')}`;
          return { title: text(title), requestedBy: text(r?.requestedBy?.displayName ?? null, 100), createdAt: r?.createdAt ?? null };
        }),
      );
      return { pending: Number.isInteger(count?.pending) ? count.pending : requests.length, requests };
    },

    async prowlarr(key) {
      const h = { 'X-Api-Key': key };
      const [indexers, statuses] = await Promise.all([
        getJson(`${base('prowlarr')}/api/v1/indexer`, h),
        getJson(`${base('prowlarr')}/api/v1/indexerstatus`, h),
      ]);
      let stats = [];
      try {
        const start = new Date(clock());
        start.setHours(0, 0, 0, 0);
        const s = await getJson(`${base('prowlarr')}/api/v1/indexerstats?startDate=${encodeURIComponent(start.toISOString())}`, h);
        stats = Array.isArray(s?.indexers) ? s.indexers : [];
      } catch {
        // usage is optional
      }
      const now = clock();
      const rows = (Array.isArray(indexers) ? indexers : []).map((ix) => {
        const st = (Array.isArray(statuses) ? statuses : []).find((x) => x?.indexerId === ix.id);
        const used = stats.find((x) => x?.indexerId === ix.id)?.numberOfQueries;
        const limit = (Array.isArray(ix.fields) ? ix.fields : []).find((f) => f?.name === 'baseSettings.queryLimit')?.value;
        if (ix.enable === false) return { name: text(ix.name, 100), status: 'idle', detail: 'Disabled' };
        const disabledTill = Date.parse(st?.disabledTill ?? '');
        if (st && (Number.isFinite(disabledTill) ? disabledTill > now : Boolean(st.mostRecentFailure))) {
          const since = st.initialFailure ?? st.mostRecentFailure;
          return { name: text(ix.name, 100), status: 'fail', detail: since ? `Failing since ${shortTime(since, timeZone)}` : 'Failing' };
        }
        if (Number.isFinite(limit) && limit > 0 && Number.isFinite(used) && used >= 0.9 * limit) {
          return { name: text(ix.name, 100), status: 'warn', detail: `${used} / ${limit} API calls today` };
        }
        return { name: text(ix.name, 100), status: 'ok', detail: 'OK' };
      });
      return { indexers: rows };
    },

    async sonarr(key) {
      return { health: arrHealth(await getJson(`${base('sonarr')}/api/v3/health`, { 'X-Api-Key': key })) };
    },
    async radarr(key) {
      return { health: arrHealth(await getJson(`${base('radarr')}/api/v3/health`, { 'X-Api-Key': key })) };
    },
  };

  function arrHealth(list) {
    if (!Array.isArray(list)) throw new IntegrationError('parse', 'Unexpected answer');
    return list.map((h) => ({
      source: text(h?.source ?? '', 100) ?? '',
      level: h?.type === 'error' ? 'fail' : h?.type === 'warning' ? 'warn' : 'info',
      message: text(h?.message ?? '') ?? '',
    }));
  }

  async function tile(app, appStatus) {
    if (!RUNNING.has(appStatus)) {
      failingSince.delete(app);
      const state = appStatus === 'held' ? 'held' : appStatus === 'stopped' ? 'stopped' : 'down';
      const message = state === 'held' ? `${NAMES[app]} is held by the gate.` : state === 'stopped' ? `${NAMES[app]} is stopped.` : `${NAMES[app]} isn't running.`;
      return { state, message };
    }
    const key = keyFor(app);
    if (!key) {
      const message =
        app === 'jellyfin' || app === 'jellyseerr'
          ? `Add the ${NAMES[app]} API key in Settings.`
          : `Can't read ${NAMES[app]}'s API key from its config file.`;
      return { state: 'unconfigured', message };
    }
    try {
      const data = await fetchers[app](key);
      failingSince.delete(app);
      return { state: 'ok', message: null, ...data };
    } catch (err) {
      const reason = err instanceof IntegrationError ? err.message : 'Unexpected error';
      if (!failingSince.has(app)) failingSince.set(app, new Date(clock()).toISOString());
      const since = failingSince.get(app);
      return {
        state: 'error',
        message: `${NAMES[app]} isn't answering`,
        detail: `${reason} at ${shortTime(since, timeZone)}.${appStatus === 'healthy' ? ' The container is running, so this may clear on its own.' : ''}`,
        since,
      };
    }
  }

  return {
    // appStatuses: {name: status}; driveMounted: bool
    registerKeys,
    async collect(appStatuses, { driveMounted }) {
      registerKeys();
      const apps = Object.keys(fetchers);
      const tiles = await Promise.all(apps.map((a) => tile(a, appStatuses[a] ?? 'down')));
      const media = Object.fromEntries(apps.map((a, i) => [a, tiles[i]]));
      media.checks = buildChecks(media, { driveMounted });
      return media;
    },
  };
}

function worst(levels) {
  return levels.includes('fail') ? 'fail' : levels.includes('warn') ? 'warn' : 'ok';
}

export function buildChecks(media, { driveMounted }) {
  const sab = media.sabnzbd;
  const sabRow =
    sab.state === 'ok'
      ? { status: 'ok', detail: 'OK' }
      : sab.state === 'error'
        ? { status: 'fail', detail: 'Not answering' }
        : sab.state === 'held'
          ? { status: 'idle', detail: 'Held by gate' }
          : sab.state === 'stopped'
            ? { status: 'idle', detail: 'Stopped' }
            : sab.state === 'unconfigured'
              ? { status: 'idle', detail: 'API key not readable' }
              : { status: 'fail', detail: "SABnzbd isn't running" };
  const arrIssues = ['sonarr', 'radarr'].flatMap((a) => (media[a].state === 'ok' ? media[a].health : []));
  const rootFolder = arrIssues.find((h) => /rootfolder/i.test(h.source) && h.level !== 'info');
  const library = !driveMounted
    ? { status: 'fail', detail: 'Missing: drive offline' }
    : rootFolder
      ? { status: rootFolder.level, detail: rootFolder.message }
      : { status: 'ok', detail: 'OK' };
  const login =
    sab.state !== 'ok'
      ? { status: 'idle', detail: 'Unknown' }
      : sab.loginProblem
        ? { status: 'fail', detail: sab.loginProblem.slice(0, 160) }
        : { status: 'ok', detail: 'OK' };
  const arrRow = (a) => {
    const t = media[a];
    if (t.state !== 'ok') return { status: t.state === 'error' ? 'fail' : 'idle', detail: t.state === 'error' ? 'Not answering' : t.message };
    const problems = t.health.filter((h) => h.level !== 'info');
    if (!problems.length) return { status: 'ok', detail: 'OK' };
    return { status: worst(problems.map((p) => p.level)), detail: problems.length === 1 ? problems[0].message : `${problems.length} problems` };
  };
  return [
    { key: 'download-client', label: 'Download client reachable', ...sabRow },
    { key: 'library-folders', label: 'Library folders present', ...library },
    { key: 'usenet-login', label: 'Usenet login', ...login },
    { key: 'sonarr-health', label: 'Sonarr health', ...arrRow('sonarr') },
    { key: 'radarr-health', label: 'Radarr health', ...arrRow('radarr') },
  ];
}
