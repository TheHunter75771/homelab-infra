// App state: one pure reducer plus selectors. No DOM, no fetch; app.js dispatches and the tabs read.

export const TABS = ['overview', 'routing', 'models', 'audit', 'logs'];

export const LOG_LINE_COUNTS = [100, 200, 500, 1000];
export const AUDIT_HOSTS = [
  { id: 'aibox', label: 'ai-box' },
  { id: 'pc', label: 'Gaming PC' },
];
export const AUDIT_USERS = ['claude', 'localai'];
export const AUDIT_RANGES = [
  { id: '1d', label: 'Last 24 hours', hours: 24 },
  { id: '7d', label: 'Last 7 days', hours: 168 },
  { id: '30d', label: 'Last 30 days', hours: 720 },
];
export const REPLAY_CAP = 262144; // helper X max_bytes (§12.15.2)
export const LOG_POLL_MS = 10_000;

const empty = () => ({ items: null, error: null, loading: false, at: null });

export function initialState() {
  return {
    tab: 'overview',
    summary: null,
    summaryAt: null, // ms when the summary shown was produced (receipt time, or the snapshot's savedAt)
    fromSnapshot: false,
    conn: 'connecting', // connecting | live | polling | offline
    failure: null, // { kind: 'network'|'server', detail } while the summary can't be refreshed
    history: { points: null, minutes: 30, error: null, at: null },
    requests: empty(),
    // `sources` comes from GET /api/logs/sources; `source` stays null until that list has loaded.
    logs: { sources: { items: null, error: null }, source: null, lines: 200, filter: '', auto: false, text: null, truncated: false, error: null, loading: false, at: null },
    audit: {
      host: 'aibox',
      user: 'claude',
      range: '1d',
      sudo: empty(),
      sessions: empty(),
      localai: empty(),
      replay: null, // { session, output, data, error, loading }
    },
  };
}

const isObj = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);

