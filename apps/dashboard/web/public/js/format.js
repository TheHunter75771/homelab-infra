// Formatters: English month names, 24 h clock. Times show in the host's time zone when the summary names
// it (server.timeZone), so they agree with the server's preformatted lines (gate.line, tile details);
// otherwise in the browser's own zone.

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const DAY = 86400;

let zone = null; // IANA zone name, or null for the browser's zone
const zoneFormats = new Map();

export function setTimeZone(tz) {
  zone = null;
  if (typeof tz !== 'string' || !tz) return;
  try {
    if (!zoneFormats.has(tz)) {
      zoneFormats.set(
        tz,
        new Intl.DateTimeFormat('en-US', { timeZone: tz, hourCycle: 'h23', year: 'numeric', month: 'numeric', day: 'numeric', hour: 'numeric', minute: 'numeric', second: 'numeric' }),
      );
    }
    zone = tz;
  } catch {
    zone = null; // unknown zone name: keep the browser's
  }
}

export const timeZone = () => zone;

// Calendar fields of an instant in the display zone: {y, mo (0-11), d, h, mi, s}.
export function zoned(date) {
  if (!zone) return { y: date.getFullYear(), mo: date.getMonth(), d: date.getDate(), h: date.getHours(), mi: date.getMinutes(), s: date.getSeconds() };
  const p = {};
  for (const x of zoneFormats.get(zone).formatToParts(date)) p[x.type] = Number(x.value);
  return { y: p.year, mo: p.month - 1, d: p.day, h: p.hour % 24, mi: p.minute, s: p.second };
}

const pad2 = (n) => String(n).padStart(2, '0');

// A real number (Number(null) is 0, which must not read as "zero bytes" or "due now").
export const isNum = (v) => v !== null && v !== undefined && v !== '' && typeof v !== 'boolean' && Number.isFinite(Number(v));

export function toDate(value) {
  if (value == null || value === '') return null;
  const d = value instanceof Date ? value : new Date(typeof value === 'number' ? value : String(value));
  return Number.isNaN(d.getTime()) ? null : d;
}

// --- sizes ----------------------------------------------------------------------------------

const UNITS = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];

// Decimal units, three significant figures: 2.64 TB, 18.4 GB, 812 GB, 4.0 TB, 16 GB.
export function fmtBytes(bytes, { zeroUnit = 'B' } = {}) {
  if (!isNum(bytes)) return '—';
  const n = Number(bytes);
  if (n < 0) return '—';
  if (n === 0) return `0 ${zeroUnit}`;
  let i = 0;
  let v = n;
  while (v >= 999.5 && i < UNITS.length - 1) {
    v /= 1000;
    i += 1;
  }
  if (i === 0) return `${Math.round(v)} B`;
  let s;
  if (v < 9.995) s = v.toFixed(2).replace(/0$/, ''); // 4.00 -> 4.0, 2.64 stays
  else if (v < 99.95) s = v.toFixed(1).replace(/\.0$/, ''); // 16.0 -> 16, 18.4 stays
  else s = String(Math.round(v));
  return `${s} ${UNITS[i]}`;
}

export function fmtRate(bytesPerSecond) {
  const n = Number(bytesPerSecond);
  if (!isNum(bytesPerSecond) || n <= 0) return '0 B/s';
  return `${fmtBytes(n)}/s`;
}

// RAM uses binary units (what the kernel reports), labelled GB like every phone does.
export function fmtMem(usedBytes, totalBytes) {
  const g = (b) => (Number(b) / 1024 ** 3).toFixed(1);
  if (!isNum(usedBytes) || !isNum(totalBytes) || !Number(totalBytes)) return '—';
  return `${g(usedBytes)} / ${g(totalBytes)} GB`;
}

export function fmtLoad(load) {
  if (!Array.isArray(load) || !load.length) return '—';
  return load
    .slice(0, 3)
    .map((v) => (isNum(v) ? Number(v).toFixed(2) : '—'))
    .join(' ');
}

export function fmtPct(value, digits = 0) {
  if (!isNum(value)) return '—';
  return `${Number(value).toFixed(digits)}%`;
}

// Uptime percentages: 100% stays "100%", otherwise two decimals (99.95%).
export function fmtUptimePct(value) {
  if (!isNum(value)) return '—';
  const n = Number(value);
  if (n >= 99.995) return '100%';
  return `${n.toFixed(2)}%`;
}

// --- durations ------------------------------------------------------------------------------

