import { html, Icon, Chip, Btn, Reason, useNow, useId } from '../ui.js';
import { useState } from '../../vendor/hooks.mjs';
import { GATE_EVENT } from '../model.js';
import {
  fmtCountdown,
  fmtBytes,
  fmtDay,
  fmtDate,
  fmtDayShort,
  fmtDayTime,
  fmtClockSec,
  fmtHorizon,
  fmtPct,
  daysBetween,
  plural,
} from '../format.js';
import { TAILNET_ONLY } from './apps.js';

function NextRow({ gate }) {
  const now = useNow(1000);
  if (gate.status === 'ok') {
    return html`<div class="next-row"><${Icon} name="power" /><span>Next check</span><b>next boot</b></div>`;
  }
  if (gate.status === 'waiting') {
    return html`<div class="next-row"><${Icon} name="hourglass" /><span>Boot check in progress</span><b>…</b></div>`;
  }
  const next = gate.nextRetryAt;
  const value = next ? fmtCountdown((next.getTime() - now) / 1000) : `every ${gate.retryMinutes} min`;
  return html`<div class="next-row" role="timer" aria-live="off">
    <${Icon} name="timer" /><span>Next automatic retry</span><b>${value}</b>
  </div>`;
}

export function rerunReason(view, { paused, remote }) {
  if (remote) return TAILNET_ONLY;
  if (paused) return paused;
  if (view.gate.action) return 'A re-run is already in progress.';
  if (!view.gate.canRerun) return view.gate.rerunLock || 'Not available right now.';
  return null;
}

function CurrentState({ view, ctx, actions }) {
  const g = view.gate;
  const reasonId = useId('rr');
  const held = view.apps.filter((a) => a.statusKey === 'held').length;
  const facts =
    g.status === 'ok'
      ? [
          ['Mounted', g.mountPath],
          ['Last check', g.lastPassAt ? fmtDayTime(g.lastPassAt) : '—'],
          ['Boot', g.currentBoot || '—'],
        ]
      : [
          ['Mounted', 'no'],
          ['Attempts', Number.isFinite(g.attempts) ? String(g.attempts) : '—'],
          ['Apps held', String(held)],
        ];
  const reason = rerunReason(view, ctx);
  return html`<section class="card" aria-label="Current state">
    <div class="label-row"><span class="label">Current state</span><${Chip} tone=${g.chip.tone} icon=${g.chip.icon} label=${g.status} mono big /></div>
    <span class="gate-line-big">${g.line}</span>
    <${NextRow} gate=${g} />
    <div class="facts three">${facts.map(([k, v]) => html`<div class="fact"><span>${k}</span><span>${v}</span></div>`)}</div>
    <div class="btn-col">
      <${Btn}
        icon="arrow-clockwise"
        label="Re-run check now"
        primary
        lg
        lock=${ctx.remote}
        reason=${reason}
        describedBy=${reason ? reasonId : undefined}
        onClick=${() => actions.ask('rerun')}
      />
      <${Reason} id=${reasonId} text=${reason} />
    </div>
  </section>`;
}

