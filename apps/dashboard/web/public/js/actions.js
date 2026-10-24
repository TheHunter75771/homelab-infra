// User actions: app restart/stop/start/update, gate re-run, push devices, test push, settings.
// Each one drives the dialog/toast/pending state in the store and confirms the outcome from the
// summary (or the action's progress stream when the server gives one).

import { post, del, put, getJSON, streamSSE, parseJSON, appPath, ApiError } from './api.js';
import { normalizeApp, pendingOutcome, APP_META, gateStatus } from './model.js';
import { shortImage, fmtCountdown, plural } from './format.js';
import * as pushLib from './push.js';

// Server step names (apps/dashboard/server, "Action steps") -> index in the dialog's step list.
// The gate re-run can only see the trigger being picked up and the retry service finishing, so its list
// is the server's two steps: requested -> running, then done | failed.
export const STEP_INDEX = {
  update: { pulling: 0, recreating: 1, 'waiting-health': 2, done: 3 },
  rerun: { requested: 0, running: 1, done: 2 },
};

// What a dialog says. `app` is a derived app (deriveApps), `device` a {id,label}.
export function dialogContent(dialog, { app, gate } = {}) {
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
  async function followAction(id, kind, { onStep } = {}) {
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

  store.subscribe((state, prev) => {
    if (state.summary !== prev.summary) checkPending();
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

    async confirm() {
      const d = store.getState().dialog;
      if (!d || d.busy || d.phase !== 'confirm') return;
      const L = label(d.app);
      switch (d.kind) {
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
