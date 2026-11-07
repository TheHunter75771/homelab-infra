// Test helper: loads the app's modules (from public/, or from MC_WEB_PUBLIC for the mutation check), builds
// page states from fixtures the way app.js does, and renders them into the DOM shim.

import path from 'node:path';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { installDom, allElements } from './dom-shim.mjs';
import * as fx from './fixtures.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
export const PUBLIC = process.env.MC_WEB_PUBLIC ? path.resolve(process.env.MC_WEB_PUBLIC) : path.resolve(HERE, '..', 'public');

export const imp = (p) => import(pathToFileURL(path.join(PUBLIC, p)).href);

export async function loadModules() {
  const [model, dom, view, app] = await Promise.all([imp('js/model.js'), imp('js/dom.js'), imp('js/view.js'), imp('js/app.js')]);
  return { model, dom, view, app };
}

const ok = (data) => ({ ok: true, status: 200, data });
const fail = (kind, status = null) => ({ ok: false, fail: { kind, status }, data: null });
export { ok, fail };

/**
 * A page state as app.js builds it. opts: {cards, briefing} are raw JSON (or a fail object made with fail()),
 * {cardsSnapshot, briefingSnapshot} are {savedAt, data}, {push} a push state.
 */
export function buildState(mods, opts = {}) {
  const { initialState, applyResult } = mods.app;
  let s = initialState();
  const res = (v) => (v && v.ok === false ? v : ok(v));
  if (opts.cards !== undefined) s = applyResult(s, 'cards', res(opts.cards), opts.cardsSnapshot ?? null, fx.NOW);
  if (opts.briefing !== undefined) s = applyResult(s, 'briefing', res(opts.briefing), opts.briefingSnapshot ?? null, fx.NOW);
  if (opts.push) s = { ...s, push: { phase: 'off', blocked: null, isIOS: false, busy: false, message: null, ...opts.push } };
  if (opts.highlight) s.highlight = opts.highlight;
  return s;
}

/** Renders a state into a fresh document; returns {doc, view, root}. */
export function render(mods, state, actions = {}) {
  const doc = installDom();
  const view = mods.view.createView(actions);
  doc.body.appendChild(view.el);
  view.update(state, fx.NOW);
  return { doc, view, root: view.el };
}

/** The <section> for a card id. */
export function card(root, id) {
  return allElements(root).find((e) => e.localName === 'section' && e.getAttribute('id') === `card-${id}`);
}

export function find(root, pred) {
  return allElements(root).filter(pred);
}

export { fx, allElements };