// Compact uptime: 3d 8h, 4h 12m, 12m, 45s.
export function fmtUptime(seconds) {
  const s = Math.floor(Number(seconds));
  if (!isNum(seconds) || s < 0) return '—';
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ${m % 60}m`;
  return `${Math.floor(h / 24)}d ${h % 24}h`;
}

// Spaced spans for outage lists: 45 s, 4 m, 2 h 5 m, 3 d 4 h.
export function fmtSpan(seconds) {
  const s = Math.round(Number(seconds));
  if (!isNum(seconds) || s < 0) return '—';
  if (s < 60) return `${s} s`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m} m`;
  const h = Math.floor(m / 60);
  if (h < 24) return m % 60 ? `${h} h ${m % 60} m` : `${h} h`;
  const d = Math.floor(h / 24);
  return h % 24 ? `${d} d ${h % 24} h` : `${d} d`;
}

// Precise spans for drills: 14 m 02 s.
export function fmtSpanPrecise(seconds) {
  const s = Math.round(Number(seconds));
  if (!isNum(seconds) || s < 0) return '—';
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} m ${pad2(s % 60)} s`;
  return `${Math.floor(m / 60)} h ${pad2(m % 60)} m`;
}

// Countdown clock: 3:42, 0:05, 1:02:03. Never negative.
export function fmtCountdown(seconds) {
  const s = Math.max(0, Math.ceil(Number(seconds) || 0));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const r = s % 60;
  return h ? `${h}:${pad2(m)}:${pad2(r)}` : `${m}:${pad2(r)}`;
}

// Playback position: 1:13:20 / 1:56:00 (m:ss under an hour).
export function fmtPlayTime(seconds) {
  const s = Math.max(0, Math.floor(Number(seconds) || 0));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const r = s % 60;
  return h ? `${h}:${pad2(m)}:${pad2(r)}` : `${m}:${pad2(r)}`;
}

export function fmtPlayback(positionSeconds, durationSeconds) {
  return `${fmtPlayTime(positionSeconds)} / ${fmtPlayTime(durationSeconds)}`;
}

export function fmtEta(seconds) {
  const s = Number(seconds);
  if (!isNum(seconds) || s < 0) return 'queued';
  if (s < 60) return 'under 1 min';
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60);
  return m % 60 ? `${h} h ${m % 60} min` : `${h} h`;
}

// "~63 weeks" until a date. Short horizons in days, long ones in years.
export function fmtHorizon(days) {
  const d = Number(days);
  if (!isNum(days)) return '—';
  if (d < 1) return 'under a day';
  if (d < 14) return `~${Math.round(d)} day${Math.round(d) === 1 ? '' : 's'}`;
  if (d < 365 * 3) return `~${Math.round(d / 7)} weeks`;
  return `~${Math.round(d / 365)} years`;
}

// Whole days until a date: "23 days", "1 day", "today", "expired".
export function fmtDaysLeft(days) {
  if (!Number.isFinite(days)) return '—';
  if (days < 0) return 'expired';
  if (days === 0) return 'today';
  return `${days} day${days === 1 ? '' : 's'}`;
}

// Calendar-day difference between two instants in the display zone (ignores the time of day).
export function daysBetween(from, to) {
  const a = toDate(from);
  const b = toDate(to);
  if (!a || !b) return NaN;
  const za = zoned(a);
  const zb = zoned(b);
  return Math.round((Date.UTC(zb.y, zb.mo, zb.d) - Date.UTC(za.y, za.mo, za.d)) / (DAY * 1000));
}

// The instant at noon of a calendar day in the display zone. Formatting it in the same zone always gives
// that day back, from UTC-12 to UTC+14 (noon UTC would not: it's already the next day at UTC+12).
export function zonedNoon(y, mo, d) {
  if (!zone) return new Date(y, mo, d, 12);
  const guess = Date.UTC(y, mo, d, 12);
  const z = zoned(new Date(guess));
  const offset = Date.UTC(z.y, z.mo, z.d, z.h, z.mi, z.s) - guess;
  return new Date(guess - offset);
}

// A calendar day (YYYY-MM-DD, the host's) as zonedNoon of that day.
export function parseDay(iso) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(iso || ''));
  if (!m) return null;
  const [y, mo, d] = [Number(m[1]), Number(m[2]) - 1, Number(m[3])];
  const check = new Date(Date.UTC(y, mo, d));
  if (check.getUTCFullYear() !== y || check.getUTCMonth() !== mo || check.getUTCDate() !== d) return null;
  return zonedNoon(y, mo, d);
}

export function dayKey(value) {
  const d = toDate(value);
  if (!d) return '';
  const z = zoned(d);
  return `${z.y}-${pad2(z.mo + 1)}-${pad2(z.d)}`;
}

// Keys of the day / month `offset` steps from the one containing `value` ("2026-10-24", "2026-10").
export function dayKeyOffset(value, offset) {
  const z = zoned(toDate(value));
  const t = new Date(Date.UTC(z.y, z.mo, z.d + offset));
  return `${t.getUTCFullYear()}-${pad2(t.getUTCMonth() + 1)}-${pad2(t.getUTCDate())}`;
}

export function monthKeyOffset(value, offset) {
  const z = zoned(toDate(value));
  const t = new Date(Date.UTC(z.y, z.mo + offset, 1));
  return `${t.getUTCFullYear()}-${pad2(t.getUTCMonth() + 1)}`;
}

// --- relative time --------------------------------------------------------------------------

// "12 s ago", "5 min ago", "3 h ago", "2 d ago".
export function fmtAgo(value, now = Date.now()) {
  const d = toDate(value);
  if (!d) return '—';
  const s = Math.max(0, Math.floor((toDate(now).getTime() - d.getTime()) / 1000));
  if (s < 60) return `${s} s ago`;
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < DAY) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / DAY)} d ago`;
}

