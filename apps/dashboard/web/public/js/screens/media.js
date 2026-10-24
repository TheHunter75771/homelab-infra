import { html, Icon, Chip, Bar, Btn } from '../ui.js';
import { sessionView, queueItemView, requestView, mediaHealthRows, MEDIA_LABEL, pick } from '../model.js';
import { fmtPlayback, fmtRate, fmtEta, fmtWhen, fmtDate, fmtDaysLeft, fmtClock, toDate } from '../format.js';
import { playingEmptyText, tileMessage } from './overview.js';

// The fail-tinted panel a media tile shows when its app's API doesn't answer (only that tile).
// The server's detail reads like "Timed out after 5 s at 14:19. The container is running, so this may clear
// on its own."; without one, say what we know.
export function tileErrorText(tile, app) {
  if (tile.detail) return tile.detail;
  const since = toDate(tile.since);
  const running = app && ['healthy', 'unhealthy', 'starting'].includes(app.statusKey);
  return `No answer${since ? ` since ${fmtClock(since)}` : ''}.${running ? ' The container is running, so this may clear on its own.' : ''}`;
}

// No force-recheck in v2.0: the server asks every app again every 30 s, and the tile updates by itself.
function TileError({ name, view }) {
  const tile = view.media.tiles[name];
  const app = view.apps.find((a) => a.name === name);
  return html`<div class="err-panel" role="alert">
    <span class="err-title"><${Icon} name="plugs" />${tile.message || `${MEDIA_LABEL[name]} isn't answering`}</span>
    <span class="text2">${tileErrorText(tile, app)}</span>
    <span class="caption"><${Icon} name="arrow-clockwise" /> Rechecks automatically every 30 s.</span>
  </div>`;
}

function NowPlaying({ view, actions }) {
  const t = view.media.tiles.jellyfin;
  const sessions = t.state === 'ok' && Array.isArray(t.sessions) ? t.sessions.map(sessionView) : [];
  return html`<section class="card tight" aria-label="Now playing">
    <span class="label"><${Icon} name="play-circle" />Now playing</span>
    ${t.state === 'error' && html`<${TileError} name="jellyfin" view=${view} />`}
    ${sessions.map(
      (np, i) => html`<div class="np-big" key=${i}>
        <div class="head"><b>${np.title}</b><${Chip} tone=${np.mode.tone} icon=${np.mode.icon} label=${np.mode.label} /></div>
        <span class="who">${[np.user, np.device].filter(Boolean).join(' · ')}${np.paused ? ' · paused' : ''}</span>
        <${Bar} pct=${np.pct} cls="thin" />
        <span class="time">${fmtPlayback(np.positionS, np.durationS)}</span>
      </div>`,
    )}
    ${!sessions.length && t.state !== 'error' && html`<span class="empty">${playingEmptyText(view)}</span>`}
  </section>`;
}

function Downloads({ view, actions }) {
  const t = view.media.tiles.sabnzbd;
  const ok = t.state === 'ok';
  const items = ok && Array.isArray(t.items) ? t.items.map(queueItemView) : [];
  let empty = null;
  if (ok && !items.length) empty = 'Queue is empty.';
  else if (!ok && t.state !== 'error') empty = t.state === 'held' ? 'SABnzbd is held by the gate. The queue resumes when the drive is back.' : tileMessage('sabnzbd', t);
  const more = ok && Number(t.total) > items.length ? Number(t.total) - items.length : 0;
  return html`<section class="card tight" aria-label="Downloads">
    <div class="label-row">
      <span class="label"><${Icon} name="download-simple" />Downloads</span>
      ${ok && t.paused && html`<${Chip} tone="idle" icon="pause-circle" label="Paused" />`}
      ${ok && !t.paused && html`<span class="dl-speed">${fmtRate(pick(t, 'speedBytesPerSec', 'speedBps'))}</span>`}
    </div>
    ${t.state === 'error' && html`<${TileError} name="sabnzbd" view=${view} />`}
    ${items.map(
      (q, i) => html`<div class="dl-item" key=${i}>
        <div class="head"><span class="name">${q.name}</span><span class="eta">${Number.isFinite(q.etaS) ? fmtEta(q.etaS) : q.status || 'queued'}</span></div>
        <${Bar} pct=${q.pct} cls="thin" />
      </div>`,
    )}
    ${more > 0 && html`<span class="caption">and ${more} more in the queue</span>`}
    ${empty && html`<span class="empty">${empty}</span>`}
  </section>`;
}

function MediaHealth({ view }) {
  const rows = mediaHealthRows(view.media);
  return html`<section class="card tight" aria-label="Media health">
    <span class="label"><${Icon} name="first-aid-kit" />Media health</span>
    ${rows.map(
      (r, i) => html`<div class=${`hrow tone-${r.tone}`} key=${i}><${Icon} name=${r.icon} /><span>${r.label}</span><span>${r.text}</span></div>`,
    )}
  </section>`;
}

function Requests({ view, actions }) {
  const t = view.media.tiles.jellyseerr;
  const ok = t.state === 'ok';
  const list = ok && Array.isArray(t.requests) ? t.requests.map(requestView) : [];
  const n = Number(t.pending);
  const count = Number.isFinite(n) ? n : list.length;
  let empty = null;
  if (ok && !list.length) empty = 'Nothing waiting for approval.';
  else if (!ok && t.state !== 'error') empty = tileMessage('jellyseerr', t);
  return html`<section class="card tight" aria-label="Requests">
    <span class="label"><${Icon} name="hand-waving" />Requests${ok ? ` · ${count} pending` : ''}</span>
    ${t.state === 'error' && html`<${TileError} name="jellyseerr" view=${view} />`}
    ${list.map(
      (r, i) => html`<div class="req" key=${i}><span>${r.title}</span><span>${[r.by, r.at ? fmtWhen(r.at, view.now) : ''].filter(Boolean).join(' · ')}</span></div>`,
    )}
    ${empty && html`<span class="empty">${empty}</span>`}
    ${t.state === 'unconfigured' && html`<${Btn} href="#/settings" icon="gear-six" label="Open Settings" cls="align-start" />`}
  </section>`;
}

export function Renewals({ view }) {
  const list = view.renewalList;
  return html`<section class="card tight" aria-label="Subscription renewals">
    <span class="label"><${Icon} name="calendar-check" />Subscription renewals</span>
    ${list.map(
      (r) => html`<div class=${`renew tone-${r.tone}`} key=${r.key}>
        <span>${r.name}</span><span class="date">${r.date ? fmtDate(r.date) : '—'}</span><span class="left">${fmtDaysLeft(r.days)}</span>
      </div>`,
    )}
    ${!list.length && html`<span class="empty">No renewal dates yet.</span>`}
    <span class="caption">A reminder push fires 7 days before each date.</span>
    <${Btn} href="#/settings" icon="calendar-check" label="Edit dates in Settings" cls="align-start" />
  </section>`;
}

export function Media({ view, actions }) {
  return html`<div class="grid grid-340">
    <div class="stack">
      <${NowPlaying} view=${view} actions=${actions} />
      <${Downloads} view=${view} actions=${actions} />
    </div>
    <${MediaHealth} view=${view} />
    <div class="stack">
      <${Requests} view=${view} actions=${actions} />
      <${Renewals} view=${view} />
    </div>
  </div>`;
}
