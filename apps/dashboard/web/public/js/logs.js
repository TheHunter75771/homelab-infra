// Log lines from GET /api/apps/:name/logs (SSE). The server sends `lines` events whose data is a JSON array
// of {ts, level: INF|DBG|WRN|ERR, msg}, then one `end` event {reason: ended|max-duration|error, message}.
// Anything else (a bare `message` event with one JSON object or raw text) is read leniently, and the level
// is taken from the text when the server didn't give one.

import { parseJSON } from './api.js';
import { fmtClockSec, toDate } from './format.js';

export const MAX_LINES = 2000;
const LEVEL_NAMES = new Set(['INF', 'DBG', 'WRN', 'ERR']);

const LEVELS = [
  ['ERR', /\b(?:ERROR|ERR|FATAL|FTL|CRIT(?:ICAL)?|PANIC)\b|\|Error\||\[Error\]|\[Fatal\]|::ERROR::|level=(?:error|fatal|panic)/i],
  ['WRN', /\b(?:WARN(?:ING)?|WRN)\b|\|Warn\||\[Warn\]|::WARNING::|level=warn/i],
  ['DBG', /\b(?:DEBUG|DBG|TRACE|TRC|VERBOSE|VRB)\b|\|Debug\||\[Debug\]|\[Trace\]|::DEBUG::|level=(?:debug|trace)/i],
];

// Level from the first 80 characters, where every app here puts it; INF when none is found.
export function levelOf(text) {
  const head = String(text).slice(0, 80);
  for (const [lv, re] of LEVELS) if (re.test(head)) return lv;
  return 'INF';
}

const LEADING_TS = /^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?)\s+/;

let seq = 0;

function toLine(item) {
  let ts = null;
  let text = '';
  let level = null;
  if (item && typeof item === 'object') {
    ts = item.ts ?? item.time ?? null;
    text = item.msg ?? item.line ?? item.message ?? '';
    level = LEVEL_NAMES.has(item.level) ? item.level : null;
  } else text = item;
  text = String(text ?? '').replace(/\r?\n$/, '');
  if (!ts) {
    const m = LEADING_TS.exec(text);
    if (m) {
      ts = m[1].replace(',', '.');
      text = text.slice(m[0].length);
    }
  }
  if (!text.trim() && !ts) return null;
  const d = toDate(ts);
  seq += 1;
  return { key: seq, t: d ? fmtClockSec(d) : '', lv: level || levelOf(text), m: text };
}

// One SSE event -> { lines: [...], end: null | {reason, message} }.
export function parseLogEvent(ev) {
  const out = { lines: [], end: null };
  if (!ev || ev.type === 'comment' || ev.type === 'ping') return out;
  const data = typeof ev.data === 'string' && /^[[{]/.test(ev.data) ? parseJSON(ev.data) : null;
  if (ev.type === 'end') {
    const reason = ['error', 'max-duration'].includes(data?.reason) ? data.reason : 'ended';
    out.end = { reason, message: data?.message ? String(data.message) : null };
    return out;
  }
  const items = Array.isArray(data) ? data : [data ?? ev.data];
  for (const item of items) {
    const l = toLine(item);
    if (l) out.lines.push(l);
  }
  return out;
}

export function filterLines(lines, q) {
  const needle = String(q || '').trim().toLowerCase();
  if (!needle) return lines;
  return lines.filter((l) => `${l.t} ${l.lv} ${l.m}`.toLowerCase().includes(needle));
}

export const lineText = (l) => `${l.t ? `${l.t} ` : ''}${l.lv} ${l.m}`;
