// Boot gate (CONTRACTS §3, contracts 1 and 2): /var/lib/bootgate/{status,attempts,history.jsonl,media-stopped}
// plus the retry timer's next elapse. Read-only; the only write the dashboard ever makes for the gate is the
// re-run trigger (actions.js).
import path from 'node:path';
import { exists, readJsonlTail, readText } from '../util/fsx.js';
import { parseKeyValues } from '../util/run.js';
import { localMonth, monotonicNowUs, parseTimespanUs, shortDateTime, shortTime } from '../util/time.js';

export const GATE_STATUSES = ['ok', 'waiting', 'failed', 'media-lost'];
export const GATE_EVENTS = ['pass', 'repair', 'powercycle', 'fail', 'retry-pass', 'media-lost'];
const PASS_EVENTS = new Set(['pass', 'retry-pass']);
const PROBLEM_EVENTS = new Set(['fail', 'media-lost', 'powercycle', 'repair']);

export const normBootId = (id) => (typeof id === 'string' ? id.replace(/-/g, '').toLowerCase() : '');

function readStatus(dir) {
  const raw = readText(path.join(dir, 'status'));
  if (raw === null) return 'unknown';
  const s = raw.trim();
  return GATE_STATUSES.includes(s) ? s : 'unknown';
}

function readAttempts(dir) {
  const raw = readText(path.join(dir, 'attempts'));
  const n = Number.parseInt(raw ?? '', 10);
  return Number.isFinite(n) && n >= 0 ? n : 0;
}

function cleanEvent(e) {
  if (!GATE_EVENTS.includes(e.event) || typeof e.ts !== 'string' || Number.isNaN(Date.parse(e.ts))) return null;
  return {
    ts: new Date(e.ts).toISOString(),
    bootId: normBootId(e.boot_id),
    event: e.event,
    detail: typeof e.detail === 'string' ? e.detail : '',
    waitSeconds: Number.isFinite(e.wait_seconds) ? e.wait_seconds : null,
  };
}

// Events grouped by boot, newest boot first, events newest first.
export function groupHistory(events, currentBootId, maxGroups = 8) {
  const groups = [];
  const byBoot = new Map();
  for (const e of events) {
    let g = byBoot.get(e.bootId);
    if (!g) {
      g = { boot: e.bootId.slice(0, 4), current: Boolean(currentBootId) && e.bootId === currentBootId, startedAt: e.ts, events: [] };
      byBoot.set(e.bootId, g);
      groups.push(g);
    }
    if (e.ts < g.startedAt) g.startedAt = e.ts;
    g.events.push({ ts: e.ts, event: e.event, detail: e.detail, waitSeconds: e.waitSeconds });
  }
  for (const g of groups) g.events.sort((a, b) => (a.ts < b.ts ? 1 : -1));
  groups.sort((a, b) => (a.events[0].ts < b.events[0].ts ? 1 : -1));
  return groups.slice(0, maxGroups);
}

// Start of the current incident: the first problem event after the last pass.
export function incidentStart(events) {
  let start = null;
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (PASS_EVENTS.has(e.event)) break;
    if (PROBLEM_EVENTS.has(e.event)) start = e;
  }
  return start;
}

// Distinct incidents per month: a boot with a `fail`, or any `media-lost` event.
export function failuresByMonth(events, months, timeZone) {
  const counts = Object.fromEntries(months.map((m) => [m, 0]));
  const seenBoots = new Set();
  for (const e of events) {
    const m = localMonth(e.ts, timeZone);
    if (!(m in counts)) continue;
    if (e.event === 'media-lost') counts[m] += 1;
    else if (e.event === 'fail' && !seenBoots.has(e.bootId)) {
      seenBoots.add(e.bootId);
      counts[m] += 1;
    }
  }
  return months.map((month) => ({ month, count: counts[month] }));
}

