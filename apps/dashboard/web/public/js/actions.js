// User actions: app restart/stop/start/update, gate re-run, push devices, test push, settings.
// Each one drives the dialog/toast/pending state in the store and confirms the outcome from the
// summary (or the action's progress stream when the server gives one).

import { post, del, put, getJSON, streamSSE, parseJSON, appPath, ApiError } from './api.js';
import { normalizeApp, pendingOutcome, APP_META, gateStatus } from './model.js';
import { shortImage, fmtCountdown, fmtClock, plural } from './format.js';
import * as pushLib from './push.js';

// Server step names (apps/dashboard/server, "Action steps") -> index in the dialog's step list.
// The gate re-run can only see the trigger being picked up and the retry service finishing, so its list
// is the server's two steps: requested -> running, then done | failed.
export const STEP_INDEX = {
  update: { pulling: 0, recreating: 1, 'waiting-health': 2, done: 3 },
  rerun: { requested: 0, running: 1, done: 2 },
  reboot: { requested: 0, scheduled: 1, done: 2 },
};

// Media-app updates (CONTRACTS §11.7.2): the helper's per-app steps, as the progress rows say them.
export const MEDIA_STEP_LABEL = {
  pulling: 'Pulling',
  pinning: 'Saving the new version',
  recreating: 'Recreating',
  'waiting-health': 'Waiting for health check',
  'rolling-back': 'Rolling back',
};
export const MAJOR_NOTE = 'Major version: read the release notes first';
// status `ahead` (§11.7.2): the pin is newer than the registry's latest; never offered (a downgrade can break databases)
export const AHEAD_NOTE = "The pinned version is newer than the registry's latest release.";

// A version for display: the tag of a pin ("lscr.io/x/sonarr:4.0.20-ls7@sha256:…" → "4.0.20-ls7"), or the
// value itself when it is already a version.
export function versionOf(v) {
  if (v && typeof v === 'object') return versionOf(v.version ?? v.pin);
  const s = String(v ?? '').trim();
  if (!s) return '';
  if (!s.includes('@') && !s.includes('/')) return s;
  const ref = s.split('@')[0];
  const i = ref.lastIndexOf(':');
  return i > ref.lastIndexOf('/') ? ref.slice(i + 1) : ref;
}

// Rows of a GET /api/media/updates answer: [{name, label, status, major, current, latest, message}].
export function mediaCheckApps(check) {
  return (Array.isArray(check?.apps) ? check.apps : [])
    .filter((a) => a && typeof a.name === 'string')
    .map((a) => ({
      name: a.name,
      label: String(a.label || APP_META[a.name]?.label || a.name),
      status: ['update', 'current', 'ahead', 'deprecated', 'error'].includes(a.status) ? a.status : 'error',
      major: Boolean(a.major),
      current: versionOf(a.current),
      latest: versionOf(a.latest),
      message: typeof a.message === 'string' ? a.message : '',
    }));
}

// What is ticked when a check arrives: every update except the major ones.
export function defaultSelection(check) {
  return Object.fromEntries(mediaCheckApps(check).filter((a) => a.status === 'update').map((a) => [a.name, !a.major]));
}

// The names to send, in the check's order: ticked and still offered as an update.
export function selectedApps(dialog) {
  const sel = dialog?.selected || {};
  return mediaCheckApps(dialog?.check)
    .filter((a) => a.status === 'update' && sel[a.name])
    .map((a) => a.name);
}

