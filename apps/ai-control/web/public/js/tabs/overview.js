// Overview: alerts, where answers go (with the GPU sparkline over the gate's window), services, resources, recent requests.

import { h, badge, icon, bar, mount } from '../dom.js';
import { bytes, duration, tokS, clock, str, toMs, DASH } from '../format.js';
import { explainRouting, serviceLabel, statusKey, statusWord, backendLabel } from '../explain.js';
import { diskRows } from '../state.js';
import { sparkline } from '../spark.js';
import { card, sectionError, emptyNote, loadingNote, kv, isObj, hasError } from '../ui.js';

const isNum = (v) => typeof v === 'number' && Number.isFinite(v);

export function alertsView(alerts) {
  if (!Array.isArray(alerts) || !alerts.length) return null;
  return h(
    'section',
    { class: 'alerts', 'aria-label': 'Alerts' },
    h('h2', { class: 'sr-only' }, 'Alerts'),
    h(
      'ul',
      { class: 'alert-list' },
      alerts.filter(isObj).map((a) => {
        const key = a.level === 'crit' ? 'crit' : 'warn';
        return h('li', { class: ['alert', `st-${key}`] }, badge(key, key === 'crit' ? 'Critical' : 'Warning'), h('p', null, str(a.text, 'An alert with no text.')));
      }),
    ),
  );
}

export function routingCard(state, { onRules } = {}) {
  const summary = state.summary || {};
  const r = explainRouting(summary.routing);
  const threshold = isObj(summary.routing) && isNum(summary.routing.threshold) ? summary.routing.threshold : 50;
  let graph;
  if (state.history.points) graph = sparkline(state.history.points, { threshold, minutes: state.history.minutes, windowS: isObj(summary.routing) ? summary.routing.window_s : undefined });
  else if (state.history.error) graph = sectionError('The GPU history', state.history.error);
  else graph = loadingNote('Loading the GPU history…');
  const key = statusKey(r.status);
  return card(
    'Where answers go',
    [
      h('div', { class: 'route-head' }, icon(r.target === 'gpu' ? 'gpu' : r.target === 'cpu' ? 'cpu' : 'unknown', 'route-icon'), h('p', { class: 'route-target' }, r.headline), badge(key, r.target === 'gpu' ? 'Fast' : r.target === 'cpu' ? 'Slower' : statusWord(key))),
      h('p', { class: 'route-why' }, r.sentence),
      graph,
      onRules ? h('button', { type: 'button', class: 'btn btn-quiet', onclick: onRules }, 'How the rule works') : null,
    ],
    { id: 'ov-route', cls: 'route-card' },
  );
}

export function servicesCard(summary, now) {
  const list = summary && summary.services;
  let body;
  if (hasError(list)) body = sectionError('Services', list);
  else if (!Array.isArray(list) || !list.length) body = emptyNote('No services reported.');
  else
    body = h(
      'ul',
      { class: 'svc-list' },
      list.filter(isObj).map((sv) => {
        const key = statusKey(sv.status);
        return h(
          'li',
          { class: 'svc' },
          h('div', { class: 'svc-main' }, h('p', { class: 'svc-name' }, serviceLabel(sv.id)), badge(key, statusWord(key))),
          h('p', { class: 'svc-detail' }, str(sv.detail, ''), toMs(sv.since) !== null ? h('span', { class: 'svc-since' }, `${str(sv.detail, '') ? ' · ' : ''}${key === 'ok' ? 'up' : 'like this'} for ${duration(Math.max(0, now - toMs(sv.since)))}`) : null),
        );
      }),
    );
  return card('Services', body, { id: 'ov-svc' });
}

function diskList(disks) {
  const rows = diskRows(disks);
  if (!rows.length) return emptyNote('No disks reported.');
  return h(
    'ul',
    { class: 'disk-list' },
    rows.map((d) =>
      h(
        'li',
        { class: 'disk' },
        h('div', { class: 'disk-top' }, h('span', { class: 'mono' }, str(d.mount)), h('span', null, `${bytes(d.avail)} free of ${bytes(d.total)}`, d.low ? h('span', { class: 'low-word' }, ' · low') : null)),
        bar(d.total ? d.used / d.total : 0, { low: d.low }),
      ),
    ),
  );
}

