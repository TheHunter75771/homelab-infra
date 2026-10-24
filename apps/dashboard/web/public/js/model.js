// Selectors: turn a raw /api/summary into what the screens show. Pure functions only, so they run in
// node:test as well as the browser.
//
// The server (apps/dashboard/server, schema 1) already decides most things: app status and lock reasons,
// the gate line, the issue list, alert states, the forecast. This module prefers those and only derives a
// value itself when the server didn't send it. The fields read are listed in ../../README.md.

import {
  toDate,
  fmtClock,
  fmtDayTime,
  daysBetween,
  parseDay,
  dayKey,
  dayKeyOffset,
  monthKeyOffset,
  imageTag,
  imageDigest,
  plural,
  setTimeZone,
} from './format.js';

// Known containers (CORRECTIONS 1): fallback labels, purposes and links when the server sends none.
// ADR 0015: the dashboard has 443 to itself; the path apps are on 8444 and Jellyseerr on 8443.
export const APP_META = {
  jellyfin: { label: 'Jellyfin', desc: 'Streams the library to TVs and phones', open: { port: 8444, path: '/watch' } },
  jellyseerr: { label: 'Jellyseerr', desc: 'Takes requests for new shows and films', open: { port: 8443, path: '/' } },
  sonarr: { label: 'Sonarr', desc: 'Finds and grabs TV episodes', open: { port: 8444, path: '/sonarr' } },
  radarr: { label: 'Radarr', desc: 'Finds and grabs films', open: { port: 8444, path: '/radarr' } },
  prowlarr: { label: 'Prowlarr', desc: 'Keeps the indexers in one place', open: { port: 8444, path: '/prowlarr' } },
  sabnzbd: { label: 'SABnzbd', desc: 'Downloads from Usenet', open: { port: 8444, path: '/sabnzbd' } },
  cloudflared: { label: 'cloudflared', desc: 'Tunnel for the public /watch route', open: null },
  'wol-relay': { label: 'wol-relay', desc: 'Wakes the gaming PC from the tailnet', open: null },
};
const APP_ORDER = Object.keys(APP_META);
// The media stack: what the drop-out watcher stops when the drive is lost while running.
export const MEDIA_STACK = new Set(['jellyfin', 'jellyseerr', 'prowlarr', 'radarr', 'sabnzbd', 'sonarr']);

export const STATUS = {
  healthy: { tone: 'ok', label: 'Healthy', icon: 'check-circle' },
  unhealthy: { tone: 'warn', label: 'Unhealthy', icon: 'warning' },
  starting: { tone: 'info', label: 'Starting', icon: 'circle-notch', spin: true },
  updating: { tone: 'info', label: 'Updating', icon: 'circle-notch', spin: true },
  held: { tone: 'idle', label: 'Held by gate', icon: 'pause-circle' },
  stopped: { tone: 'idle', label: 'Stopped on purpose', icon: 'pause-circle' },
  down: { tone: 'fail', label: 'Down', icon: 'x-circle' },
  unknown: { tone: 'idle', label: 'Unknown', icon: 'question' },
};

export const GATE_CHIP = {
  ok: { tone: 'ok', icon: 'check-circle' },
  waiting: { tone: 'info', icon: 'hourglass' },
  failed: { tone: 'fail', icon: 'x-circle' },
  'media-lost': { tone: 'fail', icon: 'plugs' },
  unknown: { tone: 'idle', icon: 'question' },
};

export const GATE_EVENT = {
  pass: { tone: 'ok', icon: 'check' },
  repair: { tone: 'info', icon: 'wrench' },
  powercycle: { tone: 'warn', icon: 'lightning' },
  fail: { tone: 'fail', icon: 'x' },
  'retry-pass': { tone: 'ok', icon: 'arrow-clockwise' },
  'media-lost': { tone: 'fail', icon: 'plugs' },
};

export const DEFAULT_THRESHOLDS = { diskWarnPct: 90, backupMaxAgeH: 36 };
export const FAIL_PCT = 97;

// First present key; tolerates camelCase or snake_case.
export function pick(obj, ...keys) {
  if (!obj || typeof obj !== 'object') return undefined;
  for (const k of keys) if (obj[k] !== undefined && obj[k] !== null) return obj[k];
  return undefined;
}
const arr = (v) => (Array.isArray(v) ? v : []);
const num = (v) => (v === null || v === undefined || v === '' ? NaN : Number(v));
const ms = (v) => toDate(v)?.getTime() ?? NaN;
const isWebUrl = (u) => {
  try {
    return ['http:', 'https:'].includes(new URL(String(u)).protocol);
  } catch {
    return false;
  }
};
// A YYYY-MM-DD or an ISO time.
const when = (v) => (typeof v === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(v) ? parseDay(v) : toDate(v));

// --- gate → apps ----------------------------------------------------------------------------

export function gateStatus(summary) {
  const s = pick(summary?.gate, 'status');
  return ['ok', 'waiting', 'failed', 'media-lost'].includes(s) ? s : 'unknown';
}

export function dockerState(summary) {
  const d = summary?.docker;
  if (d && typeof d === 'object') return d.state ?? 'unknown';
  return typeof d === 'string' ? d : 'active';
}

// Is this app held by the boot gate? (CORRECTIONS 2)
// - failed at boot: the gate holds all of Docker, so every container is held.
// - media-lost while running: the watcher stops the media stack; cloudflared and wol-relay keep running.
// - waiting: Docker waits for the gate.
export function heldByGate(app, gate, docker) {
  const running = app.state === 'running';
  const dockerUp = docker === 'active';
  if (gate === 'failed' || gate === 'waiting') return !dockerUp || !running;
  if (gate === 'media-lost') return (app.mediaGated ?? MEDIA_STACK.has(app.name)) && !running;
  return false;
}

