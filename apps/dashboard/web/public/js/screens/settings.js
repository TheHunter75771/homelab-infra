import { html, Icon, Btn, Reason } from '../ui.js';
import { useState, useEffect } from '../../vendor/hooks.mjs';
import { renewals, pick } from '../model.js';
import { fmtDaysLeft, shortImage, dayKey } from '../format.js';

const RUNBOOKS = [
  'Failed boot gate',
  'Media drive failure',
  'Restore from scratch',
  'Rotate a secret',
  'First run of the apps',
  'Cloudflare tunnel',
];

function NumberField({ label, unit, value, min, max, disabled, onCommit }) {
  const [v, setV] = useState(String(value ?? ''));
  useEffect(() => setV(String(value ?? '')), [value]);
  // Reads the field itself: a change can arrive without input events (autofill, steppers).
  const commit = (e) => {
    const raw = e?.currentTarget?.value ?? v;
    const n = Number(raw);
    if (raw === '' || !Number.isInteger(n) || n < min || n > max) {
      setV(String(value ?? ''));
      if (e?.currentTarget) e.currentTarget.value = String(value ?? '');
      return;
    }
    setV(String(n));
    if (n !== Number(value)) onCommit(n);
  };
  return html`<label class="field">
    <span>${label}</span>
    <span class="field-row">
      <input class="input" type="number" inputmode="numeric" min=${min} max=${max} value=${v} disabled=${disabled}
        onInput=${(e) => setV(e.currentTarget.value)} onChange=${commit} />
      <span class="unit">${unit}</span>
    </span>
  </label>`;
}

function Thresholds({ view, state, ctx, actions }) {
  const t = state.settings?.thresholds || {};
  const disk = pick(t, 'diskWarnPct') ?? view.thresholds.diskWarnPct;
  const age = pick(t, 'backupMaxAgeH') ?? view.thresholds.backupMaxAgeH;
  return html`<section class="card" aria-label="Thresholds">
    <span class="label">Thresholds</span>
    <${NumberField} label="Disk warning at" unit="% used" value=${disk} min=${50} max=${99} disabled=${Boolean(ctx.paused)}
      onCommit=${(n) => actions.saveSettings({ thresholds: { diskWarnPct: n } }, 'Disk warning saved')} />
    <${NumberField} label="Backup max age" unit="hours" value=${age} min=${1} max=${720} disabled=${Boolean(ctx.paused)}
      onCommit=${(n) => actions.saveSettings({ thresholds: { backupMaxAgeH: n } }, 'Backup age saved')} />
    <${Reason} text=${ctx.paused} />
  </section>`;
}

function RenewalEditor({ view, state, ctx, actions }) {
  const source = state.settings?.subscriptions ?? view.renewalList.map((r) => ({ key: r.key, name: r.name, date: r.iso }));
  const list = renewals(source, view.now);
  const [name, setName] = useState('');
  const [date, setDate] = useState('');
  const raw = list.map((r) => ({ key: r.key, name: r.name, date: r.iso }));
  const save = (next, text) => actions.saveSettings({ subscriptions: next }, text);
  const setDateFor = (key, value) => {
    if (!value) return;
    save(raw.map((r) => (r.key === key ? { ...r, date: value } : r)), 'Renewal date saved');
  };
  const remove = (key) => save(raw.filter((r) => r.key !== key), 'Renewal removed');
  const add = async (e) => {
    e.preventDefault();
    if (!name.trim() || !date) return;
    if (await save([...raw, { name: name.trim(), date }], 'Renewal added')) {
      setName('');
      setDate('');
    }
  };
  const off = Boolean(ctx.paused);
  return html`<section class="card" aria-label="Subscription renewal dates">
    <span class="label">Subscription renewal dates</span>
    ${list.map(
      (r) => html`<div class=${`renew-edit tone-${r.tone}`} key=${r.key}>
        <span>${r.name}</span>
        <input class="input" type="date" value=${r.iso} disabled=${off} aria-label=${`${r.name} renews on`}
          onChange=${(e) => setDateFor(r.key, e.currentTarget.value)} />
        <span class="left">${fmtDaysLeft(r.days)}</span>
        <${Btn} icon="x" ariaLabel=${`Remove ${r.name}`} reason=${off ? ctx.paused : null} onClick=${() => remove(r.key)} />
      </div>`,
    )}
    ${!list.length && html`<span class="empty">No renewal dates yet. Add the indexers and the Usenet provider so a push reminds you a week before.</span>`}
    <form class="sub-form" onSubmit=${add}>
      <label>Name<input class="input" value=${name} maxlength="60" disabled=${off} onInput=${(e) => setName(e.currentTarget.value)} placeholder="e.g. Usenet provider" /></label>
      <label>Renews on<input class="input mono" type="date" value=${date} disabled=${off} min=${dayKey(view.now)} onInput=${(e) => setDate(e.currentTarget.value)} /></label>
      <${Btn} type="submit" icon="plus" label="Add" reason=${off ? ctx.paused : !name.trim() || !date ? 'Name and date needed' : null} />
    </form>
    <${Reason} text=${ctx.paused} />
  </section>`;
}

