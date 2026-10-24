import { html, Icon, Btn, Dot, useNow, useMedia, DESKTOP_QUERY, hrefFor } from './ui.js';
import { useState, useEffect, useMemo, useRef } from '../vendor/hooks.mjs';
import { buildView } from './model.js';
import { actionsPausedReason, serverStale } from './store.js';
import { parentOf, titleOf } from './route.js';
import { dialogContent } from './actions.js';
import { fmtClock, fmtDayTime, fmtAgo, daysBetween } from './format.js';
import { Overview, LoadingSkeleton } from './screens/overview.js';
import { Apps } from './screens/apps.js';
import { AppDetail } from './screens/app-detail.js';
import { Drive } from './screens/drive.js';
import { Media } from './screens/media.js';
import { Alerts } from './screens/alerts.js';
import { Uptime } from './screens/uptime.js';
import { Settings } from './screens/settings.js';
import { More } from './screens/more.js';
import { Offline } from './screens/offline.js';

export const TOAST_MS = 3800;

export function useStore(store) {
  const [state, setState] = useState(store.getState());
  useEffect(() => {
    const off = store.subscribe((s) => setState(s));
    setState(store.getState());
    return off;
  }, [store]);
  return state;
}

// The dot's shape differs too (circle = failure, diamond = warning), and it has a text label.
const dotLabel = (tone) => (tone === 'fail' ? 'failure' : 'warning');

function LiveIndicator({ state }) {
  const now = useNow(1000);
  let tone = 'info';
  let label = 'Connecting';
  let ago = '';
  if (state.connection === 'live' && serverStale(state, now)) {
    // connected, but the server's own refreshes have stopped: the data is old even though pings arrive
    tone = 'warn';
    label = 'Stale';
    ago = `· updated ${fmtAgo(state.serverAtSeenAt, now)}`;
  } else if (state.connection === 'live') {
    tone = 'ok';
    label = 'Live';
    ago = `· ${Math.max(0, Math.round((now - (state.receivedAt || now)) / 1000))} s ago`;
  } else if (state.connection === 'stale' || state.connection === 'offline') {
    tone = 'warn';
    label = 'Stale';
    ago = state.dataAt ? `· updated ${daysBetween(state.dataAt, now) === 0 ? fmtClock(state.dataAt) : fmtDayTime(state.dataAt)}` : '';
  }
  return html`<span class=${`live-dot tone-${tone}`}></span><span class=${`tc tone-${tone}`}>${label}</span>${ago && html`<span>${ago}</span>`}`;
}

function AccessBadge({ access }) {
  if (access === 'remote') {
    return html`<span class="access remote" title="Through Cloudflare Access. Update and Re-run gate are tailnet only."><${Icon} name="globe-simple" />Remote</span>`;
  }
  return html`<span class="access" title="Full access"><${Icon} name="shield-check" />Tailnet</span>`;
}

function Header({ state, view, route, desktop, titleRef }) {
  const parent = parentOf(route, !desktop);
  const app = route.name === 'app' && view ? view.apps.find((a) => a.name === route.id) : null;
  const title = titleOf(route, app?.label);
  return html`<header class="header">
    ${parent && html`<a class="back" href=${hrefFor(parent)} aria-label="Back"><${Icon} name="caret-left" /></a>`}
    <div class="header-text">
      <h1 class="title" tabindex="-1" ref=${titleRef}>${title}</h1>
      <span class="meta"><span class="host">${view?.hostName || 'homelab'}</span><span aria-hidden="true">·</span><${LiveIndicator} state=${state} /></span>
    </div>
    <${AccessBadge} access=${view?.access} />
  </header>`;
}

