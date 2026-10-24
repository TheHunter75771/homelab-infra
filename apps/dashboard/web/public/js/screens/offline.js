import { html, Icon, Btn } from '../ui.js';
import { useState } from '../../vendor/hooks.mjs';
import { healthBanner, offlineRows } from '../model.js';
import { fmtClock, fmtDayTime, daysBetween } from '../format.js';

const stamp = (ms, now) => (daysBetween(ms, now) === 0 ? fmtClock(ms) : fmtDayTime(ms));

// Full-screen view from the snapshot the service worker / app saved on this device.
export function Offline({ view, savedAt, actions }) {
  const [busy, setBusy] = useState(false);
  const now = Date.now();
  const retry = async () => {
    setBusy(true);
    await actions.refresh();
    setBusy(false);
  };
  let line = 'Nothing has been saved on this device yet, so there is no last known state to show.';
  if (view && savedAt) {
    const b = healthBanner(view);
    const what = b.issues.length ? b.issues[0].text.replace(/\.$/, '').replace(/^./, (c) => c.toLowerCase()) : 'all systems normal';
    line = `Last known state at ${stamp(savedAt, now)}: ${what}.`;
  }
  return html`<div class="offline" role="alert" aria-live="assertive">
    <div class="offline-inner">
      <div class="off-ring"><${Icon} name="cloud-slash" /></div>
      <div class="stack tight-gap">
        <h1>Can't reach the server</h1>
        <p class="off-line">${line}</p>
      </div>
      ${view &&
      savedAt &&
      html`<div class="off-snap">
        <span class="caption"><${Icon} name="clock-counter-clockwise" />Saved on this device · ${stamp(savedAt, now)}</span>
        ${offlineRows(view).map(
          (r) => html`<div class=${`off-row tone-${r.tone}`}><${Icon} name=${r.tone === 'ok' ? 'check-circle' : r.tone === 'fail' ? 'x-circle' : r.tone === 'warn' ? 'warning' : 'pause-circle'} /><span>${r.k}</span><span>${r.v}</span></div>`,
        )}
      </div>`}
      <div class="off-info"><${Icon} name="bell-ringing" /><span>Alerts still arrive by push. They come from the host and an off-site heartbeat, not from this dashboard.</span></div>
      <p class="off-note">The dashboard runs on the host, outside Docker, so the boot gate doesn't take it down. If this lasts, the box is off or off the tailnet; the off-site heartbeat pushes when it notices.</p>
      <${Btn} icon="arrow-clockwise" label="Try again" primary busy=${busy} onClick=${retry} />
    </div>
  </div>`;
}