// Short "when" for lists: "2 h ago", "yesterday", "Oct 12".
export function fmtWhen(value, now = Date.now()) {
  const d = toDate(value);
  if (!d) return '—';
  const diff = daysBetween(d, now);
  if (diff <= 0) return fmtAgo(d, now);
  if (diff === 1) return 'yesterday';
  return fmtDay(d);
}

// --- absolute time --------------------------------------------------------------------------

export function fmtClock(value) {
  const d = toDate(value);
  if (!d) return '—';
  const z = zoned(d);
  return `${pad2(z.h)}:${pad2(z.mi)}`;
}

export function fmtClockSec(value) {
  const d = toDate(value);
  if (!d) return '—';
  const z = zoned(d);
  return `${pad2(z.h)}:${pad2(z.mi)}:${pad2(z.s)}`;
}

export function fmtDay(value) {
  const d = toDate(value);
  if (!d) return '—';
  const z = zoned(d);
  return `${MONTHS[z.mo]} ${z.d}`;
}

export function fmtDayTime(value) {
  const d = toDate(value);
  return d ? `${fmtDay(d)} ${fmtClock(d)}` : '—';
}

// "Nov 16, 2026"
export function fmtDate(value) {
  const d = toDate(value);
  return d ? `${fmtDay(d)}, ${zoned(d).y}` : '—';
}

// "Sep 30 2026" (device list)
export function fmtDayYear(value) {
  const d = toDate(value);
  return d ? `${fmtDay(d)} ${zoned(d).y}` : '—';
}

// "Jan 6" this year, "Jan '28" in another year (chart labels).
export function fmtDayShort(value, now = Date.now()) {
  const d = toDate(value);
  if (!d) return '—';
  const z = zoned(d);
  return z.y === zoned(toDate(now)).y ? fmtDay(d) : `${MONTHS[z.mo]} '${String(z.y).slice(-2)}`;
}

// "03:12 today", "yesterday 21:40", "Oct 23 03:10".
export function fmtStamp(value, now = Date.now()) {
  const d = toDate(value);
  if (!d) return '—';
  const diff = daysBetween(d, now);
  if (diff === 0) return `${fmtClock(d)} today`;
  if (diff === 1) return `yesterday ${fmtClock(d)}`;
  return fmtDayTime(d);
}

// "today 03:12" style for the offline snapshot rows.
export function fmtStampLead(value, now = Date.now()) {
  const d = toDate(value);
  if (!d) return '—';
  const diff = daysBetween(d, now);
  if (diff === 0) return `today ${fmtClock(d)}`;
  if (diff === 1) return `yesterday ${fmtClock(d)}`;
  return fmtDayTime(d);
}

export function monthLabel(key) {
  const m = /^(\d{4})-(\d{2})/.exec(String(key || ''));
  return m ? MONTHS[Number(m[2]) - 1] || key : String(key || '');
}

// Image ref without registry and digest: "lscr.io/linuxserver/radarr:6.1@sha256:.." -> "linuxserver/radarr:6.1".
export function shortImage(ref) {
  if (typeof ref !== 'string' || !ref) return '—';
  let s = ref.split('@')[0];
  const first = s.split('/')[0];
  if (s.includes('/') && (first.includes('.') || first.includes(':') || first === 'localhost')) {
    s = s.slice(first.length + 1);
  }
  if (s.startsWith('library/')) s = s.slice(8);
  return s;
}

// The tag of an image ref ("6.1.1.10360-ls303"), or "" when it has none.
export function imageTag(ref) {
  const s = shortImage(ref);
  if (s === '—') return '';
  const slash = s.lastIndexOf('/');
  const colon = s.lastIndexOf(':');
  return colon > slash ? s.slice(colon + 1) : '';
}

export function imageDigest(ref) {
  const m = /@(sha256:[0-9a-f]{64})/.exec(String(ref || ''));
  return m ? m[1] : '';
}

export const plural = (n, word, many = `${word}s`) => `${n} ${n === 1 ? word : many}`;