export function reduce(state, action) {
  switch (action.type) {
    case 'tab':
      return TABS.includes(action.tab) ? { ...state, tab: action.tab } : state;

    case 'summary/loaded':
      if (!isObj(action.summary)) return reduce(state, { type: 'summary/failed', kind: 'server', detail: 'unreadable summary', at: action.at });
      return { ...state, summary: action.summary, summaryAt: action.at, fromSnapshot: false, failure: null, conn: state.conn === 'offline' || state.conn === 'connecting' ? 'polling' : state.conn };

    case 'summary/unchanged': // a 304: the data is still current
      return { ...state, summaryAt: state.fromSnapshot ? state.summaryAt : action.at, failure: null, conn: state.conn === 'offline' ? 'polling' : state.conn };

    case 'summary/failed':
      // kind 'network': no answer at all. kind 'server': ai-box answered, with an HTTP error or a summary that
      // can't be read. `detail` is "HTTP 502" or "unreadable summary" (built by app.js, not server text).
      return {
        ...state,
        conn: 'offline',
        failure: { kind: action.kind === 'server' ? 'server' : 'network', detail: typeof action.detail === 'string' ? action.detail : null },
      };

    case 'snapshot/loaded':
      // Only fills in when there is nothing newer on screen.
      if (!isObj(action.summary) || (state.summary && !state.fromSnapshot)) return state;
      return { ...state, summary: action.summary, summaryAt: action.savedAt, fromSnapshot: true };

    case 'conn':
      return ['connecting', 'live', 'polling', 'offline'].includes(action.mode) ? { ...state, conn: action.mode } : state;

    case 'history/loaded':
      return { ...state, history: { points: Array.isArray(action.points) ? action.points : [], minutes: Number.isInteger(action.minutes) ? action.minutes : 30, error: null, at: action.at } };
    case 'history/failed':
      return { ...state, history: { ...state.history, error: action.error || 'unavailable' } };

    case 'requests/loaded':
      return { ...state, requests: { items: Array.isArray(action.items) ? action.items : [], error: null, loading: false, at: action.at } };
    case 'requests/failed':
      return { ...state, requests: { ...state.requests, error: action.error || 'unavailable', loading: false } };

    case 'logs/sources/loaded': {
      const items = normaliseSources(action.data);
      const keep = items.some((x) => x.id === state.logs.source);
      const source = keep ? state.logs.source : items.length ? items[0].id : null;
      const changed = source !== state.logs.source;
      return { ...state, logs: { ...state.logs, sources: { items, error: null }, source, ...(changed ? { text: null, error: null, truncated: false } : {}) } };
    }
    case 'logs/sources/failed':
      return { ...state, logs: { ...state.logs, sources: { items: state.logs.sources.items, error: action.error || 'unavailable' } } };

    case 'logs/options': {
      const next = { ...state.logs };
      if (Array.isArray(state.logs.sources.items) && state.logs.sources.items.some((s) => s.id === action.source)) next.source = action.source;
      if (LOG_LINE_COUNTS.includes(action.lines)) next.lines = action.lines;
      if (typeof action.auto === 'boolean') next.auto = action.auto;
      const changed = next.source !== state.logs.source || next.lines !== state.logs.lines;
      if (changed) Object.assign(next, { text: null, error: null, truncated: false });
      return { ...state, logs: next };
    }
    case 'logs/filter':
      return { ...state, logs: { ...state.logs, filter: typeof action.filter === 'string' ? action.filter : '' } };
    case 'logs/loading':
      return { ...state, logs: { ...state.logs, loading: true } };
    case 'logs/loaded':
      // A reply for a source the user has already left is dropped.
      if (action.source !== state.logs.source || action.lines !== state.logs.lines) return state;
      return { ...state, logs: { ...state.logs, text: normaliseLines(action.data), truncated: !!(isObj(action.data) && action.data.truncated), error: null, loading: false, at: action.at } };
    case 'logs/failed':
      if (action.source !== state.logs.source) return state;
      return { ...state, logs: { ...state.logs, error: action.error || 'unavailable', loading: false } };

    case 'audit/options': {
      const a = { ...state.audit };
      if (AUDIT_HOSTS.some((h) => h.id === action.host)) a.host = action.host;
      if (AUDIT_USERS.includes(action.user)) a.user = action.user;
      if (AUDIT_RANGES.some((r) => r.id === action.range)) a.range = action.range;
      if (a.host === state.audit.host && a.user === state.audit.user && a.range === state.audit.range) return state;
      return {
        ...state,
        audit: {
          ...a,
          sudo: empty(),
          sessions: a.user === state.audit.user ? state.audit.sessions : empty(),
          localai: a.range === state.audit.range ? state.audit.localai : empty(),
          replay: null,
        },
      };
    }
    case 'audit/loading':
      if (!['sudo', 'sessions', 'localai'].includes(action.part)) return state;
      return { ...state, audit: { ...state.audit, [action.part]: { ...state.audit[action.part], loading: true } } };
    case 'audit/loaded': {
      if (!['sudo', 'sessions', 'localai'].includes(action.part)) return state;
      if (action.key && action.key !== auditKey(state, action.part)) return state; // stale reply
      return { ...state, audit: { ...state.audit, [action.part]: { items: listOf(action.data), skipped: skippedOf(action.data), error: null, loading: false, at: action.at } } };
    }
    case 'audit/failed':
      if (!['sudo', 'sessions', 'localai'].includes(action.part)) return state;
      if (action.key && action.key !== auditKey(state, action.part)) return state;
      return { ...state, audit: { ...state.audit, [action.part]: { ...state.audit[action.part], error: action.error || 'unavailable', loading: false } } };

    case 'replay/open':
      return { ...state, audit: { ...state.audit, replay: { session: action.session, output: false, data: null, error: null, loading: true } } };
    case 'replay/output':
      if (!state.audit.replay) return state;
      return { ...state, audit: { ...state.audit, replay: { ...state.audit.replay, output: true, loading: true, error: null } } };
    case 'replay/loaded':
      if (!state.audit.replay || state.audit.replay.session !== action.session) return state;
      // A command-only reply (output=0; its `output` is null) that lands after "Show output" is dropped.
      if (typeof action.output === 'boolean' && action.output !== state.audit.replay.output) return state;
      return { ...state, audit: { ...state.audit, replay: { ...state.audit.replay, data: isObj(action.data) ? action.data : null, loading: false, error: isObj(action.data) ? null : 'The replay came back in a shape AI Control can\'t read.' } } };
    case 'replay/failed':
      if (!state.audit.replay || state.audit.replay.session !== action.session) return state;
      return { ...state, audit: { ...state.audit, replay: { ...state.audit.replay, loading: false, error: action.error || 'unavailable' } } };
    case 'replay/close':
      return { ...state, audit: { ...state.audit, replay: null } };

    default:
      return state;
  }
}

/** Which request an audit reply belongs to, so a slow reply for an old picker value is dropped. */
export function auditKey(state, part) {
  const a = state.audit;
  if (part === 'sudo') return `${a.host}/${a.user}/${a.range}`;
  if (part === 'sessions') return a.user;
  return `localai/${a.range}`;
}

/** The `since` for the audit period: integer epoch seconds (§12.15 Lead decisions). */
export function auditSince(range, now = Date.now()) {
  const r = AUDIT_RANGES.find((x) => x.id === range) || AUDIT_RANGES[0];
  return Math.floor((now - r.hours * 3_600_000) / 1000);
}

/** `/api/audit/sessions` is {"sessions":[...], "skipped":N}: N session logs couldn't be read. Only a positive
 * integer counts; anything else (missing, a string, negative) is 0. */
