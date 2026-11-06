// Pure data handling (no DOM): what the server sent → what the views draw. Everything the server sends is
// untrusted. A field of the wrong type becomes its fallback, a status we don't know becomes "unknown", a link
// that isn't plain http(s) is dropped, and a card that's missing becomes an "unknown" card. Nothing here
// throws on bad input.

/** Card ids, in page order. The server reports every one except `briefing` in /api/cards (README). */
export const CARD_ORDER = ['briefing', 'ai', 'ai_control', 'money', 'school', 'home', 'servers', 'alerts'];

export const CARD_META = {
  briefing: { title: 'Briefing', icon: 'briefing', linkLabel: null },
  ai: { title: 'AI', icon: 'ai', linkLabel: 'Open Open WebUI' },
  ai_control: { title: 'AI Control', icon: 'gauge', linkLabel: 'Open AI Control' },
  money: { title: 'Money', icon: 'money', linkLabel: 'Open Money Hub' },
  school: { title: 'School', icon: 'school', linkLabel: 'Open the ERAU dashboard' },
  home: { title: 'Home', icon: 'home', linkLabel: 'Open Home Assistant' },
  servers: { title: 'Servers & backups', icon: 'servers', linkLabel: 'Open the homelab dashboard' },
  alerts: { title: 'Alerts', icon: 'alert', linkLabel: null },
};

/** The Open WebUI model the AI card asks with (CONTRACTS §12.29: the agent router picks the real model). */
export const ASK_MODEL = 'agent_router.auto';
export const MAX_QUESTION = 2000;
const MAX_TEXT = 500;
const MAX_ITEMS = 12;

export const STATUS_WORD = { ok: 'OK', warn: 'Warning', down: 'Down', unknown: 'Unknown', setup: 'Set up' };
const STATUS_ALIASES = new Map(Object.entries({ ok: 'ok', good: 'ok', up: 'ok', healthy: 'ok', warn: 'warn', warning: 'warn', stale: 'warn', degraded: 'warn', down: 'down', error: 'down', fail: 'down', failed: 'down', crit: 'down', critical: 'down', setup: 'setup', unknown: 'unknown' }));

/** One of ok | warn | down | unknown | setup. Anything else (wrong type, unknown word) is "unknown". */
export function statusKey(v) {
  if (typeof v !== 'string') return 'unknown';
  return STATUS_ALIASES.get(v.trim().toLowerCase()) || 'unknown';
}

const isObj = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);

/** A display string: strings (trimmed, capped) and finite numbers; anything else gives `fallback`. */
export function str(v, fallback = '') {
  if (typeof v === 'number' && Number.isFinite(v)) return String(v);
  if (typeof v !== 'string') return fallback;
  const t = v.trim();
  if (!t) return fallback;
  return t.length > MAX_TEXT ? `${t.slice(0, MAX_TEXT - 1)}…` : t;
}

/** Epoch seconds → ms, or null. Strings and silly values are rejected. */
export function epochMs(v) {
  return typeof v === 'number' && Number.isFinite(v) && v > 1e9 && v < 1e11 ? v * 1000 : null;
}

/**
 * A link the page may put in an href: an absolute http(s) URL with no credentials in it, returned in the
 * URL parser's normalised form (so `<`, `"` and spaces come back percent-encoded). Anything else → null.
 */
export function safeHref(v) {
  if (typeof v !== 'string' || !v.trim() || v.length > 2048) return null;
  let u;
  try {
    u = new URL(v.trim());
  } catch {
    return null;
  }
  if (u.protocol !== 'https:' && u.protocol !== 'http:') return null;
  if (u.username || u.password) return null;
  return u.href;
}

/**
 * The AI card's deep link: Open WebUI at `base` with `?models=agent_router.auto&q=<question>`. Each value is
 * percent-encoded with encodeURIComponent (a space is %20, `&` is %26, `#` is %23), so the question can't add
 * or override a parameter, or end the query. Any query or fragment on `base` is replaced. Empty question →
 * just the model. Bad base → null.
 */
export function askUrl(base, question) {
  const href = safeHref(base);
  if (!href) return null;
  const u = new URL(href);
  const q = typeof question === 'string' ? question.trim().slice(0, MAX_QUESTION) : '';
  u.hash = '';
  u.search = `?models=${encodeURIComponent(ASK_MODEL)}${q ? `&q=${encodeURIComponent(q)}` : ''}`;
  return u.href;
}

