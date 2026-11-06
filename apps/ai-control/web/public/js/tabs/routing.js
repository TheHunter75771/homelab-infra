// Routing: view only (P1). Where answers go, why, the numbers behind it, and the rule in plain words.

import { h, badge, mount } from '../dom.js';
import { pct, str, relTime, windowText, DASH } from '../format.js';
import { explainRouting, ruleSentences, uidMatchNote, statusKey, statusWord, gateProblem } from '../explain.js';
import { sparkline } from '../spark.js';
import { card, sectionError, loadingNote, kv, isObj, hasError } from '../ui.js';

const isNum = (v) => typeof v === 'number' && Number.isFinite(v);

function gateRow(routing) {
  const gp = gateProblem(routing);
  return gp ? gp.row : 'Answering';
}

export function createRouting() {
  const el = h('div', { class: 'tab-body' });
  return {
    el,
    update(state, now = Date.now()) {
      const s = state.summary;
      if (!s) {
        mount(el, loadingNote());
        return;
      }
      const summary = s;
      const routing = s.routing;
      const r = explainRouting(routing);
      const key = statusKey(r.status);
      const cards = [];

      cards.push(
        card('Right now', [h('div', { class: 'route-head' }, h('p', { class: 'route-target' }, r.headline), badge(key, r.target === 'gpu' ? 'Fast' : r.target === 'cpu' ? 'Slower' : statusWord(key))), h('p', { class: 'route-why' }, r.sentence)], { id: 'rt-now', cls: 'route-card' }),
      );

      if (isObj(routing) && !hasError(routing)) {
        const game = str(routing.game, '');
        const busy = isNum(routing.gpu_busy) ? routing.gpu_busy : null;
        const share = isNum(routing.ollama_share) ? routing.ollama_share : null;
        const gameUse = busy !== null ? Math.max(0, busy - (share ?? 0)) : null;
        const note = uidMatchNote(routing);
        cards.push(
          card(
            'The numbers',
            [
              kv([
                ['Game running', game ? h('span', { class: 'mono' }, game) : 'No'],
                ['GPU busy now', pct(busy)],
                ["Ollama's own share", pct(share)],
                ['Game GPU use now', gameUse !== null ? pct(gameUse) : DASH],
                [`Average of game use (${windowText(routing.window_s) || 'gate window'})`, pct(routing.avg30)],
                ['Limit', isNum(routing.threshold) ? pct(routing.threshold) : '50% (default)'],
                ['Gate', gateRow(routing)],
                ['Summary from', relTime(s.server && s.server.time, now)],
              ]),
              note ? h('p', { class: 'note' }, badge('warn', 'Note'), ' ', note) : null,
            ],
            { id: 'rt-num' },
          ),
        );
      } else if (hasError(routing)) {
        cards.push(card('The numbers', sectionError('Routing', routing), { id: 'rt-num' }));
      }

      const threshold = isObj(routing) && isNum(routing.threshold) ? routing.threshold : 50;
      let graph;
      if (state.history.points) graph = sparkline(state.history.points, { threshold, minutes: state.history.minutes, windowS: isObj(summary.routing) ? summary.routing.window_s : undefined });
      else if (state.history.error) graph = sectionError('The GPU history', state.history.error);
      else graph = loadingNote('Loading the GPU history…');
      cards.push(card(`Last ${windowText(state.history.minutes * 60)}`, graph, { id: 'rt-hist' }));

      cards.push(
        card(
          'The rule',
          [
            h('ol', { class: 'rule-list' }, ruleSentences(routing).map((t) => h('li', null, t))),
            h('p', { class: 'note' }, 'This page only shows the rule. Overrides ("keep the GPU off for 2 h") and settings come in the next version.'),
          ],
          { id: 'rt-rule' },
        ),
      );
      mount(el, cards);
    },
  };
}