function skippedOf(data) {
  const n = isObj(data) ? data.skipped : null;
  return Number.isInteger(n) && n > 0 ? n : 0;
}

function listOf(data) {
  if (Array.isArray(data)) return data;
  if (isObj(data)) for (const k of ['items', 'sessions', 'commands', 'entries']) if (Array.isArray(data[k])) return data[k];
  return [];
}

/** /api/logs replies → an array of strings (accepts {lines:[...]}, a bare array, or {text}). */
export function normaliseLines(data) {
  let lines = [];
  if (Array.isArray(data)) lines = data;
  else if (isObj(data) && Array.isArray(data.lines)) lines = data.lines;
  else if (isObj(data) && typeof data.text === 'string') lines = data.text.split('\n');
  return lines.map((l) => (typeof l === 'string' ? l : isObj(l) && typeof l.line === 'string' ? l.line : isObj(l) && typeof l.message === 'string' ? l.message : '')).filter((l, i, arr) => !(l === '' && i === arr.length - 1));
}

/** /api/logs/sources → [{id, label}] with string ids; anything else is dropped. A missing label shows the id. */
export function normaliseSources(data) {
  if (!Array.isArray(data)) return [];
  const seen = new Set();
  const out = [];
  for (const x of data) {
    if (!isObj(x) || typeof x.id !== 'string' || !x.id || seen.has(x.id)) continue;
    seen.add(x.id);
    out.push({ id: x.id, label: typeof x.label === 'string' && x.label ? x.label : x.id });
  }
  return out;
}

/** Client-side log filter: case-insensitive substring; several words must all match. */
export function filterLines(lines, filter) {
  if (!Array.isArray(lines)) return [];
  const words = String(filter || '').toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return lines;
  return lines.filter((l) => {
    const low = String(l).toLowerCase();
    return words.every((w) => low.includes(w));
  });
}

/** Session ids as the helper validates them (§12.15.2). Anything else is never put in a URL. */
export function validSessionId(id) {
  return typeof id === 'string' && (/^[0-9A-Z]{6}$/.test(id) || /^[0-9A-Za-z/._-]{1,64}$/.test(id)) && !id.includes('..');
}

/** The connection line in the header. */
export function connText(state) {
  if (state.conn === 'offline') return state.failure && state.failure.kind === 'server' ? 'Server error' : 'Offline';
  if (state.conn === 'live') return 'Live';
  if (state.conn === 'polling') return 'Updating every 10 s';
  return 'Connecting…';
}

/** The age of what's on screen, for the offline banner. */
export function dataAgeMs(state, now = Date.now()) {
  return typeof state.summaryAt === 'number' ? Math.max(0, now - state.summaryAt) : null;
}

/** Disk rows with a free percentage and a low flag (< 10% free, the server's alert threshold). */
export function diskRows(disks) {
  if (!Array.isArray(disks)) return [];
  return disks
    .filter((d) => d && typeof d === 'object')
    .map((d) => {
      const total = Number.isFinite(d.total) && d.total > 0 ? d.total : null;
      const avail = Number.isFinite(d.avail) && d.avail >= 0 ? d.avail : null;
      const freePct = total !== null && avail !== null ? (avail / total) * 100 : null;
      return { mount: d.mount, total, avail, used: total !== null && avail !== null ? total - avail : null, freePct, low: freePct !== null && freePct < 10 };
    });
}

/** Minutes of gate history to fetch: the gate's window, within the server's 30..1440 range. */
export function historyMinutes(summary) {
  const w = summary && summary.routing && summary.routing.window_s;
  if (typeof w !== 'number' || !Number.isFinite(w) || w <= 0) return 30;
  return Math.min(1440, Math.max(30, Math.round(w / 60)));
}

/** History points → the two series the sparkline draws, 0..100, ordered by time. */
export function historySeries(points) {
  if (!Array.isArray(points)) return [];
  return points
    .filter((p) => p && typeof p === 'object')
    .map((p) => {
      const busy = Number.isFinite(p.gpu_busy) ? p.gpu_busy : null;
      const share = Number.isFinite(p.ollama_share) ? p.ollama_share : 0;
      return {
        ts: p.ts,
        game: busy === null ? null : Math.min(100, Math.max(0, busy - share)),
        avg: Number.isFinite(p.avg30) ? Math.min(100, Math.max(0, p.avg30)) : null,
        available: p.available === true,
      };
    });
}

export function historyStats(series) {
  const games = series.map((p) => p.game).filter((v) => v !== null);
  const avgs = series.map((p) => p.avg).filter((v) => v !== null);
  return {
    now: games.length ? games[games.length - 1] : null,
    peak: games.length ? Math.max(...games) : null,
    avg: avgs.length ? avgs[avgs.length - 1] : null,
    unavailableCount: series.filter((p) => !p.available).length,
    count: series.length,
  };
}