function normItem(raw) {
  if (typeof raw === 'string' || typeof raw === 'number') {
    const label = str(raw);
    return label ? { label, value: '', status: null } : null;
  }
  if (!isObj(raw)) return null;
  const label = str(raw.label) || str(raw.text);
  const value = str(raw.value);
  if (!label && !value) return null;
  return { label: label || value, value: label ? value : '', status: raw.status === undefined || raw.status === null ? null : statusKey(raw.status) };
}

function normItems(v) {
  if (!Array.isArray(v)) return [];
  return v.map(normItem).filter(Boolean).slice(0, MAX_ITEMS);
}

/** One card from /api/cards. `present` is false when the server didn't send it (or sent something unusable). */
export function normCard(raw) {
  if (!isObj(raw)) return { present: false, status: 'unknown', headline: '', items: [], link: null, error: null, updatedAt: null, moreCount: 0 };
  const items = Array.isArray(raw.items) ? raw.items : [];
  const error = raw.error === null || raw.error === undefined || raw.error === false ? null : str(raw.error, 'unknown error');
  return {
    present: true,
    status: error && raw.status === undefined ? 'down' : statusKey(raw.status),
    headline: str(raw.headline),
    items: normItems(items),
    moreCount: Math.max(0, items.length - MAX_ITEMS),
    link: safeHref(raw.link),
    error,
    updatedAt: epochMs(raw.updated_at),
  };
}

/** /api/cards → {generatedAt, config: {openWebuiUrl}, cards: {id: card}} with every id present. */
export function normalizeCards(json) {
  const o = isObj(json) ? json : {};
  const cfg = isObj(o.config) ? o.config : {};
  const src = isObj(o.cards) ? o.cards : {};
  const cards = {};
  for (const id of CARD_ORDER) if (id !== 'briefing') cards[id] = normCard(Object.hasOwn(src, id) ? src[id] : undefined);
  return {
    generatedAt: epochMs(o.generated_at),
    config: {
      openWebuiUrl: safeHref(cfg.open_webui_url),
      vapidPublicKey: typeof cfg.vapid_public_key === 'string' && /^[A-Za-z0-9_-]{40,200}$/.test(cfg.vapid_public_key) ? cfg.vapid_public_key : null,
    },
    cards,
  };
}

/** /api/briefing → {date, headline, summary, generatedAt, sections: [{title, items, empty}]}. */
export function normalizeBriefing(json) {
  const o = isObj(json) ? json : {};
  const sections = (Array.isArray(o.sections) ? o.sections : [])
    .filter(isObj)
    .map((s) => ({ title: str(s.title, 'Untitled'), items: normItems(s.items), empty: str(s.empty, 'Nothing here.') }))
    .slice(0, MAX_ITEMS);
  return {
    date: typeof o.date === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(o.date) ? o.date : null,
    headline: str(o.headline),
    summary: str(o.summary),
    generatedAt: epochMs(o.generated_at),
    sections,
  };
}

/** "just now", "3 min ago", "2 h ago", "yesterday", "4 days ago". `ms` is epoch ms. */
export function ago(ms, now = Date.now()) {
  if (typeof ms !== 'number' || !Number.isFinite(ms)) return '';
  const s = Math.round((now - ms) / 1000);
  if (s < -60) return 'in the future (check the clock)';
  if (s < 45) return 'just now';
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h} h ago`;
  const d = Math.round(h / 24);
  return d === 1 ? 'yesterday' : `${d} days ago`;
}

/** "Tuesday 28 October" from a YYYY-MM-DD string (no time zone math: the server's date is the day). */
export function longDate(ymd) {
  if (typeof ymd !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(ymd)) return '';
  const [y, m, d] = ymd.split('-').map(Number);
  const dt = new Date(Date.UTC(y, m - 1, d));
  if (dt.getUTCMonth() !== m - 1) return '';
  return dt.toLocaleDateString('en-GB', { weekday: 'long', day: 'numeric', month: 'long', timeZone: 'UTC' }).replace(',', '');
}

/** `#card-money` → 'money'. Unknown or malformed → null. */
export function cardFromHash(hash) {
  if (typeof hash !== 'string') return null;
  const m = /^#card-([a-z_]+)$/.exec(hash);
  return m && CARD_ORDER.includes(m[1]) ? m[1] : null;
}

/** How a failed request reads: "no answer" or "HTTP 502". */
export function failText(f) {
  if (!f) return '';
  if (f.kind === 'network') return 'no answer';
  if (f.kind === 'http') return `HTTP ${Number.isInteger(f.status) ? f.status : '?'}`;
  return 'an unreadable reply';
}