// One of the STATUS keys. The server's status wins; this is the fallback and the optimistic overlay.
export function appStatus(app, { gate = 'ok', docker = 'active', pending = null } = {}) {
  if (app.action && ['update', 'restart', 'start'].includes(app.action.kind)) return app.action.kind === 'update' ? 'updating' : 'starting';
  if (pending && !app.action) {
    if (pending.kind === 'update') return 'updating';
    if (pending.kind === 'restart' || pending.kind === 'start') return 'starting';
    if (pending.kind === 'stop' && app.state === 'running') return 'stopped';
  }
  if (STATUS[app.status]) return app.status;
  if (heldByGate(app, gate, docker)) return 'held';
  if (docker !== 'active') return 'stopped';
  switch (app.state) {
    case 'running':
      if (app.health === 'unhealthy') return 'unhealthy';
      if (app.health === 'starting') return 'starting';
      return 'healthy';
    case 'restarting':
    case undefined:
    case null:
      return 'down';
    default:
      return app.stoppedOnPurpose || app.state === 'paused' ? 'stopped' : 'down';
  }
}

export function normalizeApp(raw) {
  const name = String(pick(raw, 'name', 'id') ?? '').trim();
  const meta = APP_META[name] || { label: name, desc: '', open: null };
  const image = pick(raw, 'image') ?? '';
  const pinned = pick(raw, 'pinned') ?? '';
  const pinnedTag = pick(raw, 'pinnedTag') ?? imageTag(pinned);
  let updateAvailable = pick(raw, 'updateAvailable');
  if (updateAvailable === undefined) {
    const pd = imageDigest(pinned);
    const rd = imageDigest(image);
    updateAvailable = Boolean(pinned && image) && (pd && rd ? pd !== rd : imageTag(pinned) !== imageTag(image));
  }
  const restarts = num(pick(raw, 'restartCount', 'restart_count'));
  return {
    name,
    label: pick(raw, 'label') ?? meta.label,
    desc: pick(raw, 'purpose', 'desc') ?? meta.desc,
    open: raw && 'open' in raw ? raw.open : meta.open,
    state: pick(raw, 'state') ?? null,
    health: pick(raw, 'health') ?? null,
    status: pick(raw, 'status'),
    stoppedReason: ['intentional', 'docker-down', 'gate'].includes(raw?.stoppedReason) ? raw.stoppedReason : null,
    stoppedOnPurpose: raw?.stoppedReason === 'intentional' || Boolean(pick(raw, 'stoppedOnPurpose', 'intentional')),
    mediaGated: pick(raw, 'mediaGated'),
    startedAt: pick(raw, 'startedAt', 'started_at') ?? null,
    restartCount: Number.isFinite(restarts) ? restarts : null,
    image,
    tag: pick(raw, 'tag') ?? imageTag(image || pinned),
    pinned,
    pinnedTag,
    updateAvailable: Boolean(updateAvailable),
    stack: pick(raw, 'stack') ?? (MEDIA_STACK.has(name) ? 'media' : ''),
    locks: raw?.locks && typeof raw.locks === 'object' ? raw.locks : null,
    action: raw?.action && typeof raw.action === 'object' ? raw.action : null,
  };
}

export function sortApps(apps) {
  const rank = (n) => {
    const i = APP_ORDER.indexOf(n);
    return i === -1 ? APP_ORDER.length : i;
  };
  return [...apps].sort((a, b) => rank(a.name) - rank(b.name) || a.name.localeCompare(b.name));
}

// Href for an app's Open button: another `tailscale serve` port on this host (ADR 0015), or a path on this
// origin. The path must start with exactly one "/" and contain no "\\": "//evil" and "/\\evil" are
// protocol-relative to browsers. Anything else gives no link.
export function openHref(open, loc = globalThis.location) {
  if (!open || typeof open !== 'object') return null;
  const path = open.path === undefined || open.path === null ? '/' : open.path;
  if (typeof path !== 'string' || !/^\/(?!\/)/.test(path) || path.includes('\\')) return null;
  if (open.port !== undefined && open.port !== null) {
    const port = Number(open.port);
    if (!Number.isInteger(port) || port <= 0 || port > 65535 || !loc?.hostname) return null;
    return `${loc.protocol || 'https:'}//${loc.hostname}:${port}${path}`;
  }
  return path;
}

export function deriveApps(summary, pending = {}, now = Date.now()) {
  const gate = gateStatus(summary);
  const docker = dockerState(summary);
  return sortApps(arr(summary?.apps).map(normalizeApp).filter((a) => a.name)).map((a) => {
    const status = appStatus(a, { gate, docker, pending: pending[a.name] });
    const started = toDate(a.startedAt);
    const up = a.state === 'running' && !['held', 'stopped', 'down', 'unknown'].includes(status);
    // Why a stopped app is stopped: the server's stoppedReason, else inferred (Docker not active → docker-down).
    const stopKind = status !== 'stopped' ? null : a.stoppedReason ?? (docker !== 'active' ? 'docker-down' : 'intentional');
    const st = stopKind && stopKind !== 'intentional' ? { ...STATUS.stopped, label: stopKind === 'gate' ? 'Stopped by the gate' : 'Stopped' } : STATUS[status];
    return {
      ...a,
      statusKey: status,
      stopKind,
      st,
      uptimeSeconds: up && started ? Math.max(0, (now - started.getTime()) / 1000) : null,
      next: a.updateAvailable ? a.pinnedTag || 'pinned' : null,
    };
  });
}

// --- alerts ---------------------------------------------------------------------------------

const RECOVERY = /\b(back|recovered|resolved|restored|online|healthy again|ok again|passed|cleared)\b/i;
const INFO_TAGS = new Set(['test', 'reminder']);

