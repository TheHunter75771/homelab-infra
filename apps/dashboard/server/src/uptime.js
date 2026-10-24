// Uptime.
// - Per-app: the sampler records each app once a minute into uptime/YYYY-MM.json as
//   {"days": {"YYYY-MM-DD": {"<app>": [upMinutes, countedMinutes]}}}. Up = status healthy. Held by the gate,
//   down, unhealthy, starting and updating count as down. Stopped on purpose and unknown aren't counted.
// - Box: from the helper's `boots` (journal boot records). Gaps between boots are downtime.
import path from 'node:path';
import { ensureDir, readJson, writeJsonAtomic } from './util/fsx.js';
import { addDays, localDate } from './util/time.js';

export const UP_STATUSES = new Set(['healthy']);
export const NOT_COUNTED = new Set(['stopped', 'unknown']);

export function createUptimeStore({ stateDir, timeZone }) {
  const dir = path.join(stateDir, 'uptime');
  const cache = new Map(); // month -> doc

  function load(month) {
    if (!cache.has(month)) {
      const r = readJson(path.join(dir, `${month}.json`));
      const doc = r.value && typeof r.value.days === 'object' ? r.value : { version: 1, days: {} };
      cache.set(month, doc);
    }
    return cache.get(month);
  }

  return {
    // statuses: {app: status}
    record(at, statuses) {
      const date = localDate(at, timeZone);
      const month = date.slice(0, 7);
      const doc = load(month);
      const day = (doc.days[date] ??= {});
      let changed = false;
      for (const [app, status] of Object.entries(statuses)) {
        if (NOT_COUNTED.has(status)) continue;
        const cell = (day[app] ??= [0, 0]);
        if (UP_STATUSES.has(status)) cell[0] += 1;
        cell[1] += 1;
        changed = true;
      }
      if (changed) {
        ensureDir(dir, 0o750);
        writeJsonAtomic(path.join(dir, `${month}.json`), doc, { mode: 0o640 });
      }
      for (const m of cache.keys()) if (m < month) cache.delete(m); // keep memory small
      return changed;
    },
    // [{name, months: [{month, pct|null}]}]
    monthly(apps, months) {
      return apps.map((name) => ({
        name,
        months: months.map((month) => {
          let up = 0;
          let total = 0;
          for (const cells of Object.values(load(month).days)) {
            const c = cells[name];
            if (Array.isArray(c)) {
              up += c[0];
              total += c[1];
            }
          }
          return { month, pct: total ? Math.round((up / total) * 1000) / 10 : null };
        }),
      }));
    },
  };
}

function tzOffsetMs(epoch, timeZone) {
  const fmt = new Intl.DateTimeFormat('en-US', {
    timeZone,
    hourCycle: 'h23',
    year: 'numeric',
    month: 'numeric',
    day: 'numeric',
    hour: 'numeric',
    minute: 'numeric',
    second: 'numeric',
  });
  const p = {};
  for (const x of fmt.formatToParts(new Date(epoch))) p[x.type] = Number(x.value);
  return Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute, p.second) - Math.floor(epoch / 1000) * 1000;
}

// Epoch ms of local midnight for a YYYY-MM-DD date.
export function zonedMidnight(date, timeZone) {
  const [y, m, d] = date.split('-').map(Number);
  const guess = Date.UTC(y, m - 1, d);
  const first = guess - tzOffsetMs(guess, timeZone);
  return guess - tzOffsetMs(first, timeZone);
}

const overlap = (a0, a1, b0, b1) => Math.max(0, Math.min(a1, b1) - Math.max(a0, b0));

// boots: [{boot_id, first, last}] (any order). The newest boot is the current one and runs until `now`.
// gateEvents: to label gaps caused by a gate power-cycle.
export function computeBoxUptime(boots, { now = Date.now(), timeZone, days = 30, gateEvents = [] } = {}) {
  const list = (Array.isArray(boots) ? boots : [])
    .map((b) => ({ first: Date.parse(b.first), last: Date.parse(b.last) }))
    .filter((b) => Number.isFinite(b.first) && Number.isFinite(b.last) && b.last >= b.first)
    .sort((a, b) => a.first - b.first);
  const today = localDate(now, timeZone);
  const dates = Array.from({ length: days }, (_, i) => addDays(today, i - days + 1));
  if (!list.length) return { source: 'boots', pct30d: null, days: dates.map((date) => ({ date, pct: null })), downs: [] };
  list.at(-1).last = Math.max(list.at(-1).last, now);
  const known0 = list[0].first;
  const ups = list.map((b) => [b.first, b.last]);
  const gaps = [];
  for (let i = 1; i < list.length; i++) {
    if (list[i].first - list[i - 1].last >= 60_000) gaps.push([list[i - 1].last, list[i].first]);
  }
  const windowStart = zonedMidnight(dates[0], timeZone);
  let upTotal = 0;
  let knownTotal = 0;
  const dayRows = dates.map((date) => {
    const d0 = zonedMidnight(date, timeZone);
    const d1 = Math.min(zonedMidnight(addDays(date, 1), timeZone), now);
    const k0 = Math.max(d0, known0);
    if (d1 <= k0) return { date, pct: null };
    const known = d1 - k0;
    const up = ups.reduce((acc, [a, b]) => acc + overlap(a, b, k0, d1), 0);
    upTotal += up;
    knownTotal += known;
    return { date, pct: Math.round((Math.min(up, known) / known) * 10000) / 100 };
  });
  const powercycles = gateEvents.filter((e) => e.event === 'powercycle').map((e) => Date.parse(e.ts));
  const downs = gaps
    .filter(([, end]) => end > windowStart)
    .map(([start, end]) => ({
      start: new Date(start).toISOString(),
      end: new Date(end).toISOString(),
      seconds: Math.round((end - start) / 1000),
      reason: powercycles.some((t) => t >= start - 10 * 60_000 && t <= end) ? 'Power-cycle by the boot gate' : 'Reboot or power loss',
    }))
    .reverse();
  return {
    source: 'boots',
    pct30d: knownTotal ? Math.round((Math.min(upTotal, knownTotal) / knownTotal) * 10000) / 100 : null,
    days: dayRows,
    downs,
  };
}