const NAV = [
  { name: 'overview', label: 'Overview', icon: 'gauge' },
  { name: 'apps', label: 'Apps', icon: 'squares-four', match: ['apps', 'app'], dot: 'apps' },
  { name: 'drive', label: 'Drive & Gate', icon: 'hard-drive', dot: 'drive' },
  { name: 'media', label: 'Media', icon: 'film-strip', dot: 'media' },
  { name: 'alerts', label: 'Alerts', icon: 'bell', dot: 'alerts' },
  { name: 'uptime', label: 'Uptime', icon: 'pulse' },
  { name: 'settings', label: 'Settings', icon: 'gear-six' },
];
const TABS = [
  { name: 'overview', label: 'Overview', icon: 'gauge' },
  { name: 'apps', label: 'Apps', icon: 'squares-four', match: ['apps', 'app'], dot: 'apps' },
  { name: 'media', label: 'Media', icon: 'film-strip', dot: 'media' },
  { name: 'alerts', label: 'Alerts', icon: 'bell', dot: 'alerts' },
  { name: 'more', label: 'More', icon: 'dots-three-outline', match: ['more', 'drive', 'uptime', 'settings'], dot: 'more' },
];
const isActive = (item, route) => (item.match || [item.name]).includes(route.name);

function Sidebar({ view, route }) {
  return html`<aside class="sidebar" aria-label="Sections">
    <div class="brand">
      <div class="brand-mark"><${Icon} name="hard-drives" /></div>
      <div class="brand-text"><b>${view?.hostName || 'homelab'}</b><span>Homelab dashboard</span></div>
    </div>
    <nav aria-label="Main">
      ${NAV.map((n) => {
        const tone = view && n.dot ? view.dots[n.dot] : null;
        return html`<a class="nav-item" href=${hrefFor({ name: n.name })} aria-current=${isActive(n, route) ? 'page' : undefined}>
          <${Icon} name=${n.icon} /><span class="nav-label">${n.label}</span>${tone && html`<${Dot} tone=${tone} label=${dotLabel(tone)} />`}
        </a>`;
      })}
    </nav>
    <div class="sidebar-foot"><span class="mono">dashboard ${view?.version || '2.0.0'}</span><span>Served at / on this origin</span></div>
  </aside>`;
}

function TabBar({ view, route }) {
  return html`<nav class="tabbar" aria-label="Main">
    ${TABS.map((n) => {
      const tone = view && n.dot ? view.dots[n.dot] : null;
      return html`<a class="tab" href=${hrefFor({ name: n.name })} aria-current=${isActive(n, route) ? 'page' : undefined}>
        <${Icon} name=${n.icon} />${n.label}${tone && html`<${Dot} cls="tab-dot" tone=${tone} label=${dotLabel(tone)} />`}
      </a>`;
    })}
  </nav>`;
}

function Toast({ toast, actions }) {
  useEffect(() => {
    if (!toast) return undefined;
    const t = setTimeout(() => actions.dismissToast(toast.id), TOAST_MS);
    return () => clearTimeout(t);
  }, [toast?.id]);
  const icon = toast?.icon || { ok: 'check-circle', warn: 'warning', fail: 'x-circle', info: 'info' }[toast?.tone] || 'info';
  return html`<div class="toast-wrap" role="status" aria-live="polite">
    ${toast && html`<div class=${`toast tone-${toast.tone}`} key=${toast.id}><${Icon} name=${icon} /><span>${toast.text}</span></div>`}
  </div>`;
}