export function alertKind(alert) {
  if (!alert.tag || INFO_TAGS.has(alert.tag)) return 'info';
  if (RECOVERY.test(alert.title || '')) return 'recovery';
  return 'problem';
}

// Newest first. The server's `state` wins. Fallback rules (the server's): untagged, test and reminder
// alerts are info; a recovery ("... back", "recovered") is resolved and resolves every earlier problem with
// its tag; an unresolved problem older than 7 days becomes info.
export function alertStates(alerts, now = Date.now()) {
  const list = arr(alerts)
    .map((a) => ({
      ts: pick(a, 'ts', 'time'),
      title: String(pick(a, 'title') ?? ''),
      body: String(pick(a, 'body') ?? ''),
      tag: pick(a, 'tag') || null,
      serverState: pick(a, 'state'),
    }))
    .filter((a) => toDate(a.ts))
    .sort((x, y) => ms(y.ts) - ms(x.ts));
  const recovered = new Set(); // tags with a recovery newer than the alert being looked at
  return list.map((a) => {
    const kind = alertKind(a);
    let state;
    if (kind === 'info') state = 'info';
    else if (kind === 'recovery') state = 'resolved';
    else state = recovered.has(a.tag) ? 'resolved' : now - ms(a.ts) > 7 * 86400000 ? 'info' : 'active';
    if (kind === 'recovery') recovered.add(a.tag);
    if (['active', 'resolved', 'info'].includes(a.serverState)) state = a.serverState;
    return { ...a, kind, state };
  });
}

export const ALERT_CHIP = {
  active: { tone: 'fail', label: 'Active', icon: 'bell-ringing' },
  resolved: { tone: 'ok', label: 'Resolved', icon: 'check-circle' },
  info: { tone: 'info', label: 'Info', icon: 'info' },
};

// --- drive forecast -------------------------------------------------------------------------

export function normalizeSamples(samples) {
  return arr(samples)
    .map((s) => ({ t: ms(when(pick(s, 'date', 'ts', 'day'))), y: num(pick(s, 'used', 'usedBytes', 'used_bytes')), total: num(pick(s, 'total')) }))
    .filter((p) => Number.isFinite(p.t) && Number.isFinite(p.y))
    .sort((a, b) => a.t - b.t);
}

// Least-squares fit over the last 30 days; fewer than 7 samples gives none (CONTRACTS §11.3).
// Only used when the server sent no forecast.
export function computeForecast(samples, totalBytes, usedNow, now = Date.now()) {
  const total = num(totalBytes);
  const pts = normalizeSamples(samples).filter((p) => p.t <= now + 86400000 && now - p.t < 30 * 86400000);
  if (pts.length < 7 || !Number.isFinite(total) || total <= 0) return null;
  const xs = pts.map((p) => (p.t - now) / 86400000);
  const mx = xs.reduce((a, b) => a + b, 0) / xs.length;
  const my = pts.reduce((a, p) => a + p.y, 0) / pts.length;
  let sxy = 0;
  let sxx = 0;
  xs.forEach((x, i) => {
    sxy += (x - mx) * (pts[i].y - my);
    sxx += (x - mx) ** 2;
  });
  const perDay = sxx > 0 ? sxy / sxx : 0;
  const base = Number.isFinite(num(usedNow)) ? num(usedNow) : my - perDay * mx;
  const at = (pct) => {
    const target = (pct / 100) * total;
    if (base >= target) return new Date(now);
    if (perDay <= 0) return null;
    return new Date(now + ((target - base) / perDay) * 86400000);
  };
  return { perDayBytes: perDay, fullAt: perDay > 0 ? at(100) : null, at, samples: pts.length };
}

// The server's forecast {slopeBytesPerDay, full, at90, at97} (dates as YYYY-MM-DD), or null with a reason.
export function driveForecast(drive, now = Date.now()) {
  if (!drive) return null;
  const f = drive.forecast;
  if (f && typeof f === 'object' && (f.full || f.slopeBytesPerDay !== undefined)) {
    const perDay = num(f.slopeBytesPerDay);
    const used = num(drive.used);
    const total = num(drive.total);
    const marks = { 100: f.full, 90: f.at90, 97: f.at97 };
    return {
      perDayBytes: perDay,
      fullAt: when(f.full?.date),
      samples: num(f.samplesInWindow) || null,
      at: (pct) => {
        if (marks[pct]?.date) return when(marks[pct].date);
        if (!(perDay > 0) || !Number.isFinite(used) || !Number.isFinite(total)) return null;
        const target = (pct / 100) * total;
        return used >= target ? new Date(now) : new Date(now + ((target - used) / perDay) * 86400000);
      },
    };
  }
  if (drive.forecastReason === 'not-growing') return { perDayBytes: 0, fullAt: null, at: () => null, samples: num(drive.samplesInWindow) || null };
  if (drive.forecastReason === 'not-enough-samples' || f === null) return null;
  return computeForecast(drive.samples, drive.total, drive.used, now);
}

