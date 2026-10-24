// Time helpers. Calendar dates ("YYYY-MM-DD") are local to the host time zone (config `timezone`, default
// the process zone), because renewal dates, daily samples and uptime days are what the owner sees on the box.

const DAY_MS = 86_400_000;

function partsFor(date, timeZone) {
  const fmt = new Intl.DateTimeFormat('en-CA', {
    timeZone,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
  });
  const out = {};
  for (const p of fmt.formatToParts(date)) out[p.type] = p.value;
  return out;
}

export function localDate(date, timeZone) {
  const p = partsFor(date instanceof Date ? date : new Date(date), timeZone);
  return `${p.year}-${p.month}-${p.day}`;
}

export function localMonth(date, timeZone) {
  return localDate(date, timeZone).slice(0, 7);
}

// "14:08"
export function shortTime(date, timeZone) {
  const p = partsFor(date instanceof Date ? date : new Date(date), timeZone);
  return `${p.hour}:${p.minute}`;
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

// "Oct 21 06:01"
export function shortDateTime(date, timeZone) {
  const p = partsFor(date instanceof Date ? date : new Date(date), timeZone);
  return `${MONTHS[Number(p.month) - 1]} ${Number(p.day)} ${p.hour}:${p.minute}`;
}

// "Nov 16, 2026" for a YYYY-MM-DD string
export function longDate(isoDate) {
  const [y, m, d] = isoDate.split('-').map(Number);
  return `${MONTHS[m - 1]} ${d}, ${y}`;
}

export function isIsoDate(value) {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const [y, m, d] = value.split('-').map(Number);
  const t = new Date(Date.UTC(y, m - 1, d));
  return t.getUTCFullYear() === y && t.getUTCMonth() === m - 1 && t.getUTCDate() === d;
}

// Whole days from date a to date b (both YYYY-MM-DD). Calendar arithmetic, so DST never skews it.
export function daysBetween(a, b) {
  const [ay, am, ad] = a.split('-').map(Number);
  const [by, bm, bd] = b.split('-').map(Number);
  return Math.round((Date.UTC(by, bm - 1, bd) - Date.UTC(ay, am - 1, ad)) / DAY_MS);
}

export function addDays(isoDate, n) {
  const [y, m, d] = isoDate.split('-').map(Number);
  const t = new Date(Date.UTC(y, m - 1, d + n));
  return t.toISOString().slice(0, 10);
}

// Previous `count` months ending with `month` (YYYY-MM), oldest first.
export function lastMonths(month, count) {
  const [y, m] = month.split('-').map(Number);
  const out = [];
  for (let i = count - 1; i >= 0; i--) {
    const t = new Date(Date.UTC(y, m - 1 - i, 1));
    out.push(t.toISOString().slice(0, 7));
  }
  return out;
}

const SPAN_UNITS = {
  us: 1,
  ms: 1e3,
  s: 1e6,
  min: 60e6,
  h: 3600e6,
  d: 86400e6,
  w: 7 * 86400e6,
  month: 2629800e6, // systemd: 30.44 days
  y: 31557600e6, // systemd: 365.25 days
};

// systemd timespan ("2d 3h 4min 5.123456s", "0", "infinity", or raw microseconds) -> microseconds | null.
export function parseTimespanUs(text) {
  if (text === undefined || text === null) return null;
  const s = String(text).trim();
  if (s === '' || s === 'infinity' || s === 'n/a') return null;
  if (/^\d+$/.test(s)) return Number(s);
  let total = 0;
  let matched = '';
  for (const m of s.matchAll(/(\d+(?:\.\d+)?)\s*(month|min|ms|us|y|w|d|h|s)\b/g)) {
    total += Number(m[1]) * SPAN_UNITS[m[2]];
    matched += m[0];
  }
  return matched ? Math.round(total) : null;
}

// Monotonic "now" in microseconds (CLOCK_MONOTONIC, the clock systemd's *Monotonic properties use).
export function monotonicNowUs() {
  return Number(process.hrtime.bigint() / 1000n);
}

export function isoOrNull(value) {
  if (typeof value !== 'string') return null;
  const t = Date.parse(value);
  return Number.isNaN(t) ? null : new Date(t).toISOString();
}
