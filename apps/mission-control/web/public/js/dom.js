// The only way views build DOM. Tag and attribute names always come from this code. A string from the
// server only ever becomes a Text node (createTextNode), or, for links, an href that safeHref() has parsed
// and normalised. There is no innerHTML, outerHTML, insertAdjacentHTML or document.write anywhere in
// public/ (test/static.test.mjs), and test/hostile.test.mjs proves server text never becomes markup.

import { safeHref } from './model.js';

const SVG_NS = 'http://www.w3.org/2000/svg';

// Attributes h() may set. No on*, href, src, style or action: an event handler goes in as a function
// (addEventListener), and a link only through extLink() / hashLink() below.
const ATTRS = new Set([
  'id', 'role', 'type', 'name', 'value', 'for', 'tabindex', 'title', 'lang', 'autocomplete', 'autocapitalize', 'inputmode',
  'placeholder', 'rows', 'maxlength', 'enterkeyhint', 'spellcheck', 'datetime', 'novalidate',
  'aria-label', 'aria-labelledby', 'aria-describedby', 'aria-live', 'aria-busy', 'aria-hidden', 'aria-current',
  'aria-controls', 'aria-expanded', 'aria-disabled', 'aria-atomic', 'aria-invalid',
]);
const SVG_ATTRS = new Set(['viewBox', 'd', 'cx', 'cy', 'r', 'x1', 'x2', 'y1', 'y2', 'fill', 'stroke', 'stroke-width', 'stroke-linecap', 'stroke-linejoin', 'opacity', 'focusable', 'aria-hidden']);
const PROPS = new Set(['disabled', 'hidden']);

/** The allowlists, for the test that pins them (test/dom.test.mjs). */
export function allowedAttributes() {
  return { html: [...ATTRS].sort(), svg: [...SVG_ATTRS].sort(), props: [...PROPS].sort() };
}

export function text(s) {
  return document.createTextNode(typeof s === 'string' ? s : String(s ?? ''));
}

function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false || c === '') continue;
    el.appendChild(typeof c === 'object' ? c : text(c));
  }
}

function setProps(el, props, allowed) {
  if (!props) return;
  for (const [k, v] of Object.entries(props)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') el.setAttribute('class', Array.isArray(v) ? v.filter(Boolean).join(' ') : String(v));
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (PROPS.has(k)) el[k] = !!v;
    else if (allowed.has(k)) el.setAttribute(k, v === true ? '' : String(v));
    else throw new Error(`dom.js: attribute not allowed: ${k}`);
  }
}

/** h('p', {class: 'x'}, 'text', child). Strings become Text nodes. */
export function h(tag, props, ...children) {
  const el = document.createElement(tag);
  setProps(el, props, ATTRS);
  append(el, children);
  return el;
}

/** The SVG twin of h(), for the fixed icons. */
export function s(tag, props, ...children) {
  const el = document.createElementNS(SVG_NS, tag);
  setProps(el, props, SVG_ATTRS);
  append(el, children);
  return el;
}

/** Replaces a container's children. */
export function mount(container, ...children) {
  container.replaceChildren();
  append(container, children);
  return container;
}

/**
 * A link to another app. `url` is untrusted: it goes through safeHref() (absolute http(s), no credentials,
 * normalised). When it fails the result is null and the caller shows plain text instead. Other apps open in a
 * new context with no opener and no referrer.
 */
export function extLink(url, props, ...children) {
  const href = safeHref(url);
  if (!href) return null;
  const el = h('a', props, ...children);
  el.setAttribute('href', href);
  el.setAttribute('target', '_blank');
  el.setAttribute('rel', 'noopener noreferrer');
  return el;
}

