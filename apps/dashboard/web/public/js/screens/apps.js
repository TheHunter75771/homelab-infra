import { html, Icon, Chip, Btn, Reason, hrefFor, useId } from '../ui.js';
import { fmtUptime, shortImage, plural } from '../format.js';
import { openHref, DOCKER_DOWN_UNKNOWN } from '../model.js';

export const HELD_REASON = 'Held by the boot gate until the media drive is back.';
export const TAILNET_ONLY = 'Only available on the tailnet';

// Lock reasons that the status chip already explains; they don't get a separate reason line.
const SELF_EVIDENT = new Set(['Busy', 'Stopped. Start it instead', 'Already stopped', 'Already running', 'Already on the pinned version']);

// Buttons for one app plus the visible reason lines for the disabled ones. The server's `locks` decide;
// the fallbacks below only apply to a server that sends none.
export function appActions(app, { paused, remote, docker }) {
  const held = app.statusKey === 'held';
  const stopped = app.statusKey === 'stopped' || (app.state && app.state !== 'running' && app.statusKey !== 'held');
  const busy = app.statusKey === 'starting' || app.statusKey === 'updating';
  const L = app.locks || {};
  const has = (k) => Object.prototype.hasOwnProperty.call(L, k);
  // Docker down, gate not holding it: the apps may still be serving, but nothing can act on them.
  const unknownDocker = app.stopKind === 'docker-down' ? `${DOCKER_DOWN_UNKNOWN}.` : null;
  const dockerDown = unknownDocker || (docker && docker !== 'active' && !held ? "Docker isn't running." : null);
  const blocked = (held ? HELD_REASON : null) || dockerDown;
  const lock = (k, fallback) => paused || unknownDocker || (has(k) ? L[k] : fallback);
  const list = [];
  const href = openHref(app.open);
  if (href) list.push({ key: 'open', label: 'Open', icon: 'arrow-square-out', href, newTab: true });
  list.push({ key: 'restart', label: 'Restart', icon: 'arrow-clockwise', reason: lock('restart', blocked || (stopped ? 'Stopped. Start it instead' : busy ? 'Busy' : null)) });
  if (stopped) list.push({ key: 'start', label: 'Start', icon: 'play', reason: lock('start', blocked || (busy ? 'Busy' : null)) });
  else {
    list.push({
      key: 'stop',
      label: 'Stop',
      icon: 'stop',
      reason: lock('stop', blocked || (app.name === 'dashboard' ? "The dashboard can't stop itself" : busy ? 'Busy' : null)),
    });
  }
  list.push({ key: 'logs', label: 'Logs', icon: 'scroll', href: hrefFor({ name: 'app', id: app.name }) });
  if (app.next && app.statusKey !== 'updating') {
    const reason = remote ? TAILNET_ONLY : lock('update', blocked || (busy ? 'Busy' : null));
    list.push({ key: 'update', label: `Update to ${app.next}`, icon: 'arrow-circle-up', primary: true, lock: reason === TAILNET_ONLY, reason });
  }
  const notes = [];
  for (const b of list) {
    if (!b.reason || SELF_EVIDENT.has(b.reason)) continue;
    notes.push(b.key === 'update' && b.reason === TAILNET_ONLY ? 'Update is only available on the tailnet.' : b.reason);
  }
  return { list, notes: [...new Set(notes.map((n) => (/[.!]$/.test(n) ? n : `${n}.`)))] };
}

export function ActionRow({ app, ctx, actions }) {
  const noteId = useId('note');
  const { list, notes } = appActions(app, ctx);
  const run = (key) => {
    if (key === 'restart') actions.restart(app.name);
    else if (key === 'stop') actions.ask('stop', { app: app.name });
    else if (key === 'start') actions.ask('start', { app: app.name });
    else if (key === 'update') actions.ask('update', { app: app.name, target: app.next });
  };
  return html`<div class="actions">
      ${list.map(
        (b) => html`<${Btn}
          key=${b.key}
          icon=${b.icon}
          label=${b.label}
          href=${b.href}
          newTab=${b.newTab}
          primary=${b.primary}
          lock=${b.lock}
          reason=${b.reason}
          describedBy=${b.reason && notes.length ? noteId : undefined}
          onClick=${() => run(b.key)}
        />`,
      )}
    </div>
    ${notes.length > 0 && html`<div id=${noteId}>${notes.map((n) => html`<${Reason} text=${n} />`)}</div>`}`;
}

export function StatusChip({ app }) {
  return html`<${Chip} tone=${app.st.tone} icon=${app.st.icon} label=${app.st.label} spin=${app.st.spin} />`;
}

export function AppCard({ app, ctx, actions }) {
  return html`<article class="app-card" aria-label=${app.label}>
    <div class="app-head">
      <a class="app-name" href=${hrefFor({ name: 'app', id: app.name })}><b>${app.label}</b><span>${app.desc}</span></a>
      <${StatusChip} app=${app} />
    </div>
    <div class="app-meta">
      <span>${app.uptimeSeconds !== null ? `up ${fmtUptime(app.uptimeSeconds)}` : 'not running'}</span>
      ${app.restartCount !== null && html`<span>${plural(app.restartCount, 'restart')}</span>`}
      <span>${shortImage(app.image || app.pinned)}</span>
    </div>
    ${app.next &&
    html`<span class="app-next"><${Icon} name="arrow-circle-up" />Pinned version ${app.next} is newer than the running image</span>`}
    <${ActionRow} app=${app} ctx=${ctx} actions=${actions} />
  </article>`;
}

export function Apps({ view, ctx, actions }) {
  if (!view.apps.length) {
    return html`<div class="card"><span class="empty">No containers reported yet. The list comes from the host's Docker helper.</span></div>`;
  }
  return html`<div class="app-grid">${view.apps.map((a) => html`<${AppCard} key=${a.name} app=${a} ctx=${ctx} actions=${actions} />`)}</div>`;
}
