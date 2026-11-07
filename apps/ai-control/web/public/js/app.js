// The controller: state, rendering, the live connection (SSE with a 10 s poll fallback) and data loading.

import { h } from './dom.js';
import { initialState, reduce, TABS, auditKey, auditSince, historyMinutes, LOG_POLL_MS } from './state.js';
import { getJSON, paths, readSnapshot } from './api.js';
import { createShell } from './shell.js';
import { createOverview } from './tabs/overview.js';
import { createRouting } from './tabs/routing.js';
import { createModels } from './tabs/models.js';
import { createAudit } from './tabs/audit.js';
import { createLogs } from './tabs/logs.js';

const POLL_MS = 10_000;
const SSE_RETRY_MS = 30_000;
const TICK_MS = 15_000;
const HISTORY_MIN_GAP = 30_000;
const REQUESTS_MIN_GAP = 10_000;

export function startApp(root) {
  let state = initialState();
  let etag = null;
  let es = null;
  let pollTimer = null;
  let sseRetry = null;
  let logTimer = null;
  let lastHistory = 0;
  let lastRequests = 0;
  let renderQueued = false;
  const lastDeps = {};

  const dispatch = (action) => {
    const next = reduce(state, action);
    if (next === state) return;
    state = next;
    queueRender();
  };

  // --- actions the tabs call -------------------------------------------------------------------------
  const actions = {
    go: (tab) => setTab(tab),
    auditOptions: (o) => {
      dispatch({ type: 'audit/options', ...o });
      loadAudit();
    },
    refreshAudit: () => loadAudit(),
    openReplay: (session) => {
      dispatch({ type: 'replay/open', session });
      loadReplay(false);
    },
    replayOutput: () => {
      dispatch({ type: 'replay/output' });
      loadReplay(true);
    },
    closeReplay: () => {
      dispatch({ type: 'replay/close' });
      requestAnimationFrame(() => document.getElementById('audit-refresh')?.focus());
    },
    logOptions: (o) => {
      const before = state.logs;
      dispatch({ type: 'logs/options', ...o });
      if (state.logs.source !== before.source || state.logs.lines !== before.lines) loadLogs();
      scheduleLogPoll();
    },
    logFilter: (filter) => dispatch({ type: 'logs/filter', filter }),
    refreshLogs: () => loadLogs(),
  };

  // --- DOM ---------------------------------------------------------------------------------------------
  const shell = createShell({ onTab: (t) => setTab(t) });
  const tabs = {
    overview: createOverview(actions),
    routing: createRouting(actions),
    models: createModels(actions),
    audit: createAudit(actions),
    logs: createLogs(actions),
  };
  const panel = h('div', { class: 'panel' });
  shell.main.appendChild(panel);
  const skip = h('button', { type: 'button', class: 'skip', onclick: () => shell.main.focus() }, 'Skip to content');
  root.replaceChildren(skip, shell.header, shell.banner, shell.main, shell.nav);

  const depsFor = (tab, tick) => {
    const s = state.summary;
    switch (tab) {
      case 'overview':
        return [s, state.history, state.requests, state.conn, tick];
      case 'routing':
        return [s, state.history, tick];
      case 'models':
        return [s, tick];
      case 'audit':
        return [state.audit, s && s.server, tick];
      case 'logs':
        return [state.logs, s && s.timers, s && s.server, tick];
      default:
        return [];
    }
  };

  let shownTab = null;
  function render() {
    renderQueued = false;
    const now = Date.now();
    shell.update(state, now);
    const tick = Math.floor(now / TICK_MS);
    const tab = state.tab;
    if (shownTab !== tab) {
      panel.replaceChildren(tabs[tab].el);
      shownTab = tab;
      lastDeps[tab] = null;
    }
    const deps = depsFor(tab, tick);
    const prev = lastDeps[tab];
    if (prev && prev.length === deps.length && prev.every((v, i) => v === deps[i])) return;
    lastDeps[tab] = deps;
    // Keep focus on a control that is rebuilt with the same id.
    const focusedId = document.activeElement && panel.contains(document.activeElement) ? document.activeElement.id : null;
    tabs[tab].update(state, now);
    if (focusedId && document.activeElement && document.activeElement.id !== focusedId) document.getElementById(focusedId)?.focus();
  }
  function queueRender() {
    if (renderQueued) return;
    renderQueued = true;
    requestAnimationFrame(render);
  }

  // --- tabs (hash routing: #/overview ...) -------------------------------------------------------------
  function tabFromHash() {
    const t = location.hash.replace(/^#\/?/, '');
    return TABS.includes(t) ? t : 'overview';
  }
  function setTab(t) {
    if (!TABS.includes(t)) return;
    if (location.hash !== `#/${t}`) history.replaceState(null, '', `#/${t}`);
    enterTab(t);
  }
  function enterTab(t) {
    const changed = state.tab !== t;
    dispatch({ type: 'tab', tab: t });
    if (t === 'audit' && !state.audit.sudo.items && !state.audit.sudo.loading) loadAudit();
    if (t === 'logs' && !state.logs.text && !state.logs.loading) loadLogs();
    scheduleLogPoll();
    if (changed) requestAnimationFrame(() => shell.main.focus({ preventScroll: true }));
  }
  window.addEventListener('hashchange', () => enterTab(tabFromHash()));

  // --- loading -------------------------------------------------------------------------------------
  async function loadSummary() {
    try {
      const r = await getJSON(paths.summary(), { etag });
      if (r.status === 304) dispatch({ type: 'summary/unchanged', at: Date.now() });
      else {
        etag = r.etag;
        dispatch({ type: 'summary/loaded', summary: r.data, at: Date.now() });
        if (es && es.readyState === 1) dispatch({ type: 'conn', mode: 'live' });
        loadHistory();
        loadRequests();
      }
    } catch (e) {
      // status 0: no answer (network, timeout). Any HTTP status: ai-box answered with an error.
      const server = e && e.status > 0;
      dispatch({ type: 'summary/failed', kind: server ? 'server' : 'network', detail: server ? `HTTP ${e.status}` : null });
      if (!state.summary) {
        const snap = await readSnapshot();
        if (snap) dispatch({ type: 'snapshot/loaded', summary: snap.summary, savedAt: snap.savedAt });
      }
    }
  }
  async function loadHistory(force = false) {
    if (!force && Date.now() - lastHistory < HISTORY_MIN_GAP) return;
    lastHistory = Date.now();
    try {
      const minutes = historyMinutes(state.summary);
      const r = await getJSON(paths.history(minutes));
      dispatch({ type: 'history/loaded', points: r.data, minutes, at: Date.now() });
    } catch (e) {
      dispatch({ type: 'history/failed', error: e.message });
    }
  }
  async function loadRequests(force = false) {
    if (!force && Date.now() - lastRequests < REQUESTS_MIN_GAP) return;
    lastRequests = Date.now();
    try {
      const r = await getJSON(paths.requests(50));
      dispatch({ type: 'requests/loaded', items: Array.isArray(r.data) ? r.data : r.data && r.data.items, at: Date.now() });
    } catch (e) {
      dispatch({ type: 'requests/failed', error: e.message });
    }
  }
  async function loadLogSources() {
    try {
      const r = await getJSON(paths.logSources());
      dispatch({ type: 'logs/sources/loaded', data: r.data });
    } catch (e) {
      dispatch({ type: 'logs/sources/failed', error: e.message });
    }
  }
  async function loadLogs() {
    if (!state.logs.sources.items) {
      await loadLogSources(); // no list, no fallback: the tab shows the error
      if (!state.logs.sources.items) return;
    }
    const { source, lines } = state.logs;
    if (!source) return;
    dispatch({ type: 'logs/loading' });
    try {
      const r = await getJSON(paths.logs(source, lines));
      dispatch({ type: 'logs/loaded', source, lines, data: r.data, at: Date.now() });
    } catch (e) {
      dispatch({ type: 'logs/failed', source, error: e.message });
    }
  }
  async function loadPart(part, path) {
    const key = auditKey(state, part);
    dispatch({ type: 'audit/loading', part });
    try {
      const r = await getJSON(path);
      dispatch({ type: 'audit/loaded', part, key, data: r.data, at: Date.now() });
    } catch (e) {
      dispatch({ type: 'audit/failed', part, key, error: e.message });
    }
  }
  function loadAudit() {
    const a = state.audit;
    const since = auditSince(a.range);
    loadPart('sudo', paths.sudo(a.host, a.user, since));
    if (a.host === 'aibox') loadPart('sessions', paths.sessions(a.user));
    if (a.host === 'aibox' && a.user === 'localai') loadPart('localai', paths.localai(since));
  }
  async function loadReplay(output) {
    const rp = state.audit.replay;
    if (!rp) return;
    const session = rp.session;
    try {
      const r = await getJSON(paths.replay(session.id, state.audit.user, output));
      dispatch({ type: 'replay/loaded', session, output, data: r.data });
    } catch (e) {
      dispatch({ type: 'replay/failed', session, error: e.message });
    }
  }

  // --- live: SSE, falling back to a 10 s poll ------------------------------------------------------
  function startPoll() {
    if (pollTimer) return;
    pollTimer = setInterval(loadSummary, POLL_MS);
  }
  function stopPoll() {
    clearInterval(pollTimer);
    pollTimer = null;
  }
  function connectSSE() {
    clearTimeout(sseRetry);
    if (typeof EventSource === 'undefined') {
      dispatch({ type: 'conn', mode: 'polling' });
      startPoll();
      return;
    }
    es = new EventSource(paths.events());
    es.onopen = () => {
      stopPoll();
      dispatch({ type: 'conn', mode: 'live' });
      loadSummary();
    };
    const onChange = () => loadSummary();
    es.onmessage = onChange;
    for (const name of ['summary', 'summary-changed', 'changed']) es.addEventListener(name, onChange);
    es.onerror = () => {
      es.close();
      es = null;
      if (state.conn === 'live') dispatch({ type: 'conn', mode: 'polling' });
      startPoll();
      sseRetry = setTimeout(connectSSE, SSE_RETRY_MS);
    };
  }

  function scheduleLogPoll() {
    clearInterval(logTimer);
    logTimer = null;
    if (state.tab === 'logs' && state.logs.auto && document.visibilityState === 'visible') {
      logTimer = setInterval(() => {
        if (!state.logs.loading) loadLogs();
      }, LOG_POLL_MS);
    }
  }
  document.addEventListener('visibilitychange', () => {
    scheduleLogPoll();
    if (document.visibilityState === 'visible') loadSummary();
  });
  window.addEventListener('online', () => loadSummary());
  setInterval(queueRender, TICK_MS);

  // --- go ------------------------------------------------------------------------------------------
  enterTab(tabFromHash());
  loadSummary().then(() => {
    if (state.conn === 'connecting') dispatch({ type: 'conn', mode: 'polling' });
  });
  startPoll(); // until the stream opens
  connectSSE();
  queueRender();
  return { getState: () => state };
}