function Dialog({ dialog, view, actions }) {
  const ref = useRef(null);
  const opener = useRef(null);
  const open = Boolean(dialog);
  // a[href], not [href]: the icons' <use href> must not count as focus stops.
  const focusables = () => [...(ref.current?.querySelectorAll('a[href], button:not([disabled]), input:not([disabled])') || [])];
  useEffect(() => {
    if (!open) return undefined;
    opener.current = document.activeElement;
    const el = ref.current;
    (focusables().find((b) => b.dataset.autofocus) || focusables()[0] || el)?.focus?.();
    // Focus stays inside through every phase. While steps run (or a request is busy) nothing in the dialog
    // is focusable, so the dialog itself holds focus.
    const onKey = (e) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        actions.cancelDialog();
      } else if (e.key === 'Tab') {
        const box = ref.current;
        const f = focusables();
        if (!f.length) {
          e.preventDefault();
          box?.focus?.();
          return;
        }
        const first = f[0];
        const last = f[f.length - 1];
        const inside = box?.contains?.(document.activeElement);
        if (!inside || document.activeElement === box) {
          e.preventDefault();
          (e.shiftKey ? last : first).focus();
        } else if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('keydown', onKey);
      opener.current?.focus?.({ preventScroll: true });
    };
  }, [open]);
  // Entering the progress phase (or a busy confirm) removes or disables the focused button; keep focus in.
  useEffect(() => {
    if (!open) return;
    const box = ref.current;
    if (box && !box.contains(document.activeElement)) (focusables()[0] || box).focus?.();
  }, [open, dialog?.phase, dialog?.busy]);
  if (!dialog) return null;
  const app = view?.apps.find((a) => a.name === dialog.app);
  const c = dialogContent(dialog, { app, gate: view?.gate });
  if (!c) return null;
  const progress = dialog.phase === 'progress';
  return html`<div class="backdrop" onClick=${(e) => e.target === e.currentTarget && actions.cancelDialog()}>
    <div class=${`dialog tone-${c.tone}`} role="dialog" aria-modal="true" aria-labelledby="dlg-title" aria-describedby="dlg-body" ref=${ref} tabindex="-1">
      <div class="dialog-head"><${Icon} name=${c.icon} /><h2 id="dlg-title">${c.title}</h2></div>
      <p class="dialog-body" id="dlg-body">${c.body}</p>
      ${progress &&
      html`<div class="steps" role="list" aria-live="polite">
        ${(c.steps || []).map((label, i) => {
          const done = i < dialog.step;
          const cur = i === dialog.step;
          return html`<div class=${`step${done ? ' done' : cur ? ' current' : ''}`} role="listitem">
            <${Icon} name=${done ? 'check-circle' : cur ? 'circle-notch' : 'circle'} spin=${cur} />${label}
            <span class="sr-only">${done ? ' (done)' : cur ? ' (in progress)' : ' (waiting)'}</span>
          </div>`;
        })}
      </div>`}
      ${!progress &&
      html`<div class="dialog-actions">
        <${Btn} label="Cancel" onClick=${() => actions.cancelDialog()} reason=${dialog.busy ? 'Working' : null} />
        <button type="button" class=${`btn toned tone-${c.tone}`} data-autofocus=${c.tone === 'fail' ? undefined : 'true'} disabled=${dialog.busy} onClick=${() => actions.confirm()}>
          ${dialog.busy && html`<${Icon} name="circle-notch" spin />`}${c.confirm}
        </button>
      </div>`}
    </div>
  </div>`;
}

function usePullToRefresh(onRefresh, enabled) {
  const [pull, setPull] = useState(0);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (!enabled) return undefined;
    let startY = null;
    let dist = 0;
    let running = false;
    const start = (e) => {
      if (running || (window.scrollY || 0) > 0 || document.querySelector('.backdrop')) return;
      startY = e.touches[0].clientY;
      dist = 0;
    };
    const move = (e) => {
      if (startY === null) return;
      dist = e.touches[0].clientY - startY;
      setPull(dist > 0 && (window.scrollY || 0) <= 0 ? Math.min(80, dist / 2) : 0);
    };
    const end = async () => {
      if (startY === null) return;
      startY = null;
      if (dist / 2 >= 56) {
        running = true;
        setBusy(true);
        setPull(40);
        await onRefresh();
        running = false;
        setBusy(false);
      }
      setPull(0);
    };
    window.addEventListener('touchstart', start, { passive: true });
    window.addEventListener('touchmove', move, { passive: true });
    window.addEventListener('touchend', end);
    window.addEventListener('touchcancel', end);
    return () => {
      window.removeEventListener('touchstart', start);
      window.removeEventListener('touchmove', move);
      window.removeEventListener('touchend', end);
      window.removeEventListener('touchcancel', end);
    };
  }, [enabled]);
  return { pull, busy };
}