export function resourcesCard(summary) {
  const res = summary && summary.resources;
  if (hasError(res)) return card('Resources', sectionError('Resources', res), { id: 'ov-res' });
  if (!isObj(res)) return card('Resources', emptyNote('No resource data.'), { id: 'ov-res' });
  const parts = [];

  const ab = res.aibox;
  if (hasError(ab)) parts.push(h('h3', null, 'ai-box'), sectionError('ai-box', ab));
  else if (isObj(ab)) {
    const memUsed = isNum(ab.mem_total) && isNum(ab.mem_avail) ? ab.mem_total - ab.mem_avail : null;
    parts.push(
      h('h3', null, 'ai-box'),
      kv([
        ['Load (1 min, 4 cores)', isNum(ab.load1) ? ab.load1.toFixed(2) : DASH],
        ['Memory', memUsed !== null ? `${bytes(memUsed)} used of ${bytes(ab.mem_total)}` : DASH],
        ['Swap used', bytes(ab.swap_used)],
      ]),
      memUsed !== null && ab.mem_total > 0 ? bar(memUsed / ab.mem_total, { low: ab.mem_avail / ab.mem_total < 0.1 }) : null,
      diskList(ab.disk),
    );
  }

  const pc = res.pc;
  parts.push(h('h3', null, 'Gaming PC'));
  if (hasError(pc)) parts.push(sectionError('Gaming PC disks', pc));
  else if (isObj(pc)) parts.push(diskList(pc.disk));
  else parts.push(emptyNote('No data.'));

  const v = res.vram;
  parts.push(h('h3', null, 'GPU memory'));
  if (hasError(v)) parts.push(sectionError('GPU memory', v));
  else if (isObj(v)) parts.push(h('p', null, h('span', { class: 'mono' }, str(v.model)), ` is loaded, using ${bytes(v.size_vram)} of VRAM.`));
  else parts.push(emptyNote('No model is loaded on the GPU.'));

  const c = summary.clock;
  parts.push(h('h3', null, 'Clock'));
  if (hasError(c)) parts.push(sectionError('Clock status', c));
  else if (isObj(c)) {
    const key = c.synced === true ? 'ok' : 'warn';
    parts.push(h('p', { class: 'clock-line' }, badge(key, c.synced === true ? 'Synced' : 'Not synced'), ` ${isNum(c.offset_ms) ? `off by ${Math.abs(c.offset_ms) < 1 ? '<1 ms' : duration(Math.abs(c.offset_ms))}` : ''}`, c.source ? ` · source ${str(c.source)}` : ''));
  } else parts.push(emptyNote('No data.'));

  return card('Resources', parts, { id: 'ov-res' });
}

export function requestsCard(state, { limit = 8, title = 'Recent requests' } = {}) {
  const rq = state.requests;
  const tz = state.summary && isObj(state.summary.server) ? state.summary.server.timeZone : undefined;
  let body;
  if (rq.error && !rq.items) body = sectionError('Recent requests', rq.error);
  else if (!rq.items) body = loadingNote('Loading recent requests…');
  else if (!rq.items.length) body = emptyNote('No requests since the broker last started.');
  else
    body = h(
      'ul',
      { class: 'req-list' },
      rq.items
        .filter(isObj)
        .slice(0, limit)
        .map((r) => {
          const ok = isNum(r.status) && r.status < 400 && !r.error;
          const key = ok ? 'ok' : 'down';
          return h(
            'li',
            { class: 'req' },
            h('div', { class: 'req-top' }, h('span', { class: 'mono req-model' }, str(r.alias || r.model)), h('span', { class: 'req-backend' }, backendLabel(r.backend))),
            h(
              'div',
              { class: 'req-meta' },
              h('span', null, clock(r.ts, { timeZone: tz })),
              badge(key, ok ? `OK ${str(r.status, '')}`.trim() : `Failed ${isNum(r.status) ? r.status : ''}`.trim()),
              h('span', null, duration(r.duration_ms)),
              h('span', null, tokS(r.eval_tok_s)),
            ),
            r.error ? h('p', { class: 'req-error' }, str(r.error)) : null,
          );
        }),
    );
  return card(title, body, { id: 'ov-req' });
}

export function createOverview(actions = {}) {
  const el = h('div', { class: 'tab-body' });
  return {
    el,
    update(state, now = Date.now()) {
      const s = state.summary;
      if (!s) {
        mount(el, loadingNote(state.conn === 'offline' ? 'ai-box can\'t be reached and there is no saved copy yet.' : 'Loading…'));
        return;
      }
      mount(el, alertsView(s.alerts), routingCard(state, { onRules: actions.go ? () => actions.go('routing') : null }), servicesCard(s, now), resourcesCard(s), requestsCard(state));
    },
  };
}