// Geometry for the 600×180 forecast chart: the last 60 days of samples + the projection to full.
export function forecastChart(drive, forecast, { now = Date.now(), warnPct = 90 } = {}) {
  const total = num(drive?.total);
  if (!forecast || !(total > 0)) return null;
  const start = now - 60 * 86400000;
  const full = forecast.fullAt ? forecast.fullAt.getTime() : NaN;
  const end = Number.isFinite(full) ? Math.min(Math.max(full, now + 30 * 86400000), now + 3 * 365 * 86400000) : now + 365 * 86400000;
  const span = end - start;
  const X = (t) => ((t - start) / span) * 600;
  const Y = (pct) => 180 - Math.max(0, Math.min(100, pct)) * 1.8;
  const pts = normalizeSamples(drive.samples).filter((p) => p.t >= start && p.t <= now + 86400000);
  const pctOf = (p) => (p.y / (p.total > 0 ? p.total : total)) * 100;
  const line = pts.map((p) => `${X(Math.min(p.t, now)).toFixed(1)},${Y(pctOf(p)).toFixed(1)}`);
  const todayX = X(now);
  const usedPct = (num(drive.used) / total) * 100;
  const lastY = Number.isFinite(usedPct) ? usedPct : pts.length ? pctOf(pts[pts.length - 1]) : 0;
  let proj = null;
  if (forecast.perDayBytes > 0) {
    const endPct = lastY + ((forecast.perDayBytes * ((end - now) / 86400000)) / total) * 100;
    const endT = endPct > 100 && Number.isFinite(full) ? Math.min(full, end) : end;
    proj = `${todayX.toFixed(1)},${Y(lastY).toFixed(1)} ${X(endT).toFixed(1)},${Y(Math.min(100, endPct)).toFixed(1)}`;
  }
  const area = line.length ? `${line[0].split(',')[0]},180 ${line.join(' ')} ${line[line.length - 1].split(',')[0]},180` : '';
  const dWarn = forecast.at(warnPct);
  const dFail = forecast.at(FAIL_PCT);
  return {
    used: line.join(' '),
    area,
    proj,
    yWarn: Y(warnPct),
    yFail: Y(FAIL_PCT),
    todayX,
    todayPct: (todayX / 600) * 100,
    start: new Date(start),
    end: new Date(end),
    dWarn: dWarn && dWarn.getTime() <= end ? dWarn : null,
    dFail: dFail && dFail.getTime() <= end ? dFail : null,
    fullAt: Number.isFinite(full) ? forecast.fullAt : null,
    warnPct,
  };
}

// --- gate ------------------------------------------------------------------------------------

// Boot groups, newest first: [{boot, current, started, rows: [{ts, event, detail, wait}]}].
// Takes the server's grouped history, or a flat list of history.jsonl events.
export function historyGroups(gate, currentBoot = '') {
  const history = arr(gate?.history);
  const cur = String(currentBoot || pick(gate, 'currentBoot') || '').slice(0, 4);
  if (history.length && Array.isArray(history[0]?.events)) {
    return history.map((g) => ({
      boot: String(g.boot ?? '').slice(0, 4),
      current: Boolean(g.current),
      started: toDate(g.startedAt),
      rows: arr(g.events)
        .map((e) => ({ ts: e.ts, event: String(e.event ?? ''), detail: String(e.detail ?? ''), wait: pick(e, 'waitSeconds', 'wait_seconds') ?? null }))
        .sort((a, b) => ms(b.ts) - ms(a.ts)),
    }));
  }
  const events = history
    .map((e) => ({
      ts: pick(e, 'ts'),
      boot: String(pick(e, 'boot_id', 'bootId') ?? '').replace(/-/g, '').slice(0, 4),
      event: String(pick(e, 'event') ?? ''),
      detail: String(pick(e, 'detail') ?? ''),
      wait: pick(e, 'wait_seconds', 'waitSeconds') ?? null,
    }))
    .filter((e) => toDate(e.ts))
    .sort((a, b) => ms(b.ts) - ms(a.ts));
  const groups = [];
  const byBoot = new Map();
  for (const e of events) {
    let g = byBoot.get(e.boot);
    if (!g) {
      g = { boot: e.boot, current: Boolean(cur) && e.boot === cur, rows: [] };
      byBoot.set(e.boot, g);
      groups.push(g);
    }
    g.rows.push(e);
  }
  for (const g of groups) g.started = toDate(g.rows[g.rows.length - 1].ts);
  return groups;
}

const at = (d, now) => (d ? (daysBetween(d, now) === 0 ? fmtClock(d) : fmtDayTime(d)) : 'boot');

// Plain-English gate line (the server sends one; this is the fallback).
export function gateLine({ status, since, lastPassAt, held, retryMinutes = 5, mountPath = '/media', powercycled = false }, now = Date.now()) {
  switch (status) {
    case 'ok':
      return lastPassAt ? `Drive found, checked and mounted at ${mountPath} (${fmtDayTime(lastPassAt)}). Apps released.` : `Drive mounted at ${mountPath}. Apps released.`;
    case 'failed':
      return `Media drive missing since ${at(since, now)}${powercycled ? ' after a power-cycle' : ''}. ${plural(held, 'app')} held. Retrying every ${retryMinutes} min.`;
    case 'media-lost':
      return `Media drive lost at ${at(since, now)}. Media apps stopped (${held} held). Retrying every ${retryMinutes} min.`;
    case 'waiting':
      return 'Waiting for the media drive. The boot check is still running.';
    default:
      return 'Boot gate status is unknown (no status file).';
  }
}

// --- backups ---------------------------------------------------------------------------------

// status: ok | stale | failed | none | unknown (the server's), derived when missing.
export function backupView(backups, thresholds, now = Date.now()) {
  const b = backups || {};
  const lastOk = toDate(pick(b, 'lastOk', 'last_ok'));
  const size = num(pick(b, 'sizeBytes', 'size_bytes'));
  const lastPull = toDate(pick(b, 'lastPull', 'last_pull'));
  const ageH = lastOk ? (now - lastOk.getTime()) / 3600000 : null;
  let status = pick(b, 'status');
  if (!['ok', 'stale', 'failed', 'none', 'unknown'].includes(status)) {
    const lastRunOk = pick(b, 'lastRunOk', 'ok');
    if (lastRunOk === false) status = 'failed';
    else if (!lastOk) status = 'none';
    else status = ageH > (num(pick(b, 'maxAgeHours')) || thresholds.backupMaxAgeH) ? 'stale' : 'ok';
  }
  return { status, lastOk, size, lastPull, ageH };
}

// --- renewals --------------------------------------------------------------------------------

