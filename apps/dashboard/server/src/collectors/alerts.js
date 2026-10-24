// Recent alerts from /var/lib/homelab-notify/history.jsonl (module B: {ts, title, body, tag, delivered, failed};
// never endpoints). Each alert gets a state:
//   resolved: it is a recovery (isRecovery below), or a later recovery with the same tag followed it
//             ("Media drive lost" -> "Media drive back")
//   info:     tag null/"test"/"reminder", or an unresolved alert older than 7 days
//   active:   everything else
// A "recovery" with a failure qualifier ("Media drive back, Docker did not start") is NOT a recovery: it stays
// active and resolves nothing. Conservative on purpose: a false "resolved" hides a live incident on a bad night,
// a false "active" only shows one extra row.
import { readJsonlTail } from '../util/fsx.js';

// Guard 1: the title must END with a recovery phrase ("Media drive back", "jellyfin recovered",
// "mediaserver back online", "... healthy again", "... ok again", "... resolved").
const RECOVERY_END_RE = /\b(?:back|recovered|online|resolved|healthy again|ok again|up again)[\s.!]*$/i;
// Guard 2: no failure qualifier anywhere in the title.
const QUALIFIER_RE =
  /\b(?:not|no|never|unable|cannot|fail(?:s|ed|ing|ure)?|errors?|still|but|however|except|partial(?:ly)?|degraded|without|pending)\b|\w+n['\u2019]t\b/i;

export function isRecovery(title) {
  // The notifier's "[REHEARSAL] " prefix holds no qualifier and sits at the start, so it changes nothing here.
  const t = String(title ?? '').trim();
  return RECOVERY_END_RE.test(t) && !QUALIFIER_RE.test(t);
}
const INFO_TAGS = new Set(['test', 'reminder']);
const ACTIVE_FOR_MS = 7 * 86_400_000;

export function classifyAlerts(raw, now = Date.now()) {
  const alerts = raw
    .filter((a) => typeof a.ts === 'string' && !Number.isNaN(Date.parse(a.ts)) && typeof a.title === 'string')
    .map((a) => ({
      ts: new Date(a.ts).toISOString(),
      title: a.title,
      body: typeof a.body === 'string' ? a.body : '',
      tag: typeof a.tag === 'string' && a.tag ? a.tag : null,
      delivered: Number.isInteger(a.delivered) ? a.delivered : null,
      failed: Number.isInteger(a.failed) ? a.failed : null,
      state: null,
    }))
    .sort((a, b) => (a.ts < b.ts ? -1 : 1));
  const openByTag = new Map();
  for (const a of alerts) {
    if (!a.tag || INFO_TAGS.has(a.tag)) {
      a.state = 'info';
      continue;
    }
    if (isRecovery(a.title)) {
      a.state = 'resolved';
      for (const prev of openByTag.get(a.tag) ?? []) prev.state = 'resolved';
      openByTag.set(a.tag, []);
      continue;
    }
    a.state = 'active';
    if (!openByTag.has(a.tag)) openByTag.set(a.tag, []);
    openByTag.get(a.tag).push(a);
  }
  for (const a of alerts) if (a.state === 'active' && now - Date.parse(a.ts) > ACTIVE_FOR_MS) a.state = 'info';
  return alerts.reverse();
}

export function collectAlerts({ cfg, now = Date.now(), limit = 50 }) {
  let raw = [];
  let available = true;
  try {
    raw = readJsonlTail(cfg.notifyHistoryFile, { maxLines: 300 });
  } catch {
    available = false;
  }
  return { available, alerts: classifyAlerts(raw, now).slice(0, limit) };
}