// Progress rows of a media update, folded from the action's raw step objects. Pure.
// row: {name, label, from, to, state: 'waiting'|'running'|'done'|'failed', step, rolledBack}
export function applyMediaStep(rows, data, labelOf = (n) => APP_META[n]?.label || n) {
  const list = Array.isArray(rows) ? rows : [];
  const step = String(data?.step ?? '').toLowerCase();
  const name = typeof data?.app === 'string' ? data.app : null;
  if (!name) return list;
  const i = list.findIndex((r) => r.name === name);
  const base = i === -1 ? { name, label: labelOf(name), from: '', to: '', state: 'waiting', step: null, rolledBack: false } : list[i];
  let row = base;
  if (step === 'app') {
    row = { ...base, state: 'running', step: null, from: versionOf(data.from) || base.from, to: versionOf(data.to) || base.to, index: Number.isFinite(base.index) ? base.index : Number.isFinite(Number(data.index)) && data.index !== null ? Number(data.index) : undefined };
  } else if (step === 'app-done') {
    row = { ...base, state: 'done', step: null };
  } else if (step === 'app-failed') {
    row = { ...base, state: 'failed', step: null, rolledBack: Boolean(data.rolledBack ?? data.rolled_back) };
  } else if (MEDIA_STEP_LABEL[step]) {
    row = { ...base, state: 'running', step, rolledBack: base.rolledBack || step === 'rolling-back' };
  } else {
    return list;
  }
  const next = i === -1 ? [...list, row] : list.map((r, j) => (j === i ? row : r));
  // the server's order (index) when it gave one; arrival order otherwise
  return next.every((r) => Number.isFinite(r.index)) ? [...next].sort((a, b) => a.index - b.index) : next;
}

// The text of a progress row's status.
export function mediaRowStatus(r) {
  if (r.state === 'done') return 'Updated';
  if (r.state === 'failed') return r.rolledBack ? 'Failed, rolled back' : 'Failed';
  if (r.state === 'running') return MEDIA_STEP_LABEL[r.step] || 'Starting';
  return 'Waiting';
}

function rebootLines(view) {
  const lines = [];
  const jf = view?.media?.tiles?.jellyfin;
  const watching = jf && jf.state === 'ok' && Array.isArray(jf.sessions) ? jf.sessions.length : 0;
  if (watching === 1) lines.push({ icon: 'play-circle', tone: 'warn', text: '1 person is watching right now.' });
  else if (watching > 1) lines.push({ icon: 'play-circle', tone: 'warn', text: `${watching} people are watching right now.` });
  if (view?.host?.rebootRequired) {
    const pkgs = (view.host.rebootPackages || []).map(String).filter(Boolean);
    lines.push({ icon: 'info', tone: 'info', text: pkgs.length ? `Installs the pending updates: ${pkgs.join(', ')}.` : 'Installs the pending updates.' });
  }
  return lines;
}

function mediaUpdateContent(dialog) {
  if (dialog.phase === 'progress') {
    const rows = (dialog.progress || []).map((r) => ({
      key: r.name,
      label: r.label,
      versions: r.from && r.to ? `${r.from} → ${r.to}` : r.to || r.from || '',
      status: mediaRowStatus(r),
      tone: r.state === 'done' ? 'ok' : r.state === 'failed' ? 'fail' : r.state === 'running' ? 'info' : 'idle',
      icon: r.state === 'done' ? 'check-circle' : r.state === 'failed' ? 'x-circle' : r.state === 'running' ? 'circle-notch' : 'circle',
      spin: r.state === 'running',
    }));
    return {
      icon: 'arrow-circle-up',
      tone: 'info',
      title: 'Updating media apps…',
      body: 'One app at a time. Each one is down for about a minute, and it is rolled back if its health check fails. The update stops at the first failure.',
      rowsLabel: 'Update progress',
      rows,
    };
  }
  const apps = mediaCheckApps(dialog.check);
  const sel = dialog.selected || {};
  const n = selectedApps(dialog).length;
  let body;
  if (dialog.loading) body = 'Checking for new stable releases…';
  else if (dialog.error) body = `Couldn't check for updates: ${String(dialog.error).replace(/\.$/, '')}.`;
  else if (!apps.length) body = 'No media apps to check.';
  else if (!apps.some((a) => a.status === 'update')) body = 'Every media app is on its newest stable release.';
  else body = 'Tick the apps to update. They update one at a time, each is down for about a minute, and a failed health check rolls it back.';
  const checkedAt = dialog.check?.checkedAt ? new Date(dialog.check.checkedAt) : null;
  const rows = dialog.loading
    ? []
    : apps.map((a) => {
        const row = { key: a.name, label: a.label, versions: a.status === 'update' ? `${a.current} → ${a.latest}` : a.current };
        if (a.status === 'update') return { ...row, checkbox: true, checked: Boolean(sel[a.name]), note: a.major ? MAJOR_NOTE : null, noteTone: 'warn' };
        if (a.status === 'current') return { ...row, status: 'Up to date', tone: 'ok', icon: 'check-circle' };
        if (a.status === 'ahead') return { ...row, status: a.message || AHEAD_NOTE, tone: 'info', icon: 'info' };
        if (a.status === 'deprecated') return { ...row, status: a.message || 'This image is no longer maintained.', tone: 'warn', icon: 'warning' };
        return { ...row, status: a.message || "Couldn't look up the newest release.", tone: 'fail', icon: 'x-circle' };
      });
  return {
    icon: 'arrow-circle-up',
    tone: 'info',
    title: 'Media app updates',
    body,
    meta: checkedAt && !Number.isNaN(checkedAt.getTime()) && !dialog.loading ? `Checked at ${fmtClock(checkedAt)}` : null,
    rowsLabel: 'Media apps',
    rows,
    confirm: `Update ${plural(n, 'app')}`,
    confirmReason: dialog.loading ? 'Checking' : n === 0 ? 'Tick at least one app' : null,
    secondary: { key: 'recheck', label: 'Check again', icon: 'arrows-clockwise', reason: dialog.loading ? 'Checking' : null },
  };
}