export function renewals(list, now = Date.now()) {
  return arr(list)
    .map((r, i) => {
      const date = parseDay(pick(r, 'date'));
      const days = date ? daysBetween(now, date) : NaN;
      const tone = !Number.isFinite(days) ? 'idle' : days <= 7 ? 'fail' : days <= 30 ? 'warn' : 'plain';
      return { key: String(pick(r, 'key', 'id') ?? `r${i}`), name: String(pick(r, 'name') ?? 'Subscription'), iso: date ? dayKey(date) : '', date, days, tone };
    })
    .sort((a, b) => (Number.isFinite(a.days) ? a.days : 1e9) - (Number.isFinite(b.days) ? b.days : 1e9));
}

// --- media -----------------------------------------------------------------------------------

export const MEDIA_LABEL = { jellyfin: 'Jellyfin', sabnzbd: 'SABnzbd', jellyseerr: 'Jellyseerr', prowlarr: 'Prowlarr', sonarr: 'Sonarr', radarr: 'Radarr' };
const TILE_STATES = new Set(['ok', 'error', 'held', 'down', 'stopped', 'unconfigured', 'pending']);

// A media tile: {state, message, detail, ...data}. Without a server state, the app's status decides.
export function mediaTile(raw, app) {
  const t = raw && typeof raw === 'object' ? raw : null;
  let state = t?.state;
  if (!TILE_STATES.has(state)) {
    if (app && ['held', 'stopped', 'down'].includes(app.statusKey)) state = app.statusKey;
    else if (!t) state = 'pending';
    else state = t.error ? 'error' : 'ok';
  }
  return { ...(t || {}), state, message: t?.message ?? null, detail: t?.detail ?? null };
}

export function sessionView(s) {
  const pos = num(pick(s, 'positionSeconds', 'position_s'));
  const dur = num(pick(s, 'durationSeconds', 'duration_s'));
  const mode = String(pick(s, 'mode', 'playMethod') ?? 'direct-play').toLowerCase();
  let chip = { tone: 'ok', label: 'Direct play', icon: 'lightning' };
  if (mode.includes('transcod')) chip = { tone: 'info', label: 'Transcoding', icon: 'cpu' };
  else if (mode.includes('stream')) chip = { tone: 'ok', label: 'Direct stream', icon: 'lightning' };
  return {
    title: String(pick(s, 'title') ?? 'Unknown title'),
    user: String(pick(s, 'user') ?? ''),
    device: String(pick(s, 'device') ?? ''),
    positionS: pos,
    durationS: dur,
    pct: dur > 0 && pos >= 0 ? Math.min(100, (pos / dur) * 100) : 0,
    paused: Boolean(pick(s, 'paused')),
    mode: chip,
  };
}

export function queueItemView(q) {
  const pct = num(pick(q, 'pct', 'percentage'));
  return {
    name: String(pick(q, 'name', 'filename') ?? ''),
    pct: Number.isFinite(pct) ? Math.max(0, Math.min(100, pct)) : 0,
    etaS: num(pick(q, 'etaSeconds', 'eta_s')),
    status: String(pick(q, 'status') ?? '').toLowerCase(),
  };
}

export function requestView(r) {
  return {
    title: String(pick(r, 'title') ?? ''),
    by: String(pick(r, 'requestedBy', 'by') ?? ''),
    at: pick(r, 'createdAt', 'at'),
  };
}

const HEALTH_ICON = { ok: 'check-circle', warn: 'warning', fail: 'x-circle', idle: 'pause-circle' };

// Rows for the Media health card: indexers first, then the server's checks.
export function mediaHealthRows(media) {
  const rows = [];
  const px = media.tiles.prowlarr;
  if (px.state === 'ok') {
    for (const ix of arr(px.indexers)) {
      const status = ['ok', 'warn', 'fail', 'idle'].includes(ix.status) ? ix.status : 'idle';
      rows.push({ label: String(ix.name ?? 'Indexer'), status, text: String(pick(ix, 'detail', 'text') ?? (status === 'ok' ? 'OK' : '')) });
    }
    if (!arr(px.indexers).length) rows.push({ label: 'Indexers', status: 'idle', text: 'None set up yet' });
  } else {
    const text = {
      held: 'Held by gate',
      stopped: 'Prowlarr stopped',
      down: "Prowlarr isn't running",
      error: "Prowlarr isn't answering",
      unconfigured: "Can't read Prowlarr's key",
      pending: 'Checking…',
    }[px.state];
    rows.push({ label: 'Indexers', status: px.state === 'error' || px.state === 'down' ? 'fail' : 'idle', text: text || 'No data' });
  }
  for (const c of media.checks) rows.push(c);
  return rows.map((r) => ({ ...r, tone: ['ok', 'warn', 'fail'].includes(r.status) ? r.status : 'idle', icon: HEALTH_ICON[r.status] || 'info' }));
}

export function mediaHealthSummary(rows) {
  const fails = rows.filter((r) => r.status === 'fail').length;
  const warns = rows.filter((r) => r.status === 'warn').length;
  if (fails) return { tone: 'fail', label: `${fails} failing` };
  if (warns) return { tone: 'warn', label: `${warns} near limit` };
  if (rows.length && rows.every((r) => r.status === 'idle')) return { tone: 'idle', label: rows.some((r) => /held/i.test(r.text)) ? 'Held by gate' : 'No data' };
  return { tone: 'ok', label: 'All OK' };
}

// --- issues (the overview banner) -------------------------------------------------------------

// Where an issue row leads. Backups have no screen of their own (their tile is on the overview).
export function issueRoute(target, app) {
  switch (target) {
    case 'drive':
      return { name: 'drive' };
    case 'apps':
      return app ? { name: 'app', id: app } : { name: 'apps' };
    case 'media':
      return { name: 'media' };
    case 'alerts':
      return { name: 'alerts' };
    case 'settings':
      return { name: 'settings' };
    default:
      return null;
  }
}