function Screen({ route, view, state, ctx, actions }) {
  const props = { view, state, ctx, actions, route };
  switch (route.name) {
    case 'apps':
      return html`<${Apps} ...${props} />`;
    case 'app':
      return html`<${AppDetail} ...${props} />`;
    case 'drive':
      return html`<${Drive} ...${props} />`;
    case 'media':
      return html`<${Media} ...${props} />`;
    case 'alerts':
      return html`<${Alerts} ...${props} />`;
    case 'uptime':
      return html`<${Uptime} ...${props} />`;
    case 'settings':
      return html`<${Settings} ...${props} />`;
    case 'more':
      return html`<${More} ...${props} />`;
    default:
      return html`<${Overview} ...${props} />`;
  }
}

export function App({ store, actions }) {
  const state = useStore(store);
  const desktop = useMedia(DESKTOP_QUERY);
  const standalone = useMedia('(display-mode: standalone)') || globalThis.navigator?.standalone === true;
  const tick = useNow(15000);
  const route = state.route;
  const view = useMemo(
    () => (state.summary ? buildView(state.summary, { pending: state.pending, now: Date.now() }) : null),
    [state.summary, state.pending, tick],
  );
  const titleRef = useRef(null);
  const firstRoute = useRef(true);
  const ptr = usePullToRefresh(() => actions.refresh(), standalone && !desktop);

  const app = route.name === 'app' && view ? view.apps.find((a) => a.name === route.id) : null;
  const title = titleOf(route, app?.label);
  useEffect(() => {
    document.title = `${title} · Homelab`;
  }, [title]);
  useEffect(() => {
    if (firstRoute.current) {
      firstRoute.current = false;
      return;
    }
    titleRef.current?.focus?.({ preventScroll: true });
  }, [route.name, route.id]);

  if (state.connection === 'offline') {
    return html`<${Offline} view=${view} savedAt=${state.snapshotAt} actions=${actions} />
      <${Toast} toast=${state.toast} actions=${actions} />`;
  }

  const ctx = { paused: actionsPausedReason(state), remote: view?.access === 'remote', docker: view?.docker };
  const loading = !view;
  return html`<div class="shell">
    ${desktop && html`<${Sidebar} view=${view} route=${route} />`}
    <div class="col">
      <div class="top">
        <${Header} state=${state} view=${view} route=${route} desktop=${desktop} titleRef=${titleRef} />
        ${view?.rehearsal &&
        html`<div class="strip rehearsal tone-info" role="note"><${Icon} name="flask" /><span>Rehearsal mode is on. Drill pushes go to test devices only.</span></div>`}
        ${state.connection === 'stale' &&
        html`<div class="strip tone-fail" role="alert"><${Icon} name="plugs" /><span><b>Can't reach the dashboard backend.</b> Showing data from ${
          state.dataAt ? (daysBetween(state.dataAt, Date.now()) === 0 ? fmtClock(state.dataAt) : fmtDayTime(state.dataAt)) : 'earlier'
        }. Actions are paused. Retrying every 10 s.</span></div>`}
      </div>
      <main class="main" id="main">
        ${(ptr.pull > 0 || ptr.busy) &&
        html`<div class="ptr" style=${{ height: `${ptr.pull}px` }} aria-live="polite">
          <${Icon} name=${ptr.busy ? 'circle-notch' : 'arrow-down'} spin=${ptr.busy} />${ptr.busy ? 'Refreshing…' : ptr.pull >= 28 ? 'Release to refresh' : ''}
        </div>`}
        ${loading ? html`<${LoadingSkeleton} />` : html`<${Screen} route=${route} view=${view} state=${state} ctx=${ctx} actions=${actions} />`}
      </main>
    </div>
    ${!desktop && html`<${TabBar} view=${view} route=${route} />`}
    <${Toast} toast=${state.toast} actions=${actions} />
    <${Dialog} dialog=${state.dialog} view=${view} actions=${actions} />
  </div>`;
}
