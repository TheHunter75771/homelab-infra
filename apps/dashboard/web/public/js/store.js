// App state: a pure reducer plus a tiny subscribable store. Components read it through a hook;
// the live connection and the actions dispatch into it.

export const initialState = {
  route: { name: 'overview' },
  // connecting: no data yet · live: in contact · stale: lost contact, showing data from this session
  // offline: no contact and only a snapshot saved on this device (or nothing)
  connection: 'connecting',
  summary: null,
  receivedAt: null, // ms of the last contact (summary, 304 or SSE ping)
  dataAt: null, // ms when the shown summary arrived
  serverAt: null, // the newest generatedAt seen (server time, ms): advances on every server refresh
  serverAtSeenAt: null, // client ms when serverAt last advanced
  snapshotAt: null, // ms when the shown snapshot was saved; null for live data
  pending: {}, // app name -> { kind, since, startedAtBefore }
  dialog: null, // { kind, app, deviceId, label, phase: 'confirm'|'progress', busy, step }
  toast: null, // { id, tone, text, icon }
  settings: null,
  settingsError: null,
  devices: null, // from GET /api/push/devices (falls back to summary.push.devices)
  meId: null, // this device's push subscription id, if it has one
  seq: 0,
};

export function reduce(state, action) {
  switch (action.type) {
    case 'route':
      return { ...state, route: action.route };
    case 'summary':
      if (!action.summary || typeof action.summary !== 'object') return state;
      return {
        ...fresher(state, action.summary.generatedAt, action.at),
        summary: action.summary,
        receivedAt: action.at,
        dataAt: action.at,
        snapshotAt: null,
        connection: 'live',
      };
    case 'contact':
      if (!state.summary || state.snapshotAt) return state;
      return { ...fresher(state, action.generatedAt, action.at), receivedAt: action.at, connection: 'live' };
    case 'snapshot':
      // Only fills in when nothing from this session is on screen.
      if (state.summary && !state.snapshotAt) return state;
      if (!action.snapshot?.summary) return state;
      return { ...state, summary: action.snapshot.summary, snapshotAt: action.snapshot.savedAt, dataAt: action.snapshot.savedAt };
    case 'unreachable':
      if (state.summary && !state.snapshotAt) return { ...state, connection: 'stale' };
      return { ...state, connection: 'offline' };
    case 'pending/set':
      return { ...state, pending: { ...state.pending, [action.name]: action.value } };
    case 'pending/clear': {
      if (!state.pending[action.name]) return state;
      const pending = { ...state.pending };
      delete pending[action.name];
      return { ...state, pending };
    }
    case 'dialog':
      return { ...state, dialog: action.dialog };
    case 'dialog/patch':
      if (!state.dialog) return state;
      return { ...state, dialog: { ...state.dialog, ...action.patch } };
    case 'toast': {
      const seq = state.seq + 1;
      return { ...state, seq, toast: { id: seq, tone: action.tone, text: action.text, icon: action.icon } };
    }
    case 'toast/clear':
      if (!state.toast || (action.id !== undefined && state.toast.id !== action.id)) return state;
      return { ...state, toast: null };
    case 'settings':
      return { ...state, settings: action.settings, settingsError: null };
    case 'settings/error':
      return { ...state, settingsError: action.error };
    case 'devices':
      return Array.isArray(action.devices) ? { ...state, devices: action.devices } : state;
    case 'me':
      return { ...state, meId: action.id || null };
    default:
      return state;
  }
}

// Track the server's own refreshes (summary.generatedAt, tick events, X-Generated-At on a 304). Measured on
// the client's clock, so clock skew between phone and box doesn't matter.
function fresher(state, generatedAt, at) {
  const t = generatedAt ? Date.parse(generatedAt) : NaN;
  if (!Number.isFinite(t) || (state.serverAt !== null && t <= state.serverAt)) return state;
  return { ...state, serverAt: t, serverAtSeenAt: at };
}

// The server refreshes every 10 s. Pings prove the connection, not the data: when its refreshes stop for
// this long (about 3 intervals), the data is shown as stale even though the stream is up.
export const SERVER_STALE_MS = 35000;

export function serverStale(state, now) {
  return state.connection === 'live' && state.serverAtSeenAt !== null && now - state.serverAtSeenAt > SERVER_STALE_MS;
}

export function createStore(reducer = reduce, init = initialState) {
  let state = init;
  const listeners = new Set();
  return {
    getState: () => state,
    dispatch(action) {
      const next = reducer(state, action);
      if (next === state) return;
      const prev = state;
      state = next;
      for (const fn of [...listeners]) fn(state, prev, action);
    },
    subscribe(fn) {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
  };
}

// Are actions allowed right now? Returns the reason they aren't, or null.
export function actionsPausedReason(state) {
  if (state.connection === 'live') return null;
  return 'Backend unreachable. Actions are paused';
}