// When bootgate-retry.timer fires next. Monotonic timers only expose NextElapseUSecMonotonic.
export async function nextRetry({ run, unit, now = Date.now() }) {
  if (!run) return null;
  const r = await run('systemctl', ['show', unit, '--no-pager', '--timestamp=unix', '-p', 'NextElapseUSecRealtime', '-p', 'NextElapseUSecMonotonic']);
  if (r.code !== 0) return null;
  const kv = parseKeyValues(r.stdout);
  const real = kv.NextElapseUSecRealtime;
  if (real && /^@\d+$/.test(real)) return new Date(Number(real.slice(1)) * 1000).toISOString();
  const mono = parseTimespanUs(kv.NextElapseUSecMonotonic);
  if (!mono) return null;
  const deltaMs = (mono - monotonicNowUs()) / 1000;
  if (deltaMs < -60_000) return null; // stale value
  return new Date(now + Math.max(0, deltaMs)).toISOString();
}

function lineFor({ status, since, lastPass, heldCount, retryMinutes, mountPath, powercycled }, timeZone) {
  const at = (iso) => (iso ? shortTime(iso, timeZone) : null);
  switch (status) {
    case 'ok':
      return lastPass
        ? `Drive found, checked and mounted at ${mountPath} (${shortDateTime(lastPass.ts, timeZone)}). Apps released.`
        : `Drive mounted at ${mountPath}. Apps released.`;
    case 'failed':
      return (
        `Media drive missing${since ? ` since ${at(since)}` : ''}${powercycled ? ' after a power-cycle' : ''}. ` +
        `${heldCount === 1 ? '1 app' : `${heldCount} apps`} held. Retrying every ${retryMinutes} min.`
      );
    case 'media-lost':
      return (
        `Media drive lost${since ? ` at ${at(since)}` : ''}. Media apps stopped (${heldCount} held). ` +
        `Retrying every ${retryMinutes} min.`
      );
    case 'waiting':
      return 'Waiting for the media drive. The boot check is still running.';
    default:
      return 'Boot gate status is unknown (no status file).';
  }
}

export function collectGate({ cfg, timeZone, now = Date.now(), currentBootId, nextRetryAt = null, heldCount = 0 }) {
  const dir = cfg.gateDir;
  const status = readStatus(dir);
  const attempts = readAttempts(dir);
  const mediaStopped = exists(path.join(dir, 'media-stopped'));
  const events = readJsonlTail(path.join(dir, 'history.jsonl'), { maxLines: 300 }).map(cleanEvent).filter(Boolean);
  events.sort((a, b) => (a.ts < b.ts ? -1 : 1));
  const start = status === 'ok' ? null : incidentStart(events);
  const lastPass = [...events].reverse().find((e) => PASS_EVENTS.has(e.event)) ?? null;
  const powercycled =
    status === 'failed' && start ? events.some((e) => e.event === 'powercycle' && e.ts >= start.ts) : false;
  const retrying = status === 'failed' || status === 'media-lost' || status === 'waiting';
  const canRerun = retrying || (status === 'ok' && mediaStopped);
  return {
    status,
    line: lineFor(
      { status, since: start?.ts, lastPass, heldCount, retryMinutes: cfg.gateRetryMinutes, mountPath: cfg.mediaMount, powercycled },
      timeZone,
    ),
    since: start?.ts ?? null,
    powercycled,
    attempts,
    mediaStopped,
    mountPath: cfg.mediaMount,
    currentBoot: currentBootId ? currentBootId.slice(0, 4) : null,
    lastPassAt: lastPass?.ts ?? null,
    retryMinutes: cfg.gateRetryMinutes,
    nextRetryAt: retrying ? nextRetryAt : null,
    canRerun,
    rerunLock: canRerun ? null : 'The drive is verified and mounted. A re-run only acts when it has failed or been lost.',
    history: groupHistory(events, currentBootId),
    _events: events, // internal (uptime, issues); stripped before sending
    _now: now,
  };
}

export function readGateStatus(cfg) {
  return { status: readStatus(cfg.gateDir), mediaStopped: exists(path.join(cfg.gateDir, 'media-stopped')) };
}

export function readBootId(cfg) {
  return normBootId(readText(path.join(cfg.procDir, 'sys/kernel/random/boot_id'))?.trim() ?? '');
}
