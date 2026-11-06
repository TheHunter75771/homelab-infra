// Formatters. Pure functions, no DOM. Every output is plain text.

const DASH = '—';

const isNum = (v) => typeof v === 'number' && Number.isFinite(v);

/**
 * A timestamp to epoch milliseconds, or null. The server sends epoch seconds only (§12.15 Lead decisions);
 * the page's own times (receipt, snapshot savedAt) are epoch ms. Strings are not accepted.
 */
export function toMs(ts) {
  if (!isNum(ts) || ts < 0) return null;
  return ts < 1e11 ? ts * 1000 : ts;
}

/** The gate's averaging window (seconds) as words: "30 min", "1 h", "90 min", "2 h". Null when unknown. */
export function windowText(seconds) {
  if (!isNum(seconds) || seconds <= 0) return null;
  const min = Math.round(seconds / 60);
  if (min >= 60 && min % 60 === 0) return `${min / 60} h`;
  return `${Math.max(1, min)} min`;
}

/** Bytes in decimal units, as Ollama shows model sizes ("4.7 GB"). */
export function bytes(n) {
  if (!isNum(n) || n < 0) return DASH;
  const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
  let i = 0;
  let v = n;
  while (v >= 1000 && i < units.length - 1) {
    v /= 1000;
    i += 1;
  }
  if (i === 0) return `${Math.round(v)} B`;
  const digits = v >= 100 ? 0 : 1;
  return `${v.toFixed(digits)} ${units[i]}`;
}

/** A duration given in milliseconds: "850 ms", "2.3 s", "4 min 12 s", "3 h 5 min", "2 d 4 h". */
export function duration(ms) {
  if (!isNum(ms) || ms < 0) return DASH;
  if (ms < 1000) return `${Math.round(ms)} ms`;
  const s = ms / 1000;
  if (s < 10) return `${(Math.floor(s * 10) / 10).toFixed(1)} s`;
  if (s < 60) return `${Math.floor(s)} s`;
  const totalS = Math.floor(s);
  const m = Math.floor(totalS / 60);
  if (m < 60) {
    const rs = totalS % 60;
    return rs ? `${m} min ${rs} s` : `${m} min`;
  }
  const h = Math.floor(m / 60);
  if (h < 24) {
    const rm = m % 60;
    return rm ? `${h} h ${rm} min` : `${h} h`;
  }
  const d = Math.floor(h / 24);
  const rh = h % 24;
  return rh ? `${d} d ${rh} h` : `${d} d`;
}

/** Time relative to `now` (ms): "just now", "12 s ago", "5 min ago", "in 3 min". */
export function relTime(ts, now = Date.now()) {
  const t = toMs(ts);
  if (t === null || !isNum(now)) return DASH;
  const diff = now - t;
  const abs = Math.abs(diff);
  if (abs < 5000) return 'just now';
  let text;
  if (abs < 60_000) text = `${Math.floor(abs / 1000)} s`;
  else if (abs < 3_600_000) text = `${Math.floor(abs / 60_000)} min`;
  else if (abs < 86_400_000) text = `${Math.floor(abs / 3_600_000)} h`;
  else text = `${Math.floor(abs / 86_400_000)} d`;
  return diff >= 0 ? `${text} ago` : `in ${text}`;
}

/** Tokens per second: "38.2 tok/s", "120 tok/s". */
export function tokS(v) {
  if (!isNum(v) || v < 0) return DASH;
  return v >= 100 ? `${Math.round(v)} tok/s` : `${v.toFixed(1)} tok/s`;
}

/** A percentage 0..100: "62%". */
export function pct(v) {
  if (!isNum(v)) return DASH;
  return `${Math.round(v)}%`;
}

/** Clock time (and the date when it isn't today) in the given IANA time zone, else the browser's. */
export function clock(ts, { timeZone, now = Date.now(), seconds = false } = {}) {
  const t = toMs(ts);
  if (t === null) return DASH;
  const opts = { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' };
  if (seconds) opts.second = '2-digit';
  const dayOpts = { year: 'numeric', month: '2-digit', day: '2-digit' };
  let tz = timeZone;
  try {
    if (tz) new Intl.DateTimeFormat('en-GB', { timeZone: tz });
  } catch {
    tz = undefined; // an unknown zone name falls back to the browser's
  }
  const fmt = (o) => new Intl.DateTimeFormat('en-GB', tz ? { ...o, timeZone: tz } : o).format(t);
  const sameDay = isNum(now) && fmt(dayOpts) === new Intl.DateTimeFormat('en-GB', tz ? { ...dayOpts, timeZone: tz } : dayOpts).format(now);
  if (sameDay) return fmt(opts);
  return `${new Intl.DateTimeFormat('en-GB', tz ? { day: 'numeric', month: 'short', timeZone: tz } : { day: 'numeric', month: 'short' }).format(t)} ${fmt(opts)}`;
}

/** Any value as display text. Objects never reach the page as "[object Object]". */
export function str(v, fallback = DASH) {
  if (v === null || v === undefined || v === '') return fallback;
  if (typeof v === 'string') return v;
  if (typeof v === 'number' || typeof v === 'boolean') return String(v);
  return fallback;
}

export { DASH };
