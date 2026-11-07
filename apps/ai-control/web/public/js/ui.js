// Small view building blocks shared by the tabs.

import { h, badge } from './dom.js';
import { str } from './format.js';

export const isObj = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);
export const hasError = (v) => isObj(v) && 'error' in v;

/** A titled card. `focusHead` makes the heading a focus target (tabindex -1) for moving focus to it. */
export function card(title, body, { extra, cls, id, focusHead = false } = {}) {
  return h(
    'section',
    { class: ['card', cls], 'aria-labelledby': id ? `${id}-h` : null },
    h('div', { class: 'card-head' }, h('h2', { id: id ? `${id}-h` : null, tabindex: focusHead ? '-1' : null }, title), extra || null),
    body,
  );
}

/** "This part couldn't be loaded" with the server's error as plain text. */
export function sectionError(what, error) {
  const msg = str(isObj(error) ? error.error : error, '');
  return h('div', { class: 'section-error', role: 'status' }, badge('down', 'Unavailable'), h('p', null, msg ? `${what} couldn't be loaded: ${msg}` : `${what} couldn't be loaded.`));
}

export function emptyNote(textValue) {
  return h('p', { class: 'empty' }, textValue);
}

export function loadingNote(textValue = 'Loading…') {
  return h('p', { class: 'empty', role: 'status', 'aria-live': 'polite' }, textValue);
}

/** A definition-list row: label, value. */
export function kv(rows) {
  return h(
    'dl',
    { class: 'kv' },
    rows.filter(Boolean).map(([k, v]) => h('div', { class: 'kv-row' }, h('dt', null, k), h('dd', null, v))),
  );
}

/** A labelled <select>. Options are fixed lists from the code: [{id, label}]. */
export function picker(labelText, options, selected, onChange, id) {
  const sel = h(
    'select',
    { id, name: id },
    options.map((o) => h('option', { value: String(o.id), selected: String(o.id) === String(selected) }, o.label)),
  );
  sel.addEventListener('change', () => onChange(sel.value));
  return { el: h('div', { class: 'field' }, h('label', { for: id }, labelText), sel), select: sel };
}

/** Sets a <select>'s value without rebuilding it (keeps focus). */
export function syncSelect(sel, value) {
  if (sel.value !== String(value)) sel.value = String(value);
}