function UsageSplit({ view }) {
  const s = view.drive.split;
  const parts = s
    ? [
        ['TV', s.tv, 'c-tv'],
        ['Movies', s.movies, 'c-movies'],
        ['Downloads', s.downloads, 'c-dl'],
      ]
    : [];
  const sum = parts.reduce((a, [, v]) => a + (Number.isFinite(v) ? v : 0), 0);
  return html`<section class="card" aria-label="Usage split">
    <span class="label">Usage split${s && (view.drive.stale || s.stale) ? ' · last known' : ''}</span>
    ${s
      ? html`<div class="splitbar" role="img" aria-label=${parts.map(([k, v]) => `${k} ${fmtBytes(v, { zeroUnit: 'GB' })}`).join(', ')}>
            ${parts.map(([, v, c]) => html`<span class=${c} style=${{ width: `${sum > 0 ? (Math.max(0, v) / sum) * 100 : 0}%` }}></span>`)}
          </div>
          ${parts.map(
            ([k, v, c]) => html`<div class="split-row"><span class=${`sw ${c}`}></span><span>${k}</span><span>${Number.isFinite(v) ? fmtBytes(v, { zeroUnit: 'GB' }) : '—'}</span></div>`,
          )}
          ${s.at && html`<span class="caption">Sizes from ${fmtDayTime(s.at)}, refreshed daily.</span>`}`
      : html`<span class="empty">Folder sizes are worked out once a day. The first run hasn't finished yet.</span>`}
  </section>`;
}

export function forecastText(view) {
  const d = view.drive;
  const f = d.forecast;
  const lastKnown = d.stale && d.lastKnownAt ? ` · last known ${fmtDay(d.lastKnownAt)}` : d.stale ? ' · last known' : '';
  const usedLine =
    d.total > 0
      ? `${fmtBytes(d.used)} used of ${fmtBytes(d.total)} (${fmtPct(d.pct)})${f && f.perDayBytes > 0 ? ` · +${fmtBytes(f.perDayBytes)} a day` : ''}${lastKnown}`
      : 'No drive data yet';
  if (!f) return { head: 'Not enough data yet', usedLine };
  if (!(f.perDayBytes > 0) || !f.fullAt) return { head: 'Not filling up over the last 30 days', usedLine };
  const days = (f.fullAt.getTime() - view.now) / 86400000;
  return { head: `Full around ${fmtDate(f.fullAt)} (${fmtHorizon(days)})`, usedLine };
}

function Chart({ view }) {
  const c = view.drive.chart;
  const now = view.now;
  const right = c.fullAt ? `full ${fmtDayShort(c.fullAt, now)}` : fmtDayShort(c.end, now);
  return html`<div class="chart" role="img" aria-label=${`Used space over the last 60 days with a projection${c.fullAt ? ` to full around ${fmtDate(c.fullAt)}` : ''}.`}>
      <svg viewBox="0 0 600 180" preserveAspectRatio="none" aria-hidden="true" focusable="false">
        <line class="axis" x1="0" x2="600" y1="180" y2="180"></line>
        <line class="mid" x1="0" x2="600" y1="90" y2="90"></line>
        <line class="th-warn" x1="0" x2="600" y1=${c.yWarn} y2=${c.yWarn}></line>
        <line class="th-fail" x1="0" x2="600" y1=${c.yFail} y2=${c.yFail}></line>
        <line class="today" x1=${c.todayX} x2=${c.todayX} y1="0" y2="180"></line>
        ${c.area && html`<polygon class="area" points=${c.area}></polygon>`}
        ${c.used && html`<polyline class="used" points=${c.used}></polyline>`}
        ${c.proj && html`<polyline class="proj" points=${c.proj}></polyline>`}
      </svg>
      <span class="lbl warn" style=${{ top: `${c.yWarn - 2}px` }}>${c.warnPct}%${c.dWarn ? ` ${fmtDayShort(c.dWarn, now)}` : ''}</span>
      <span class="lbl fail" style=${{ top: `${c.yFail - 13}px` }}>97%${c.dFail ? ` ${fmtDayShort(c.dFail, now)}` : ''}</span>
      <span class="lbl y100">100%</span>
      <span class="lbl y50">50%</span>
      <span class="lbl start">${fmtDay(c.start)}</span>
      <span class="lbl today-l" style=${{ left: `${c.todayPct}%` }}>today</span>
      <span class="lbl full">${right}</span>
    </div>
    <div class="legend">
      <span><span class="sw-line"></span>Daily used space</span>
      <span><span class="sw-dash"></span>Projection</span>
    </div>`;
}

function Forecast({ view }) {
  const t = forecastText(view);
  const d = view.drive;
  const empty = d.pct !== null && d.pct < 1 ? ' No media on the drive yet.' : '';
  return html`<section class="card" aria-label="Space forecast">
    <div class="forecast-head"><span class="label">Space forecast</span><b>${t.head}</b><span>${t.usedLine}</span></div>
    ${d.chart
      ? html`<${Chart} view=${view} />`
      : html`<div class="no-chart"><${Icon} name="chart-line-up" />The forecast starts after 7 days of samples.${empty}</div>`}
  </section>`;
}

function rowTime(ts, started) {
  return started && daysBetween(started, ts) === 0 ? fmtClockSec(ts) : fmtDayTime(ts);
}

function History({ view }) {
  const [all, setAll] = useState(false);
  const groups = view.gate.groups;
  const shown = all ? groups : groups.slice(0, 6);
  return html`<section class="card span-all" aria-label="Gate history">
    <div class="label-row"><span class="label">Gate history</span><span class="caption">Newest first · grouped by boot · wait in seconds</span></div>
    ${shown.map(
      (g, gi) => html`<div class="hist-group" key=${`${g.boot}-${gi}`}>
        <div class="hist-boot">
          <${Icon} name="power" /><span class="mono">boot ${g.boot || '?'}</span>
          <span>started ${g.started ? fmtDayTime(g.started) : '—'}</span>
          ${g.current && html`<span class="cur">· current</span>`}
        </div>
        ${g.rows.map((r) => {
          const ev = GATE_EVENT[r.event] || { tone: 'idle', icon: 'info' };
          const wait = r.wait === null || r.wait === undefined || r.wait === '' ? '—' : String(Math.round(Number(r.wait)));
          return html`<div class=${`hist-row${r.event === 'powercycle' ? ' powercycle' : ''}`}>
            <span class="t">${rowTime(r.ts, g.started)}</span>
            <${Chip} tone=${ev.tone} icon=${ev.icon} label=${r.event} mono />
            <span class="d">${r.detail}</span>
            <span class="w" aria-label=${`waited ${wait} seconds`}>${wait}</span>
          </div>`;
        })}
      </div>`,
    )}
    ${!groups.length && html`<span class="empty">No gate events recorded yet.</span>`}
    ${groups.length > 6 &&
    html`<${Btn}
      cls="align-start"
      icon=${all ? 'caret-left' : 'clock-counter-clockwise'}
      label=${all ? 'Show recent boots only' : `Show ${plural(groups.length - 6, 'older boot')}`}
      onClick=${() => setAll(!all)}
    />`}
  </section>`;
}

export function Drive({ view, ctx, actions }) {
  return html`<div class="grid grid-440">
    <div class="stack">
      <${CurrentState} view=${view} ctx=${ctx} actions=${actions} />
      <${UsageSplit} view=${view} />
    </div>
    <${Forecast} view=${view} />
    <${History} view=${view} />
  </div>`;
}
