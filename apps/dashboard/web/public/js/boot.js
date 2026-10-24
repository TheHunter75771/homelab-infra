import { render } from '../vendor/preact.mjs';
import { html } from './ui.js';
import { App } from './app.js';
import { createStore, initialState } from './store.js';
import { createLive } from './live.js';
import { createActions } from './actions.js';
import { parseHash } from './route.js';

export function boot({ root = document.getElementById('app'), registerWorker = true } = {}) {
  const store = createStore(undefined, { ...initialState, route: parseHash(location.hash) });
  const live = createLive(store);
  const actions = createActions(store, { live });
  const onHash = () => {
    store.dispatch({ type: 'route', route: parseHash(location.hash) });
    window.scrollTo?.(0, 0);
  };
  window.addEventListener('hashchange', onHash);
  render(html`<${App} store=${store} actions=${actions} />`, root);
  live.start();
  // A push click on an open dashboard window: the worker asks it to switch screens (hash routes only).
  const onWorkerMessage = (e) => {
    const hash = e.data && e.data.type === 'open-route' ? e.data.hash : null;
    if (typeof hash === 'string' && /^#\/[\w/%-]*$/.test(hash)) location.hash = hash;
  };
  navigator.serviceWorker?.addEventListener?.('message', onWorkerMessage);
  if (registerWorker && 'serviceWorker' in navigator && window.isSecureContext) {
    navigator.serviceWorker
      .register('/sw.js', { scope: '/' })
      .then((reg) => {
        actions.refreshMe();
        watchForUpdates(reg, store);
      })
      .catch((err) => console.warn('service worker registration failed', err));
  }
  return {
    store,
    live,
    actions,
    stop() {
      live.stop();
      actions.dispose();
      window.removeEventListener('hashchange', onHash);
      navigator.serviceWorker?.removeEventListener?.('message', onWorkerMessage);
      render(null, root);
    },
  };
}

// A new shell version installs in the background and waits. This page lets it take over only when the
// page is idle (no dialog open, no field being edited), then reloads once, so a page never runs a mix of
// two versions. Checks for an update whenever the app comes back to the foreground.
export function watchForUpdates(reg, store, { sw = navigator.serviceWorker, doc = document, reload = () => location.reload(), retryMs = 5000 } = {}) {
  let controlled = Boolean(sw.controller);
  let reloading = false;
  sw.addEventListener('controllerchange', () => {
    if (controlled && !reloading) {
      reloading = true;
      reload();
    }
    controlled = true; // the first install claiming this page isn't a version change
  });
  const busy = () => Boolean(store.getState().dialog) || /^(INPUT|TEXTAREA|SELECT)$/.test(doc.activeElement?.tagName || '');
  const takeOver = (worker) => {
    const attempt = () => {
      if (worker.state !== 'installed') return;
      if (busy()) setTimeout(attempt, retryMs);
      else worker.postMessage({ type: 'skip-waiting' });
    };
    attempt();
  };
  if (reg.waiting && sw.controller) takeOver(reg.waiting);
  reg.addEventListener('updatefound', () => {
    const worker = reg.installing;
    worker?.addEventListener('statechange', () => {
      if (worker.state === 'installed' && sw.controller) takeOver(worker);
    });
  });
  doc.addEventListener('visibilitychange', () => {
    if (doc.visibilityState === 'visible') reg.update().catch(() => {});
  });
}
