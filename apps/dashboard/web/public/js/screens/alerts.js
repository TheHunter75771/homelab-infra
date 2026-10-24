import { html, Icon, Btn, Reason, useId } from '../ui.js';
import { useState, useEffect } from '../../vendor/hooks.mjs';
import { alertChip } from '../model.js';
import { fmtDayTime, fmtDayYear, plural } from '../format.js';
import { pushEnv, guessLabel, deviceIcon, BLOCKED_REASON, storageGet, LABEL_KEY } from '../push.js';

function AlertList({ view }) {
  const [all, setAll] = useState(false);
  const list = all ? view.alerts : view.alerts.slice(0, 30);
  return html`<section class="card list" aria-label="Recent alerts">
    <span class="label">Recent alerts</span>
    ${list.map((al, i) => {
      const c = alertChip(al);
      return html`<div class=${`alert-row tone-${c.tone}`} key=${i}>
        <${Icon} name=${c.icon} />
        <div class="body">
          <div class="head"><b>${al.title}</b><span class="ts">${fmtDayTime(al.ts)}</span></div>
          ${al.body && html`<span class="text">${al.body}</span>`}
          <div class="foot">${al.tag && html`<span class="tag">${al.tag}</span>`}<span class="state">${c.label}</span></div>
        </div>
      </div>`;
    })}
    ${!view.alerts.length &&
    html`<div class="alert-row tone-plain">
      <div class="body">
        <b>No alerts yet</b>
        <span class="text muted">When the host or the off-site heartbeat raises something, it lands here and on your devices.</span>
      </div>
    </div>`}
    ${view.alerts.length > 30 &&
    html`<${Btn} cls="align-start" icon="clock-counter-clockwise" label=${all ? 'Show the latest 30' : `Show all ${view.alerts.length}`} onClick=${() => setAll(!all)} />`}
  </section>`;
}

function Devices({ view, state, ctx, actions }) {
  const env = pushEnv();
  const devices = state.devices ?? view.devices;
  const me = state.meId;
  const mine = Boolean(me) && devices.some((d) => d.id === me);
  const [label, setLabel] = useState(() => storageGet(LABEL_KEY) || guessLabel(env));
  const [busy, setBusy] = useState(false);
  const reasonId = useId('sub');
  const blocked = env.blocked ? BLOCKED_REASON[env.blocked] : null;
  const subReason = ctx.paused || blocked;
  const subscribe = async (e) => {
    e?.preventDefault?.();
    if (busy || subReason) return;
    setBusy(true);
    await actions.subscribe(label);
    setBusy(false);
  };
  return html`<section class="card" aria-label="Devices">
    <div class="label-row"><span class="label">Devices</span><span class="caption">${devices.length} subscribed</span></div>
    ${env.blocked === 'ios-browser' &&
    html`<div class="hint">
      <${Icon} name="export" />
      <span>On iPhone, pushes need the app on your Home Screen. Tap <b>Share</b>, then <b>Add to Home Screen</b>, and open it from there.</span>
    </div>`}
    ${devices.map(
      (d) => html`<div class="dev" key=${d.id}>
        <${Icon} name=${deviceIcon(d.label)} />
        <div class="body">
          <span class="name">${d.label}
            ${d.label === 'test' && html`<span class="pill tone-info">test</span>`}
            ${d.id === me && html`<span class="pill tone-accent">this device</span>`}</span>
          <span class="added">added ${fmtDayYear(d.created)}</span>
        </div>
        <${Btn} label="Remove" danger reason=${ctx.paused} onClick=${() => actions.ask('remove', { deviceId: d.id, label: d.label })} />
      </div>`,
    )}
    ${!devices.length &&
    html`<div class="stack tight-gap">
      <b class="big-cta">Subscribe this phone to get alerts</b>
      <span class="empty">No devices get pushes yet. If the drive fails, nobody will hear about it.</span>
    </div>`}
    ${!mine &&
    html`<form class="sub-form" onSubmit=${subscribe}>
        <label>Device name
          <input class="input" value=${label} maxlength="64" autocomplete="off" spellcheck=${false}
            onInput=${(e) => setLabel(e.currentTarget.value)} disabled=${Boolean(subReason)} />
        </label>
        <${Btn} type="submit" icon="bell-ringing" label="Subscribe this device" primary lg busy=${busy} reason=${subReason} describedBy=${subReason ? reasonId : undefined} />
      </form>
      ${subReason && env.blocked !== 'ios-browser' && html`<${Reason} id=${reasonId} text=${subReason} />`}
      <span class="caption">Name a device <span class="mono">test</span> to get rehearsal and drill pushes.</span>`}
    ${ctx.paused && devices.length > 0 && html`<${Reason} text=${ctx.paused} />`}
  </section>`;
}

function TestCard({ view, state, ctx, actions }) {
  const devices = state.devices ?? view.devices;
  const [busy, setBusy] = useState(false);
  const reason = ctx.paused || (devices.length ? null : 'Subscribe a device first');
  const send = async () => {
    setBusy(true);
    await actions.sendTest();
    setBusy(false);
  };
  return html`<section class="card" aria-label="Test">
    <span class="label">Test</span>
    <span class="text2">${devices.length ? `Sends a push to ${plural(devices.length, 'device')}.` : 'Subscribe a device first.'}</span>
    <${Btn} cls="align-start outline-accent" icon="paper-plane-tilt" label="Send test push" lg faded busy=${busy} reason=${reason} onClick=${send} />
    ${view.rehearsal &&
    html`<span class="reason tone-info tc"><${Icon} name="flask" />Rehearsal mode: only test devices receive drill pushes. This test goes to every device.</span>`}
  </section>`;
}

export function Alerts({ view, state, ctx, actions }) {
  useEffect(() => {
    actions.refreshDevices();
    actions.refreshMe();
  }, []);
  return html`<div class="grid grid-380">
    <${AlertList} view=${view} />
    <div class="stack">
      <${Devices} view=${view} state=${state} ctx=${ctx} actions=${actions} />
      <${TestCard} view=${view} state=${state} ctx=${ctx} actions=${actions} />
    </div>
  </div>`;
}