// What a dialog says. `app` is a derived app (deriveApps), `device` a {id,label}, `view` the built view.
// Optional extras: `lines` (notes under the body), `rows` (a list: checkboxes in review, status per row in
// progress), `confirmReason` (disables the confirm button), `secondary` (a second action button).
export function dialogContent(dialog, { app, gate, view } = {}) {
  if (!dialog) return null;
  const L = app?.label || dialog.label || dialog.app || '';
  switch (dialog.kind) {
    case 'stop':
      return {
        icon: 'stop-circle',
        tone: 'warn',
        title: `Stop ${L}?`,
        body: `${L} stays stopped until you start it again. It shows as “stopped on purpose” and won't raise alerts.`,
        confirm: 'Stop',
      };
    case 'start':
      return {
        icon: 'play-circle',
        tone: 'info',
        title: `Start ${L}?`,
        body: `Starts the container on its pinned image (${shortImage(app?.pinned || app?.image)}).`,
        confirm: 'Start',
      };
    case 'update':
      return {
        icon: 'arrow-circle-up',
        tone: 'info',
        title: dialog.phase === 'progress' ? `Updating ${L}…` : `Update ${L} to ${dialog.target}?`,
        body: `Pulls ${shortImage(app?.pinned) || dialog.target}, recreates the container and waits for its health check. ${L} is down for about a minute.`,
        confirm: 'Update',
        steps: ['Pulling image', 'Recreating container', 'Waiting for health check'],
      };
    case 'rerun':
      return {
        icon: 'hard-drive',
        tone: 'warn',
        title: dialog.phase === 'progress' ? 'Running the check…' : 'Re-run the media-drive check?',
        body: `Re-checks the media drive now: detect, verify and mount at ${gate?.mountPath || '/media'}. Never reboots. Apps stay held until it passes.`,
        confirm: 'Run check',
        // CORRECTIONS 3 (amended): only what the server can observe.
        steps: ['Requested', `Checking the drive (detect, verify, mount ${gate?.mountPath || '/media'})`],
      };
    case 'reboot':
      return {
        icon: 'arrows-clockwise',
        tone: 'fail',
        title: 'Restart the media server?',
        body: 'Everything on the box (Jellyfin, requests, downloads and this dashboard) is down for about 2–5 minutes. When it comes back, the boot gate checks the media drive before the apps start.',
        lines: rebootLines(view),
        confirm: 'Restart host',
      };
    case 'media-update':
      return mediaUpdateContent(dialog);
    case 'remove':
      return {
        icon: 'bell-slash',
        tone: 'fail',
        title: `Remove ${dialog.label || 'this device'}?`,
        body: 'This device stops getting pushes. You can subscribe it again later.',
        confirm: 'Remove',
      };
    default:
      return null;
  }
}