/** An in-page link. Only literal fragment ids (`#card-money`) from this code. */
export function hashLink(hash, props, ...children) {
  if (typeof hash !== 'string' || !/^#[a-z][a-z0-9_-]*$/.test(hash)) throw new Error(`dom.js: bad in-page link ${hash}`);
  const el = h('a', props, ...children);
  el.setAttribute('href', hash);
  return el;
}

// Icons: own drawings, 24×24, stroke style, fixed paths. Status icons differ in shape, not just colour, and
// always sit next to a word.
const ICONS = {
  ok: ['M12 3a9 9 0 1 0 0 18a9 9 0 1 0 0-18', 'M8 12.5l2.6 2.6L16 9.6'],
  warn: ['M12 3.5L21.5 20h-19z', 'M12 10v4.5', 'M12 17.2v.3'],
  down: ['M8.3 3h7.4L21 8.3v7.4L15.7 21H8.3L3 15.7V8.3z', 'M9 9l6 6', 'M15 9l-6 6'],
  unknown: ['M12 3a9 9 0 1 0 0 18a9 9 0 1 0 0-18', 'M9.6 9.4a2.5 2.5 0 1 1 3.4 2.3c-.7.3-1 .8-1 1.5v.4', 'M12 16.6v.3'],
  setup: ['M9 3v5', 'M15 3v5', 'M6 8h12v3a6 6 0 0 1-12 0z', 'M12 17v4'],
  briefing: ['M3 18h18', 'M6 18a6 6 0 0 1 12 0', 'M12 4v3', 'M4.2 9.2l2 1.5', 'M19.8 9.2l-2 1.5', 'M7 21h10'],
  ai: ['M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z', 'M18.5 15.5l.8 2.2l2.2.8l-2.2.8l-.8 2.2l-.8-2.2l-2.2-.8l2.2-.8z'],
  gauge: ['M4 17a8 8 0 1 1 16 0', 'M12 17l4-5', 'M12 17v.3', 'M4 20h16'],
  money: ['M3 7h18v12H3z', 'M3 11h18', 'M16 15h2', 'M6 7V5h12v2'],
  school: ['M2 9l10-5l10 5l-10 5z', 'M6 11v5c1.5 1.5 3.7 2.3 6 2.3s4.5-.8 6-2.3v-5', 'M22 9v6'],
  home: ['M3 11l9-7l9 7', 'M5 9.5V20h14V9.5', 'M10 20v-6h4v6'],
  servers: ['M4 4h16v6H4z', 'M4 14h16v6H4z', 'M8 7h.01', 'M8 17h.01', 'M12 7h5', 'M12 17h5'],
  alert: ['M6 16V11a6 6 0 0 1 12 0v5l1.5 2h-15z', 'M10 20.5a2 2 0 0 0 4 0'],
  bellOff: ['M3 3l18 18', 'M8.6 5.6A6 6 0 0 1 18 11v4', 'M6 11v5l-1.5 2H17', 'M10 20.5a2 2 0 0 0 4 0'],
  refresh: ['M20 11a8 8 0 1 0-2.3 5.7', 'M20 5v6h-6'],
  external: ['M14 4h6v6', 'M20 4l-9 9', 'M18 14v5H5V6h5'],
  send: ['M4 12l16-8l-6 16l-2.5-6.5z', 'M11.5 13.5L20 4'],
  offline: ['M3 3l18 18', 'M8.5 8.8A9 9 0 0 0 4.2 11', 'M12 7a12 12 0 0 1 7.8 4', 'M8 15a5 5 0 0 1 6.1-.6', 'M12 19.2v.3'],
  share: ['M12 3v12', 'M8 7l4-4l4 4', 'M5 11v9h14v-9'],
  plusSquare: ['M4 4h16v16H4z', 'M12 8v8', 'M8 12h8'],
  apps: ['M4 4h6v6H4z', 'M14 4h6v6h-6z', 'M4 14h6v6H4z', 'M14 14h6v6h-6z'],
};

export function icon(name, cls) {
  const paths = ICONS[name] || ICONS.unknown;
  return s(
    'svg',
    { class: ['icon', cls], viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': '1.8', 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true', focusable: 'false' },
    paths.map((d) => s('path', { d })),
  );
}

/** The header mark: the app icon's radar, drawn small. */
export function radarMark() {
  return s(
    'svg',
    { class: 'mark', viewBox: '0 0 24 24', fill: 'none', 'aria-hidden': 'true', focusable: 'false' },
    s('circle', { cx: '12', cy: '12', r: '9.5', stroke: 'currentColor', 'stroke-width': '1.6' }),
    s('circle', { cx: '12', cy: '12', r: '5.5', stroke: 'currentColor', 'stroke-width': '1.2', opacity: '0.55' }),
    s('path', { d: 'M12 12L19.3 5.9', stroke: 'currentColor', 'stroke-width': '1.8', 'stroke-linecap': 'round' }),
    s('circle', { cx: '14.6', cy: '7.6', r: '1.3', fill: 'currentColor' }),
  );
}

/** A status badge: icon shape + word. `key` comes from model.statusKey(). */
export function badge(key, word) {
  return h('span', { class: ['badge', `st-${key}`] }, icon(key), h('span', { class: 'badge-word' }, word));
}