// Fallback when the server sent no `issues`: the same rules the server uses.
export function deriveIssues(view) {
  const out = [];
  const add = (level, text, to) => out.push({ level, text, to });
  const { gate, apps, drive, media, backups, renewalList } = view;
  const held = apps.filter((a) => a.statusKey === 'held').length;
  if (gate.status === 'failed') add('fail', `Media drive failed${gate.powercycled ? ' after a power-cycle' : ''}. ${plural(held, 'app')} held.`, { name: 'drive' });
  else if (gate.status === 'media-lost') add('fail', `Media drive lost${gate.since ? ` at ${fmtClock(gate.since)}` : ''}. ${plural(held, 'app')} held.`, { name: 'drive' });
  else if (gate.status === 'waiting') add('warn', 'Waiting for the media drive. The boot check is still running.', { name: 'drive' });
  else if (gate.status === 'unknown') add('warn', 'Boot gate status is unknown.', { name: 'drive' });
  if (view.helperReachable === false) add('warn', "Can't reach the Docker helper. App status is unknown.", { name: 'apps' });
  else if (view.docker !== 'active' && gate.status !== 'failed') add('fail', view.docker === 'failed' ? 'Docker failed to start.' : "Docker isn't running.", { name: 'apps' });
  for (const a of apps) {
    if (a.statusKey === 'down') add('fail', `${a.label} is down.`, { name: 'app', id: a.name });
    else if (a.statusKey === 'unhealthy') add('warn', `${a.label} is unhealthy.`, { name: 'app', id: a.name });
  }
  if (drive.warn) add('warn', `Media drive is ${Math.round(drive.pct)}% full.`, { name: 'drive' });
  for (const [name, t] of Object.entries(media.tiles)) if (t.state === 'error') add('warn', `${MEDIA_LABEL[name] || name}'s API isn't answering.`, { name: 'media' });
  if (media.tiles.prowlarr.state === 'ok') {
    for (const ix of arr(media.tiles.prowlarr.indexers)) {
      if (ix.status === 'fail') add('warn', `${ix.name}: ${String(ix.detail || 'failing').replace(/^Failing/, 'failing')}.`, { name: 'media' });
    }
  }
  for (const c of media.checks) {
    if (c.key === 'usenet-login' && c.status === 'fail') add('warn', 'Usenet login is failing.', { name: 'media' });
    if (c.key === 'library-folders' && c.status !== 'ok' && c.status !== 'idle' && drive.mounted) add('warn', 'A library folder is missing.', { name: 'media' });
  }
  if (backups.status === 'failed') add('fail', 'The last nightly backup failed.', null);
  else if (backups.status === 'stale' && backups.lastOk) add('warn', `Last good backup was ${Math.round(backups.ageH)} h ago.`, null);
  if (view.storeError) add('fail', 'The push subscription list is unreadable. Alerts may not arrive.', { name: 'alerts' });
  for (const r of renewalList) {
    if (!Number.isFinite(r.days)) continue;
    if (r.days < 0) add('warn', `${r.name} renewal date has passed.`, { name: 'settings' });
    else if (r.days <= 7) add('warn', `${r.name} renews in ${plural(r.days, 'day')}.`, { name: 'settings' });
  }
  return [...out.filter((i) => i.level === 'fail'), ...out.filter((i) => i.level !== 'fail')];
}

export function serverIssues(list) {
  return arr(list)
    .filter((i) => i && typeof i.text === 'string')
    .map((i) => ({ level: i.severity === 'fail' ? 'fail' : 'warn', text: i.text, to: issueRoute(i.target, i.app) }));
}

// --- the whole view --------------------------------------------------------------------------

