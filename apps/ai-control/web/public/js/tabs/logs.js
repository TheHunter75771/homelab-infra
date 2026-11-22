// Logs: a source picker (from GET /api/logs/sources), a line count, a client-side filter, and auto-refresh (off by default).
// Lines are host-written text: they go into a <pre> through textContent. Also the scheduled jobs (timers).

import { h, badge, mount } from '../dom.js';
import { clock, relTime, str } from '../format.js';
import { LOG_LINE_COUNTS, filterLines } from '../state.js';
import { card, sectionError, emptyNote, loadingNote, picker, syncSelect, isObj, hasError } from '../ui.js';

export function timersCard(summary, now) {
  const t = summary && summary.timers;
  const tz = summary && isObj(summary.server) ? summary.server.timeZone : undefined;
  let body;
  if (hasError(t)) body = sectionError('Scheduled jobs', t);
  else if (!Array.isArray(t) || !t.length) body = emptyNote('No scheduled jobs on ai-box.');
  else
    body = h(
      'ul',
      { class: 'cmd-list' },
      t.filter(isObj).map((j) => {
        const res = str(j.last_result, '');
        const key = !res ? 'unknown' : res === 'success' ? 'ok' : 'down';
        return h(
          'li',
          { class: 'cmd' },
          h('div', { class: 'svc-main' }, h('p', { class: 'mono' }, str(j.unit)), badge(key, !res ? 'Not run yet' : res === 'success' ? 'Last run OK' : 'Last run failed')),
          h('p', { class: 'cmd-meta' }, `Next ${j.next ? `${clock(j.next, { timeZone: tz, now })} (${relTime(j.next, now)})` : 'not scheduled'} · last ${j.last ? relTime(j.last, now) : 'never'}`, res && res !== 'success' ? ` · result ${res}` : ''),
        );
      }),
    );
  return card('Scheduled jobs (ai-box)', body, { id: 'lg-timers' });
}

export function createLogs(actions = {}) {
  const noop = () => {};
  const act = { logOptions: noop, logFilter: noop, refreshLogs: noop, ...actions };
  // The source list comes from the server. Option values are list indexes, never the server's ids, so no
  // server string ends up in an attribute; labels are option text.
  let sourceItems = null;
  const sourceSelect = h('select', { id: 'log-source', name: 'log-source' });
  sourceSelect.addEventListener('change', () => {
    const it = sourceItems && sourceItems[Number(sourceSelect.value)];
    if (it) act.logOptions({ source: it.id });
  });
  const sourceLabel = h('label', { for: 'log-source' }, 'Source');
  const sourceError = h('div');
  const source = { el: h('div', { class: 'field' }, sourceLabel, sourceSelect, sourceError) };
  const lines = picker('Lines', LOG_LINE_COUNTS.map((n) => ({ id: n, label: `Last ${n}` })), 200, (v) => act.logOptions({ lines: Number(v) }), 'log-lines');
  const filter = h('input', { type: 'search', id: 'log-filter', name: 'log-filter', autocomplete: 'off', spellcheck: 'false', placeholder: 'e.g. error', enterkeyhint: 'search' });
  filter.addEventListener('input', () => act.logFilter(filter.value));
  const auto = h('input', { type: 'checkbox', id: 'log-auto', name: 'log-auto' });
  auto.addEventListener('change', () => act.logOptions({ auto: auto.checked }));
  const refresh = h('button', { type: 'button', class: 'btn', id: 'log-refresh', onclick: () => act.refreshLogs() }, 'Refresh');
  const controls = h(
    'div',
    { class: 'controls', role: 'group', 'aria-label': 'Log options' },
    source.el,
    lines.el,
    h('div', { class: 'field field-grow' }, h('label', { for: 'log-filter' }, 'Filter (on this device)'), filter),
    h('div', { class: 'field field-check' }, auto, h('label', { for: 'log-auto' }, 'Refresh every 10 s')),
    refresh,
  );
  const status = h('p', { class: 'log-status', role: 'status', 'aria-live': 'polite' });
  const out = h('div', { class: 'log-wrap' });
  const timers = h('div');
  const el = h('div', { class: 'tab-body' }, card('Service logs', [controls, status, out], { id: 'lg-logs' }), timers);

  return {
    el,
    update(state, now = Date.now()) {
      const lg = state.logs;
      const src = lg.sources;
      if (src.items !== sourceItems) {
        sourceItems = src.items;
        sourceSelect.replaceChildren(...(sourceItems || []).map((it, i) => h('option', { value: String(i) }, it.label)));
      }
      const idx = sourceItems ? sourceItems.findIndex((it) => it.id === lg.source) : -1;
      if (idx > -1) syncSelect(sourceSelect, idx);
      sourceLabel.hidden = sourceSelect.hidden = !(sourceItems && sourceItems.length);
      if (src.error && !sourceItems) mount(sourceError, sectionError('The list of log sources', src.error));
      else if (sourceItems && !sourceItems.length) mount(sourceError, emptyNote('The server lists no log sources.'));
      else mount(sourceError);
      syncSelect(lines.select, lg.lines);
      if (filter.value !== lg.filter) filter.value = lg.filter;
      auto.checked = lg.auto;

      if (!lg.source) {
        status.textContent = '';
        mount(out, src.error || (sourceItems && !sourceItems.length) ? null : loadingNote('Loading the list of log sources…'));
      } else if (lg.error && !lg.text) {
        status.textContent = '';
        mount(out, sectionError('This log', lg.error));
      } else if (!lg.text) {
        status.textContent = lg.loading ? 'Loading…' : '';
        mount(out, loadingNote());
      } else {
        const shown = filterLines(lg.text, lg.filter);
        const label = idx > -1 ? sourceItems[idx].label : '';
        status.textContent =
          `${label}: ` +
          (lg.filter ? `${shown.length} of ${lg.text.length} lines match` : `${lg.text.length} lines`) +
          (lg.at ? `, fetched ${relTime(lg.at, now)}` : '') +
          (lg.truncated ? '. The server cut this log short.' : '') +
          (lg.error ? `. The last refresh failed: ${str(lg.error)}` : '') +
          (lg.loading ? '. Refreshing…' : '');
        mount(out, shown.length ? h('pre', { class: 'log-out', id: 'log-out', tabindex: '0', 'aria-label': 'Log lines' }, shown.join('\n')) : emptyNote(lg.filter ? 'No lines match the filter.' : 'The log is empty.'));
      }
      mount(timers, state.summary ? timersCard(state.summary, now) : null);
    },
  };
}
