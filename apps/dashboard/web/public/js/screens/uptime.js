import { html, Icon } from '../ui.js';
import { lastMonths, failuresByMonth, APP_META, pick } from '../model.js';
import { fmtUptimePct, fmtDay, fmtDayTime, fmtSpan, fmtSpanPrecise, monthLabel, toDate } from '../format.js';
import { boxDays, boxPct, dayText } from './overview.js';

function BoxUptime({ view }) {
  const days = boxDays(view);
  const downs = (Array.isArray(view.uptime?.box?.downs) ? view.uptime.box.downs : [])
    .map((d) => ({ start: toDate(pick(d, 'start')), end: toDate(pick(d, 'end')), seconds: Number(pick(d, 'seconds')), reason: pick(d, 'reason') }))
    .filter((d) => d.start)
    .sort((a, b) => b.start - a.start)
    .slice(0, 8);
  return html`<section class="card tight span-all" aria-label="Box uptime">
    <div class="label-row"><span class="label">Box uptime · from the host's boot records</span><span class="big-figure">${fmtUptimePct(boxPct(view))}</span></div>
    <div class="daybars tall" role="list" aria-label="Box uptime per day, last 30 days">
      ${days.map((d) => html`<span class=${d.tone === 'none' ? '' : `tone-${d.tone}`} role="listitem" title=${dayText(d)} aria-label=${dayText(d)}></span>`)}
    </div>
    <div class="label-row caption mono"><span>${fmtDay(days[0].day)}</span><span>today</span></div>
    <div class="uptime-key" aria-hidden="true">
      <span><span class="daybars"><span class="tone-ok"></span></span>up all day</span>
      <span><span class="daybars"><span class="tone-warn"></span></span>some downtime</span>
      <span><span class="daybars"><span class="tone-fail"></span></span>down most of the day</span>
    </div>
    ${downs.map(
      (d, i) => html`<div class="down-row" key=${i}>
        <${Icon} name="arrow-down" label="Down" />
        <span class="t">${fmtDayTime(d.start)}</span>
        <span>${d.reason || 'Box down'}</span>
        <span class="dur">${Number.isFinite(d.seconds) ? fmtSpan(d.seconds) : d.end ? fmtSpan((d.end - d.start) / 1000) : 'ongoing'}</span>
      </div>`,
    )}
    ${!downs.length && html`<span class="empty">No downtime recorded in this window.</span>`}
  </section>`;
}

// Heatmap cell: ≥99.9 strong green, ≥99 green, ≥97 amber, else red; no data is a dark dash.
export function heatCell(pct) {
  if (!Number.isFinite(pct)) return { cls: 'cell none', text: '—' };
  const tone = pct >= 99 ? 'ok' : pct >= 97 ? 'warn' : 'fail';
  return { cls: `cell tone-${tone}${pct >= 99.9 ? ' best' : ''}`, tone, text: pct >= 99.95 ? '100' : pct.toFixed(1) };
}

function Heatmap({ view }) {
  const months = lastMonths(6, view.now);
  const rows = new Map();
  for (const a of view.apps) rows.set(a.name, new Map());
  for (const r of Array.isArray(view.uptime?.apps) ? view.uptime.apps : []) {
    const name = String(pick(r, 'name') ?? '');
    if (!name) continue;
    const m = rows.get(name) || new Map();
    for (const c of Array.isArray(r.months) ? r.months : []) m.set(String(c.month), c.pct === null || c.pct === undefined ? NaN : Number(c.pct));
    rows.set(name, m);
  }
  return html`<section class="card tight" aria-label="Monthly uptime per app">
    <span class="label" id="heat-label">Monthly uptime per app</span>
    <table class="heat" aria-labelledby="heat-label">
      <thead>
        <tr><td class="corner"></td>${months.map((m) => html`<th scope="col">${monthLabel(m)}</th>`)}</tr>
      </thead>
      <tbody>
        ${[...rows.entries()].map(
          ([name, m]) => html`<tr key=${name}>
            <th scope="row">${APP_META[name]?.label || name}</th>
            ${months.map((k) => {
              const c = heatCell(m.get(k));
              return html`<td><span class=${c.cls}>${c.text === '—' ? html`<span aria-hidden="true">—</span><span class="sr-only">no data</span>` : html`${c.text}<span class="sr-only">%</span>`}</span></td>`;
            })}
          </tr>`,
        )}
      </tbody>
    </table>
    <span class="caption">Percent of the month the container was healthy. Time held by the gate counts as down.</span>
  </section>`;
}

function Failures({ view }) {
  const months = lastMonths(6, view.now);
  const counts = failuresByMonth(view.gate, months);
  const max = Math.max(1, ...Object.values(counts));
  const h = (n) => (n ? (max <= 2 ? n * 34 : Math.max(8, (n / max) * 68)) : 2);
  return html`<section class="card" aria-label="Media-drive failures per month">
    <span class="label">Media-drive failures per month</span>
    <div class="fails">
      ${months.map(
        (k) => html`<div key=${k}>
          <span class="n">${counts[k]}</span>
          <span class=${`b${counts[k] ? '' : ' zero'}`} style=${{ height: `${h(counts[k])}px` }}></span>
          <span class="m">${monthLabel(k)}</span>
        </div>`,
      )}
    </div>
  </section>`;
}

function Drill({ view }) {
  const d = view.uptime?.restoreDrill || view.uptime?.restore_drill || null;
  const at = toDate(pick(d, 'at', 'ts'));
  const ok = d ? pick(d, 'ok', 'passed') !== false : null;
  return html`<section class="card tight" aria-label="Last restore drill">
    <div class="label-row"><span class="label">Last restore drill</span><span class="pill outline">later</span></div>
    ${d && at
      ? html`<div class=${`drill tone-${ok ? 'ok' : 'fail'}`}>
            <${Icon} name=${ok ? 'check-circle' : 'x-circle'} cls="tc" />
            <b>${ok ? 'Passed' : 'Failed'}</b>
            <span>${fmtDay(at)}${Number.isFinite(Number(d.durationSeconds)) ? ` · ${fmtSpanPrecise(d.durationSeconds)}` : ''}</span>
          </div>
          ${d.note && html`<span class="caption">${d.note}</span>`}`
      : html`<span class="empty">Restore drills come in a later version. Until then, follow the restore runbook by hand.</span>`}
  </section>`;
}

export function Uptime({ view }) {
  return html`<div class="grid grid-420">
    <${BoxUptime} view=${view} />
    <${Heatmap} view=${view} />
    <div class="stack">
      <${Failures} view=${view} />
      <${Drill} view=${view} />
    </div>
  </div>`;
}