export function buildView(summary, { pending = {}, now = Date.now() } = {}) {
  const s = summary || {};
  // Every time on screen in the host's zone when the server names it, matching its preformatted lines.
  setTimeZone(pick(s.server, 'timeZone', 'timezone') ?? null);
  const thresholds = { ...DEFAULT_THRESHOLDS };
  const t = s.settings?.thresholds || s.thresholds || {};
  const dw = num(pick(t, 'diskWarnPct'));
  const bm = num(pick(t, 'backupMaxAgeH'));
  if (dw > 0 && dw <= 100) thresholds.diskWarnPct = dw;
  if (bm > 0) thresholds.backupMaxAgeH = bm;

  const host = s.host || {};
  const g = s.gate || {};
  const status = gateStatus(s);
  const docker = dockerState(s);
  const apps = deriveApps(s, pending, now);
  const held = apps.filter((a) => a.statusKey === 'held').length;
  const groups = historyGroups(g);
  const current = groups.find((x) => x.current) || null;
  const since = toDate(pick(g, 'since'));
  const lastPassAt = toDate(pick(g, 'lastPassAt')) || toDate(current?.rows.find((r) => r.event === 'pass' || r.event === 'retry-pass')?.ts);
  const powercycled = pick(g, 'powercycled') ?? Boolean(current?.rows.some((r) => r.event === 'powercycle'));
  const retryMinutes = num(pick(g, 'retryMinutes')) || 5;
  const mountPath = pick(g, 'mountPath') || s.drive?.mountPath || '/media';
  let canRerun = pick(g, 'canRerun');
  let rerunLock = pick(g, 'rerunLock') ?? null;
  if (canRerun === undefined) {
    canRerun = ['failed', 'media-lost', 'waiting'].includes(status);
    rerunLock = canRerun ? null : 'The drive is verified and mounted. A re-run only acts when it has failed or been lost.';
  }
  const gate = {
    status,
    chip: GATE_CHIP[status],
    attempts: num(pick(g, 'attempts')),
    nextRetryAt: toDate(pick(g, 'nextRetryAt')),
    retryMinutes,
    since,
    lastPassAt,
    powercycled,
    mountPath,
    currentBoot: String(pick(g, 'currentBoot') ?? current?.boot ?? ''),
    canRerun: Boolean(canRerun),
    rerunLock,
    action: g.action && typeof g.action === 'object' ? g.action : null,
    line: typeof g.line === 'string' && g.line ? g.line : gateLine({ status, since, lastPassAt, held, retryMinutes, mountPath, powercycled }, now),
    groups,
    failuresByMonth: arr(s.uptime?.driveFailures),
  };

  const d = s.drive || {};
  const total = num(pick(d, 'total', 'totalBytes'));
  const used = num(pick(d, 'used', 'usedBytes'));
  const free = num(pick(d, 'free', 'freeBytes'));
  const pctGiven = num(pick(d, 'usedPct'));
  const pct = Number.isFinite(pctGiven) ? pctGiven : total > 0 && Number.isFinite(used) ? (used / total) * 100 : null;
  const driveNorm = { ...d, total, used, samples: arr(d.samples) };
  const forecast = driveForecast(driveNorm, now);
  const split = d.split && typeof d.split === 'object' ? d.split : null;
  const drive = {
    mounted: pick(d, 'mounted') ?? status === 'ok',
    stale: Boolean(pick(d, 'stale')),
    lastKnownAt: toDate(pick(d, 'lastKnownAt')),
    total,
    used,
    free: Number.isFinite(free) ? free : total - used,
    pct,
    warn: pick(d, 'warn') ?? (pct !== null && pct >= thresholds.diskWarnPct),
    samples: driveNorm.samples,
    forecast,
    forecastReason: pick(d, 'forecastReason') ?? null,
    split: split
      ? {
          tv: num(pick(split, 'tv', 'tvBytes')),
          movies: num(pick(split, 'movies', 'moviesBytes')),
          downloads: num(pick(split, 'downloads', 'downloadsBytes')),
          at: toDate(pick(split, 'computedAt', 'at')),
          stale: Boolean(split.stale),
        }
      : null,
  };
  drive.chart = forecastChart(driveNorm, forecast, { now, warnPct: thresholds.diskWarnPct });

  const m = s.media || {};
  const byName = Object.fromEntries(apps.map((a) => [a.name, a]));
  const tiles = {};
  for (const name of ['jellyfin', 'sabnzbd', 'jellyseerr', 'prowlarr', 'sonarr', 'radarr']) tiles[name] = mediaTile(m[name], byName[name]);
  const media = {
    tiles,
    checks: arr(m.checks).map((c) => ({
      key: String(c.key ?? ''),
      label: String(pick(c, 'label') ?? ''),
      status: ['ok', 'warn', 'fail', 'idle'].includes(c.status) ? c.status : 'idle',
      text: String(pick(c, 'detail', 'text') ?? (c.status === 'ok' ? 'OK' : '')),
    })),
  };

  const backups = backupView(s.backups, thresholds, now);
  const renewalList = renewals(s.subscriptions, now);
  const alerts = alertStates(s.push?.alerts, now);
  const devices = arr(s.push?.devices).map((x) => ({ id: String(x.id ?? ''), label: String(x.label ?? 'device'), created: x.created }));
  const bootedAt = toDate(pick(host, 'bootedAt'));
  const uptimeGiven = num(pick(host, 'uptimeSeconds'));

  const view = {
    schema: s.schema ?? null,
    version: pick(s.server, 'version') ?? pick(s, 'version') ?? '2.0.0',
    hostName: pick(s.server, 'name') ?? pick(host, 'name') ?? 'homelab',
    generatedAt: toDate(pick(s, 'generatedAt')),
    access: s.access === 'remote' ? 'remote' : 'tailnet',
    rehearsal: Boolean(s.rehearsal),
    docker,
    helperReachable: s.helper ? s.helper.reachable !== false : true,
    storeError: Boolean(s.push?.storeError),
    thresholds,
    gate,
    apps,
    host: {
      load: pick(host, 'load'),
      memUsed: num(pick(host, 'memUsed', 'memUsedBytes')),
      memTotal: num(pick(host, 'memTotal', 'memTotalBytes')),
      rootPct: num(pick(host, 'rootUsedPct')),
      uptimeSeconds: bootedAt ? Math.max(0, (now - bootedAt.getTime()) / 1000) : uptimeGiven,
      rebootRequired: Boolean(pick(host, 'rebootRequired')),
      rebootPackages: arr(pick(host, 'rebootPackages')),
    },
    drive,
    media,
    backups,
    renewalList,
    alerts,
    devices,
    uptime: s.uptime || {},
    // External links only (http/https), from the server's config.
    runbooks: arr(s.links?.runbooks ?? s.runbooks).filter((r) => r && typeof r.title === 'string' && isWebUrl(r.url)),
    now,
  };
  view.issues = Array.isArray(s.issues) ? serverIssues(s.issues) : deriveIssues(view);
  view.dots = navDots(view);
  return view;
}

export function navDots(view) {
  const g = view.gate.status;
  const drive = g === 'failed' || g === 'media-lost' ? 'fail' : g === 'waiting' || g === 'unknown' || view.drive.warn ? 'warn' : null;
  const mediaIssue = view.issues.some((i) => i.to?.name === 'media');
  const alerts = view.alerts.some((a) => a.state === 'active') ? 'fail' : null;
  const apps = view.apps.some((a) => a.statusKey === 'down') ? 'fail' : view.apps.some((a) => a.statusKey === 'unhealthy') ? 'warn' : null;
  return { drive, media: mediaIssue ? 'warn' : null, alerts, apps, more: drive };
}