function KeyField({ id, label, info, disabled, actions }) {
  const [value, setValue] = useState('');
  const set = Boolean(pick(info, 'configured'));
  const last4 = pick(info, 'last4');
  const field = `${id}ApiKey`;
  const save = async (e) => {
    e.preventDefault();
    if (!value.trim()) return;
    if (await actions.saveSettings({ integrations: { [field]: value.trim() } }, `${label} key saved`)) setValue('');
  };
  return html`<form class="key-row" onSubmit=${save}>
    <span class="field-row"><span class="text2">${label}</span><span class=${`pill tone-${set ? 'ok' : 'idle'}`}>${set ? `set${last4 ? ` · ends ${last4}` : ''}` : 'not set'}</span></span>
    <span class="field-row">
      <input class="input mono" type="password" autocomplete="off" spellcheck=${false} placeholder=${set ? 'Replace the key' : 'Paste the API key'}
        value=${value} disabled=${disabled} aria-label=${`${label} API key`} onInput=${(e) => setValue(e.currentTarget.value)} />
      <${Btn} type="submit" icon="key" label="Save" reason=${disabled ? 'Paused' : !value.trim() ? 'Enter a key' : null} />
      ${set && html`<${Btn} icon="x" label="Clear" reason=${disabled ? 'Paused' : null} onClick=${() => actions.saveSettings({ integrations: { [field]: null } }, `${label} key cleared`)} />`}
    </span>
  </form>`;
}

function ApiKeys({ state, ctx, actions }) {
  const keys = state.settings?.integrations || {};
  return html`<section class="card" aria-label="API keys">
    <span class="label">API keys</span>
    <span class="caption">Sonarr, Radarr, Prowlarr and SABnzbd keys are read from their own config. Jellyfin and Jellyseerr need one here. Keys stay on the server and are never shown again.</span>
    <${KeyField} id="jellyfin" label="Jellyfin" info=${keys.jellyfin} disabled=${Boolean(ctx.paused)} actions=${actions} />
    <${KeyField} id="jellyseerr" label="Jellyseerr" info=${keys.jellyseerr} disabled=${Boolean(ctx.paused)} actions=${actions} />
  </section>`;
}

function Pinned({ view }) {
  return html`<section class="card list" aria-label="Pinned versions">
    <span class="label">Pinned versions</span>
    ${view.apps.map((a) => html`<div class="pin-row" key=${a.name}><span>${a.label}</span><span>${shortImage(a.pinned || a.image)}</span></div>`)}
    ${!view.apps.length && html`<span class="empty">No containers reported yet.</span>`}
    <span class="caption">Pins live in versions.yml in the homelab-infra repo. Renovate will propose bumps later.</span>
  </section>`;
}

function Runbooks({ view }) {
  const links = view.runbooks.filter((r) => r && r.title && r.url);
  return html`<section class="card list" aria-label="Runbooks">
    <span class="label">Runbooks</span>
    ${links.length
      ? links.map(
          (r) => html`<a class="link-row rows-top" href=${r.url} target="_blank" rel="noopener noreferrer">
            <${Icon} name="book-open" /><span>${r.title}</span><${Icon} name="arrow-up-right" />
          </a>`,
        )
      : html`${RUNBOOKS.map((t) => html`<div class="link-row rows-top text2"><${Icon} name="book-open" /><span>${t}</span></div>`)}
          <span class="caption">In docs/runbooks/ of the homelab-infra repo.</span>`}
  </section>`;
}

function About({ view }) {
  return html`<section class="card" aria-label="About">
    <span class="label">About</span>
    <div class="about">
      <span class="mono">dashboard ${view.version}</span>
      <span>Served at / on this origin, on the tailnet only.</span>
      <span>Host <span class="mono">${view.hostName}</span></span>
    </div>
  </section>`;
}

export function Settings({ view, state, ctx, actions }) {
  useEffect(() => {
    actions.loadSettings();
  }, []);
  return html`<div class="grid grid-380">
    ${state.settingsError &&
    html`<div class="err-panel span-all" role="alert">
      <span class="err-title"><${Icon} name="plugs" />Couldn't load the settings</span>
      <span class="text2">${state.settingsError}. Showing what the summary knows.</span>
      <${Btn} icon="arrow-clockwise" label="Retry" onClick=${() => actions.loadSettings()} />
    </div>`}
    <${Thresholds} view=${view} state=${state} ctx=${ctx} actions=${actions} />
    <${RenewalEditor} view=${view} state=${state} ctx=${ctx} actions=${actions} />
    <${ApiKeys} state=${state} ctx=${ctx} actions=${actions} />
    <${Pinned} view=${view} />
    <${Runbooks} view=${view} />
    <${About} view=${view} />
  </div>`;
}