const errText = (err) => {
  if (err instanceof ApiError) {
    if (err.status === 403 && /tailnet|remote/i.test(err.message)) return 'Only available on the tailnet';
    return err.message;
  }
  if (err?.name === 'AbortError' || err?.name === 'TimeoutError') return 'the request timed out';
  return "the server didn't answer";
};

export function createActions(store, deps = {}) {
  const now = deps.now || (() => Date.now());
  const live = deps.live || { refresh: async () => {} };
  const pushApi = { ...pushLib, ...(deps.push || {}) };
  const sseApi = deps.streamSSE || streamSSE;
  // Wrapped: browsers throw "Illegal invocation" when timer functions are called as methods of another object.
  const timers = deps.timers || { setInterval: (fn, ms) => setInterval(fn, ms), clearInterval: (t) => clearInterval(t) };
  let rerunWatch = null;
  let mediaFollowing = null; // the media-update action id this page is following
  const mediaSeen = new Set(); // media-update ids already followed here: never re-followed

  const toast = (tone, text, icon) => store.dispatch({ type: 'toast', tone, text, icon });
  const findApp = (name) => (store.getState().summary?.apps || []).map(normalizeApp).find((a) => a.name === name);
  const label = (name) => findApp(name)?.label || APP_META[name]?.label || name;
  const setDialog = (dialog) => store.dispatch({ type: 'dialog', dialog });
  const patchDialog = (patch) => store.dispatch({ type: 'dialog/patch', patch });
  const dialogIs = (kind, app) => {
    const d = store.getState().dialog;
    return d && d.kind === kind && (app === undefined || d.app === app);
  };

  // Follow /api/actions/:id: `step` events ({step, at}) then one `end` ({state: done|failed, error, detail}).
  // Resolves {verdict: 'done'|'failed'|'ended', error, detail}; 'ended' = closed without a verdict.
  // onStep gets the STEP_INDEX position; onRaw gets every step object as sent (a media update's per-app
  // steps carry `app`, and those never decide the verdict: only the action's own done/failed/end do).
  async function followAction(id, kind, { onStep, onRaw } = {}) {
    let verdict = 'ended';
    let error = null;
    let detail = null;
    try {
      await sseApi(`/api/actions/${encodeURIComponent(id)}`, {
        onEvent(ev) {
          if (ev.type === 'comment') return;
          const data = parseJSON(ev.data) || {};
          if (ev.type === 'end') {
            verdict = data.state === 'done' ? 'done' : 'failed';
            error = data.error || null;
            detail = data.detail || null;
            return;
          }
          const step = String(data.step ?? '').toLowerCase();
          onRaw?.(data);
          if (data.app != null && kind === 'media-update') return;
          if (step === 'done') verdict = 'done';
          else if (step === 'failed') verdict = 'failed';
          else if (STEP_INDEX[kind]?.[step] !== undefined) onStep?.(STEP_INDEX[kind][step]);
        },
      });
    } catch (err) {
      if (verdict === 'ended') error = errText(err);
    }
    return { verdict, error, detail };
  }

  const actionId = (res) => res?.action?.id ?? res?.actionId ?? null;

  function finishPending(name, outcome, p) {
    store.dispatch({ type: 'pending/clear', name });
    const L = label(name);
    if (p.kind === 'update') {
      if (dialogIs('update', name)) setDialog(null);
      if (outcome === 'healthy') toast('ok', `${L} updated to ${p.target} and healthy`);
      else if (outcome === 'unhealthy') toast('warn', `${L} updated to ${p.target} but reports unhealthy`);
      else if (outcome === 'timeout') toast('warn', `${L} hasn't reported healthy on ${p.target} yet`);
      else toast('fail', `Update failed: ${L} didn't come back up`);
      return;
    }
    if (p.kind === 'stop') {
      if (outcome === 'timeout') toast('warn', `${L} hasn't stopped yet`);
      return;
    }
    if (outcome === 'healthy') toast('ok', `${L} is healthy again`);
    else if (outcome === 'unhealthy') toast('warn', `${L} is running but reports unhealthy`);
    else if (outcome === 'failed') toast('fail', `${L} didn't come back up`);
    else toast('warn', `${L} hasn't reported healthy yet`);
  }

  function checkPending() {
    const st = store.getState();
    for (const [name, p] of Object.entries(st.pending)) {
      if (p.streaming) continue; // the action stream decides
      const out = pendingOutcome(p, findApp(name), now());
      if (out !== 'wait') finishPending(name, out, p);
    }
    if (rerunWatch) checkRerun();
  }

  function checkRerun() {
    const st = store.getState();
    const w = rerunWatch;
    if (!w || w.streaming) return;
    if (gateStatus(st.summary) === 'ok') endRerun('pass');
    else if (now() - w.since > 120000) endRerun('timeout');
  }

  function endRerun(result, text) {
    rerunWatch = null;
    if (dialogIs('rerun')) setDialog(null);
    const st = store.getState();
    const mount = st.summary?.gate?.mountPath || '/media';
    if (result === 'pass') {
      toast('ok', text || `Check passed. Drive mounted at ${mount}.`);
      return;
    }
    if (text) {
      toast(result === 'timeout' ? 'warn' : 'fail', text);
      return;
    }
    const next = st.summary?.gate?.nextRetryAt;
    const secs = next ? (new Date(next).getTime() - now()) / 1000 : NaN;
    const when = Number.isFinite(secs) && secs > 0 ? ` Next automatic retry in ${secs >= 120 ? `${Math.round(secs / 60)} min` : fmtCountdown(secs)}.` : '';
    toast(result === 'timeout' ? 'warn' : 'fail', result === 'timeout' ? `The check hasn't reported back yet.${when}` : `Check failed.${when}`);
  }

  // A media update this page isn't following yet (started on another device, or before a reload): show it.
  function checkMediaRunning() {
    const st = store.getState();
    const id = st.summary?.hostActions?.mediaUpdate?.running;
    if (!id || typeof id !== 'string' || mediaFollowing || mediaSeen.has(id) || st.dialog) return;
    followMedia(id, []);
  }

  async function followMedia(id, rows) {
    mediaFollowing = id;
    mediaSeen.add(id);
    setDialog({ kind: 'media-update', phase: 'progress', busy: false, step: 0, actionId: id, progress: rows });
    let progress = rows;
    const { verdict, error, detail } = await followAction(id, 'media-update', {
      onRaw: (data) => {
        progress = applyMediaStep(progress, data, label);
        if (dialogIs('media-update') && store.getState().dialog.actionId === id) patchDialog({ progress });
      },
    });
    mediaFollowing = null;
    if (dialogIs('media-update') && store.getState().dialog.actionId === id) setDialog(null);
    if (verdict === 'done') toast('ok', detail || `Updated ${plural(progress.filter((r) => r.state === 'done').length, 'app')}`);
    else if (verdict === 'failed') toast('fail', error || 'The media update failed');
    else toast('warn', error ? `Lost the update's progress: ${error}` : "Lost the update's progress. It keeps running on the server.");
    live.refresh();
  }

  async function checkMediaUpdates(refresh = false) {
    const d = store.getState().dialog;
    if (d && !(d.kind === 'media-update' && d.phase === 'review')) return;
    if (d?.loading || d?.busy) return; // one check at a time (double taps)
    if (d) patchDialog({ loading: true, error: null });
    else setDialog({ kind: 'media-update', phase: 'review', busy: false, step: 0, loading: true, error: null, check: null, selected: {} });
    try {
      const { data } = await getJSON(`/api/media/updates${refresh ? '?refresh=1' : ''}`, { timeoutMs: 60000 });
      if (!dialogIs('media-update') || store.getState().dialog.phase !== 'review') return;
      patchDialog({ loading: false, error: null, check: data, selected: defaultSelection(data) });
    } catch (err) {
      if (dialogIs('media-update') && store.getState().dialog.phase === 'review') patchDialog({ loading: false, error: errText(err) });
    }
  }

  store.subscribe((state, prev) => {
    if (state.summary !== prev.summary) {
      checkPending();
      checkMediaRunning();
    }
    if (!state.dialog && prev.dialog) checkMediaRunning();
  });
  const sweep = timers.setInterval(checkPending, 5000);

  // restart | start: the action stream says when it's healthy (or why not); the summary is the fallback.
  async function restartLike(name, kind) {
    const before = findApp(name);
    const pend = { kind, since: now(), startedAtBefore: before?.startedAt ?? null, streaming: false };
    store.dispatch({ type: 'pending/set', name, value: pend });
    let res;
    try {
      res = await post(appPath(name, kind));
    } catch (err) {
      store.dispatch({ type: 'pending/clear', name });
      toast('fail', `Couldn't ${kind} ${label(name)}: ${errText(err)}`);
      return false;
    }
    live.refresh();
    const id = actionId(res);
    if (!id) return true;
    store.dispatch({ type: 'pending/set', name, value: { ...pend, streaming: true } });
    const { verdict, error, detail } = await followAction(id, kind);
    if (verdict === 'ended') {
      store.dispatch({ type: 'pending/set', name, value: { ...pend, streaming: false } });
      return true;
    }
    store.dispatch({ type: 'pending/clear', name });
    if (verdict === 'done') toast('ok', detail || `${label(name)} is healthy again`);
    else toast('fail', error || `${label(name)} didn't come back up`);
    live.refresh();
    return verdict === 'done';
  }

  async function refreshDevices() {
    try {
      const { data } = await getJSON('/api/push/devices');
      const list = Array.isArray(data) ? data : data?.devices;
      if (Array.isArray(list)) store.dispatch({ type: 'devices', devices: list });
    } catch {
      // the summary's push.devices stays the fallback
    }
  }

  async function refreshMe() {
    store.dispatch({ type: 'me', id: await pushApi.currentDeviceId() });
  }

  return {
    toast,
    dismissToast: (id) => store.dispatch({ type: 'toast/clear', id }),
    navigate: (route) => store.dispatch({ type: 'route', route }),

    restart(name) {
      toast('info', `Restarting ${label(name)}…`, 'arrow-clockwise');
      return restartLike(name, 'restart');
    },

    ask(kind, payload = {}) {
      setDialog({ kind, phase: 'confirm', busy: false, step: 0, ...payload });
    },

    cancelDialog() {
      const d = store.getState().dialog;
      if (d && d.phase !== 'progress' && !d.busy) setDialog(null);
    },

    checkMediaUpdates,

    toggleMediaApp(name, on) {
      const d = store.getState().dialog;
      if (!d || d.kind !== 'media-update' || d.phase !== 'review' || d.busy) return;
      patchDialog({ selected: { ...(d.selected || {}), [name]: Boolean(on) } });
    },

    // The dialog's second button (content.secondary.key).
    secondary(key) {
      if (key === 'recheck') return checkMediaUpdates(true);
      return undefined;
    },

    async confirm() {
      const d = store.getState().dialog;
      if (!d || d.busy) return;
      if (d.kind === 'media-update' ? d.phase !== 'review' || d.loading : d.phase !== 'confirm') return;
      const L = label(d.app);
      switch (d.kind) {
        case 'reboot': {
          patchDialog({ busy: true });
          let res;
          try {
            res = await post('/api/host/reboot');
          } catch (err) {
            setDialog(null);
            toast('fail', `Couldn't restart the host: ${errText(err)}`);
            return;
          }
          const id = actionId(res);
          const { verdict, error, detail } = id ? await followAction(id, 'reboot') : { verdict: 'done', detail: res?.detail || null };
          setDialog(null);
          if (verdict === 'failed') toast('fail', `Couldn't restart the host${error ? `: ${error.replace(/\.$/, '')}` : ''}`);
          else toast('ok', detail || 'Restarting in a few seconds. The dashboard is back in a few minutes.', 'arrows-clockwise');
          live.refresh();
          return;
        }
        case 'media-update': {
          const names = selectedApps(d);
          if (!names.length) return;
          patchDialog({ busy: true, error: null });
          let res;
          try {
            res = await post('/api/media/update', { apps: names });
          } catch (err) {
            // stays on the review list: "Check again" is the way out of a stale check (409)
            if (dialogIs('media-update')) patchDialog({ busy: false });
            toast('fail', `Couldn't start the update: ${errText(err)}`);
            return;
          }
          live.refresh();
          const id = actionId(res);
          const byName = Object.fromEntries(mediaCheckApps(d.check).map((a) => [a.name, a]));
          const rows = names.map((n, i) => ({ name: n, label: byName[n]?.label || label(n), from: byName[n]?.current || '', to: byName[n]?.latest || '', state: 'waiting', step: null, rolledBack: false, index: i + 1 }));
          if (!id) {
            setDialog(null);
            toast('info', `Updating ${plural(names.length, 'app')}…`, 'arrow-circle-up');
            return;
          }
          await followMedia(id, rows);
          return;
        }
        case 'stop': {
          patchDialog({ busy: true });
          const before = findApp(d.app);
          try {
            const res = await post(appPath(d.app, 'stop'));
            setDialog(null);
            store.dispatch({ type: 'pending/set', name: d.app, value: { kind: 'stop', since: now(), startedAtBefore: before?.startedAt ?? null, streaming: true } });
            live.refresh();
            const id = actionId(res);
            const { verdict, error, detail } = id ? await followAction(id, 'stop') : { verdict: 'done', detail: null };
            store.dispatch({ type: 'pending/clear', name: d.app });
            if (verdict === 'failed') toast('fail', error || `Couldn't stop ${L}`);
            else toast('ok', detail || `${L} stopped. It stays down until you start it.`);
            live.refresh();
          } catch (err) {
            patchDialog({ busy: false });
            setDialog(null);
            toast('fail', `Couldn't stop ${L}: ${errText(err)}`);
          }
          return;
        }
        case 'start': {
          setDialog(null);
          toast('info', `Starting ${L}…`, 'play');
          await restartLike(d.app, 'start');
          return;
        }
        case 'remove': {
          patchDialog({ busy: true });
          try {
            const res = await del(`/api/push/devices/${encodeURIComponent(d.deviceId)}`);
            if (Array.isArray(res?.devices)) store.dispatch({ type: 'devices', devices: res.devices });
            if (d.deviceId && d.deviceId === store.getState().meId) {
              await pushApi.forgetLocalSubscription();
              store.dispatch({ type: 'me', id: null });
            }
            setDialog(null);
            toast('ok', 'Device removed. It will stop getting pushes.');
            refreshDevices();
            live.refresh();
          } catch (err) {
            setDialog(null);
            toast('fail', `Couldn't remove the device: ${errText(err)}`);
          }
          return;
        }
        case 'update': {
          const before = findApp(d.app);
          const target = d.target || before?.pinnedTag || '';
          patchDialog({ phase: 'progress', step: 0 });
          const pend = { kind: 'update', since: now(), startedAtBefore: before?.startedAt ?? null, target, streaming: false };
          store.dispatch({ type: 'pending/set', name: d.app, value: pend });
          let res;
          try {
            res = await post(appPath(d.app, 'update'));
          } catch (err) {
            store.dispatch({ type: 'pending/clear', name: d.app });
            setDialog(null);
            toast('fail', `Update failed: ${errText(err)}`);
            return;
          }
          live.refresh();
          const id = actionId(res);
          if (!id) return; // no progress stream: the summary decides (checkPending)
          store.dispatch({ type: 'pending/set', name: d.app, value: { ...pend, streaming: true } });
          const { verdict, error } = await followAction(id, 'update', {
            onStep: (step) => dialogIs('update', d.app) && patchDialog({ step }),
          });
          if (verdict === 'ended') {
            store.dispatch({ type: 'pending/set', name: d.app, value: { ...pend, streaming: false } });
            return;
          }
          store.dispatch({ type: 'pending/clear', name: d.app });
          if (dialogIs('update', d.app)) setDialog(null);
          if (verdict === 'done') toast('ok', `${L} updated to ${target} and healthy`);
          else toast('fail', `Update failed${error ? `: ${error.replace(/\.$/, '')}` : ''}`);
          live.refresh();
          return;
        }
        case 'rerun': {
          patchDialog({ phase: 'progress', step: 0 });
          rerunWatch = { since: now(), streaming: false };
          let res;
          try {
            res = await post('/api/gate/rerun');
          } catch (err) {
            rerunWatch = null;
            setDialog(null);
            toast('fail', `Couldn't start the check: ${errText(err)}`);
            return;
          }
          live.refresh();
          const id = actionId(res);
          if (!id) return; // the summary decides (checkRerun)
          rerunWatch.streaming = true;
          const { verdict, error, detail } = await followAction(id, 'rerun', {
            onStep: (step) => dialogIs('rerun') && patchDialog({ step }),
          });
          if (verdict === 'done') endRerun('pass', detail);
          else if (verdict === 'failed') endRerun('fail', error);
          else if (rerunWatch) rerunWatch.streaming = false;
          live.refresh();
          return;
        }
        default:
          setDialog(null);
      }
    },

    async sendTest() {
      try {
        const r = await post('/api/test');
        if (!r.total) toast('warn', 'No devices are subscribed yet.');
        else toast('ok', `Test push sent to ${plural(r.sent, 'device')}${r.failed ? ` (${r.failed} failed)` : ''}`, 'paper-plane-tilt');
        if (Array.isArray(r.devices)) store.dispatch({ type: 'devices', devices: r.devices });
      } catch (err) {
        if (err.status === 429) toast('warn', 'Wait a few seconds before sending another test.');
        else if (err.status === 502 && err.data) toast('fail', `Delivery failed for all ${plural(err.data.total, 'device')}.`);
        else toast('fail', `Couldn't send the test: ${errText(err)}`);
      }
    },

    async subscribe(deviceLabel) {
      try {
        const r = await pushApi.subscribeThisDevice(deviceLabel);
        store.dispatch({ type: 'me', id: r.id });
        if (Array.isArray(r.devices)) store.dispatch({ type: 'devices', devices: r.devices });
        toast('ok', r.created ? 'This device will get alerts' : 'Subscription updated');
        refreshDevices();
        return true;
      } catch (err) {
        toast('fail', err instanceof ApiError ? `Couldn't subscribe: ${errText(err)}` : err.message || "Couldn't subscribe");
        return false;
      }
    },

    refreshDevices,
    refreshMe,

    async loadSettings() {
      try {
        const { data } = await getJSON('/api/settings');
        store.dispatch({ type: 'settings', settings: data });
      } catch (err) {
        store.dispatch({ type: 'settings/error', error: errText(err) });
      }
    },

    async saveSettings(patch, okText = 'Saved') {
      try {
        const data = await put('/api/settings', patch);
        store.dispatch({ type: 'settings', settings: data && typeof data === 'object' && !Array.isArray(data) ? data : store.getState().settings });
        toast('ok', okText);
        live.refresh();
        return true;
      } catch (err) {
        toast('fail', `Couldn't save: ${errText(err)}`);
        return false;
      }
    },

    refresh: () => live.refresh(),

    dispose() {
      timers.clearInterval(sweep);
    },
  };
}
