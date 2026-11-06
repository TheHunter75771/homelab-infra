// Models: what each side has, what's loaded, and the `assistant` mapping. View only in P1.

import { h, badge, mount } from '../dom.js';
import { bytes, relTime, str } from '../format.js';
import { card, sectionError, emptyNote, loadingNote, isObj, hasError } from '../ui.js';

const SIDES = [
  { id: 'gpu', title: 'Gaming PC (GPU)' },
  { id: 'cpu', title: 'ai-box (CPU)' },
];

/** Is `name` in the side's list? Ollama names match with or without ":latest". */
export function hasModel(list, name) {
  if (!Array.isArray(list) || typeof name !== 'string' || !name) return false;
  const norm = (n) => (typeof n === 'string' ? (n.includes(':') ? n : `${n}:latest`) : '');
  return list.some((m) => isObj(m) && norm(m.name) === norm(name));
}

function loadedOn(models, side) {
  const l = isObj(models.loaded) ? models.loaded[side] : null;
  return Array.isArray(l) ? l.filter(isObj) : [];
}

function aliasCard(models) {
  const alias = isObj(models.alias) ? models.alias.assistant : null;
  if (!isObj(alias)) return card('The "assistant" model', emptyNote('The broker reports no "assistant" alias.'), { id: 'md-alias' });
  return card(
    'The "assistant" model',
    [
      h('p', { class: 'note' }, 'Open WebUI and the tools ask for "assistant". The broker turns that into a real model on whichever side is answering.'),
      h(
        'ul',
        { class: 'alias-list' },
        SIDES.map((side) => {
          const name = str(alias[side.id], '');
          const list = models[side.id];
          let status;
          if (!name) status = badge('warn', 'Not set');
          else if (hasError(list)) status = badge('unknown', 'Can\'t check');
          else status = hasModel(list, name) ? badge('ok', 'Installed') : badge('down', 'Missing');
          return h('li', { class: 'alias' }, h('p', { class: 'alias-side' }, `On the ${side.id.toUpperCase()}`), h('p', { class: 'mono alias-model' }, name || '—'), status);
        }),
      ),
    ],
    { id: 'md-alias' },
  );
}

function sideCard(models, side, now) {
  const list = models[side.id];
  if (hasError(list)) return card(side.title, sectionError(`The ${side.title} model list`, list), { id: `md-${side.id}` });
  if (!Array.isArray(list) || !list.length) return card(side.title, emptyNote('No models installed.'), { id: `md-${side.id}` });
  const loaded = loadedOn(models, side.id);
  const alias = isObj(models.alias) && isObj(models.alias.assistant) ? models.alias.assistant[side.id] : null;
  return card(
    side.title,
    h(
      'ul',
      { class: 'model-list' },
      list.filter(isObj).map((m) => {
        const live = loaded.find((l) => l.name === m.name);
        return h(
          'li',
          { class: 'model' },
          h('div', { class: 'model-top' }, h('p', { class: 'mono model-name' }, str(m.name)), live ? badge('ok', 'Loaded') : null, typeof alias === 'string' && hasModel([m], alias) ? h('span', { class: 'tag' }, 'assistant') : null),
          h(
            'p',
            { class: 'model-meta' },
            [bytes(m.size), str(m.quant, ''), m.modified ? `changed ${relTime(m.modified, now)}` : ''].filter(Boolean).join(' · '),
            live ? h('span', null, ` · in memory ${bytes(live.size_vram)}`, live.expires_at ? `, unloads ${relTime(live.expires_at, now)}` : '') : null,
          ),
        );
      }),
    ),
    { id: `md-${side.id}` },
  );
}

export function createModels() {
  const el = h('div', { class: 'tab-body' });
  return {
    el,
    update(state, now = Date.now()) {
      const s = state.summary;
      if (!s) {
        mount(el, loadingNote());
        return;
      }
      const models = s.models;
      if (hasError(models)) {
        mount(el, card('Models', sectionError('Models', models)));
        return;
      }
      if (!isObj(models)) {
        mount(el, card('Models', emptyNote('No model data.')));
        return;
      }
      mount(el, aliasCard(models), sideCard(models, SIDES[0], now), sideCard(models, SIDES[1], now), h('p', { class: 'note' }, 'Pulling and removing models comes in the next version.'));
    },
  };
}