// Overview banner.
export function healthBanner(view) {
  const { issues, apps, drive, backups } = view;
  if (issues.length) {
    const fail = issues.some((i) => i.level === 'fail');
    return { tone: fail ? 'fail' : 'warn', icon: fail ? 'warning-octagon' : 'warning', title: plural(issues.length, 'issue'), sub: 'Tap one to see the cause', issues };
  }
  const healthy = apps.filter((a) => a.statusKey === 'healthy').length;
  const stopped = apps.filter((a) => a.stopKind === 'intentional').length;
  const parts = [`${plural(healthy, 'app')} healthy`];
  if (stopped) parts.push(`${stopped} stopped on purpose`);
  if (drive.pct === null) parts.push('drive unknown');
  else parts.push(drive.pct < 1 ? 'drive empty' : `drive ${Math.round(drive.pct)}%`);
  if (backups.status === 'none') parts.push('first backup tonight');
  else if (backups.lastOk) {
    const h = Math.floor((view.now - backups.lastOk.getTime()) / 3600000);
    parts.push(`backup ${h < 1 ? 'under 1 h' : h < 48 ? `${h} h` : `${Math.floor(h / 24)} d`} ago`);
  }
  return { tone: 'ok', icon: 'check', title: 'All systems normal', sub: parts.join(' · '), issues: [] };
}

export function appsSummary(apps, docker = 'active') {
  const n = apps.length;
  const healthy = apps.filter((a) => a.statusKey === 'healthy').length;
  const held = apps.filter((a) => a.statusKey === 'held').length;
  const stopped = apps.filter((a) => a.stopKind === 'intentional').length;
  const dockerDown = apps.some((a) => a.stopKind === 'docker-down');
  const down = apps.filter((a) => a.statusKey === 'down').length;
  let sub = 'All containers up';
  if (!n) sub = 'No containers reported';
  else if (held) sub = `${held} held by the gate`;
  else if (dockerDown || docker !== 'active') sub = "Docker isn't running";
  else if (down) sub = `${down} down`;
  else if (stopped) sub = `${stopped} stopped on purpose`;
  else if (healthy < n) sub = `${n - healthy} not healthy yet`;
  return { big: `${healthy} / ${n} healthy`, sub };
}

// Media-drive failures per month: the server's driveFailures, or (fallback, the server's rule) a boot that
// logged a fail plus every media-lost, from the history.
export function failuresByMonth(gate, months) {
  const counts = Object.fromEntries(months.map((k) => [k, 0]));
  if (gate.failuresByMonth?.length) {
    for (const f of gate.failuresByMonth) if (counts[f.month] !== undefined) counts[f.month] = num(f.count) || 0;
    return counts;
  }
  for (const g of gate.groups) {
    let failed = false;
    for (const r of [...g.rows].reverse()) {
      const k = dayKey(r.ts).slice(0, 7);
      if (counts[k] === undefined) continue;
      if (r.event === 'media-lost') counts[k] += 1;
      else if (r.event === 'fail' && !failed) {
        failed = true;
        counts[k] += 1;
      }
    }
  }
  return counts;
}

// The last n month keys ("2026-10"), oldest first, in the display zone.
export function lastMonths(n, now = Date.now()) {
  const out = [];
  for (let i = n - 1; i >= 0; i -= 1) out.push(monthKeyOffset(now, -i));
  return out;
}

// The last n day keys, oldest first, in the display zone.
export function lastDays(n, now = Date.now()) {
  const out = [];
  for (let i = n - 1; i >= 0; i -= 1) out.push(dayKeyOffset(now, -i));
  return out;
}

export function dayTone(pct) {
  if (!Number.isFinite(pct)) return 'none';
  if (pct >= 99.99) return 'ok';
  if (pct >= 50) return 'warn';
  return 'fail';
}

// Offline snapshot rows.
export function offlineRows(view) {
  const apps = view.apps;
  const healthy = apps.filter((a) => a.statusKey === 'healthy').length;
  const held = apps.filter((a) => a.statusKey === 'held').length;
  const running = apps.filter((a) => a.state === 'running').length;
  const appsTone = held ? 'idle' : healthy === apps.length ? 'ok' : apps.some((a) => a.statusKey === 'down') ? 'fail' : 'warn';
  const b = view.backups;
  return [
    { k: 'Boot gate', v: view.gate.status, tone: view.gate.chip.tone },
    { k: 'Apps', v: held ? `${running} running · ${held} held` : `${healthy} / ${apps.length} healthy`, tone: appsTone },
    { k: 'Last backup', v: b.lastOk ? fmtDayTime(b.lastOk) : 'none yet', tone: b.status === 'ok' ? 'ok' : b.status === 'none' ? 'idle' : 'warn' },
  ];
}

// Outcome of an action we're waiting on, judged from the latest summary (only used when the server's
// action stream isn't available). 'wait' | 'healthy' | 'unhealthy' | 'failed' | 'stopped' | 'timeout'.
export function pendingOutcome(p, app, now = Date.now()) {
  const limits = { restart: 180000, start: 180000, stop: 60000, update: 300000 };
  const age = now - p.since;
  const late = age > (limits[p.kind] || 180000);
  if (!app) return late ? 'timeout' : 'wait';
  if (app.action) return late ? 'timeout' : 'wait'; // the server is still on it
  if (p.kind === 'stop') {
    if (app.state && app.state !== 'running') return 'stopped';
    return late ? 'timeout' : 'wait';
  }
  const restarted = app.startedAt && app.startedAt !== p.startedAtBefore;
  const updated = p.kind !== 'update' || !app.updateAvailable;
  if (restarted && updated && app.state === 'running') {
    if (app.health === 'unhealthy') return 'unhealthy';
    if (app.health !== 'starting') return 'healthy';
  }
  if (restarted && (app.state === 'exited' || app.state === 'dead')) return 'failed';
  return late ? 'timeout' : 'wait';
}

