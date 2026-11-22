// The only way views build DOM. Tag names and attribute names always come from this code; any string from
// the server ends up in a Text node (createTextNode) or textContent, never in markup. There is no
// innerHTML, outerHTML, insertAdjacentHTML or document.write anywhere in public/ (test/static.test.mjs).

const SVG_NS = 'http://www.w3.org/2000/svg';

// Attributes views may set. Values from the server are never passed here (only literal strings and numbers
// the code computes), but the allowlist keeps it that way: an `on*` or `href`/`src` attribute can't be set.
const ATTRS = new Set([
  'id', 'role', 'type', 'name', 'value', 'for', 'tabindex', 'title', 'lang', 'autocomplete', 'inputmode', 'placeholder',
  'aria-label', 'aria-labelledby', 'aria-describedby', 'aria-live', 'aria-busy', 'aria-hidden', 'aria-current',
  'aria-selected', 'aria-controls', 'aria-expanded', 'aria-pressed', 'aria-disabled', 'aria-atomic', 'aria-valuenow', 'aria-valuemin',
  'aria-valuemax', 'aria-valuetext', 'datetime', 'spellcheck', 'maxlength', 'enterkeyhint',
]);
const SVG_ATTRS = new Set(['viewBox', 'd', 'points', 'x', 'y', 'x1', 'x2', 'y1', 'y2', 'cx', 'cy', 'r', 'width', 'height', 'fill', 'stroke', 'stroke-width', 'stroke-dasharray', 'stroke-linecap', 'stroke-linejoin', 'preserveAspectRatio', 'focusable', 'aria-hidden', 'role', 'aria-labelledby', 'id', 'vector-effect']);
const PROPS = new Set(['checked', 'disabled', 'selected', 'hidden']);

/** The allowlists, for the test that pins them (test/dom.test.mjs). */
export function allowedAttributes() {
  return { html: [...ATTRS].sort(), svg: [...SVG_ATTRS].sort(), props: [...PROPS].sort() };
}

export function text(s) {
  return document.createTextNode(typeof s === 'string' ? s : String(s ?? ''));
}

function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.appendChild(typeof c === 'object' ? c : text(c));
  }
}

/** h('p', {class: 'x'}, 'text', child). Strings become Text nodes. */
export function h(tag, props, ...children) {
  const el = document.createElement(tag);
  setProps(el, props, ATTRS);
  append(el, children);
  return el;
}

/** The SVG twin of h(), for icons and the sparkline. */
export function s(tag, props, ...children) {
  const el = document.createElementNS(SVG_NS, tag);
  setProps(el, props, SVG_ATTRS);
  append(el, children);
  return el;
}

function setProps(el, props, allowed) {
  if (!props) return;
  for (const [k, v] of Object.entries(props)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') el.setAttribute('class', Array.isArray(v) ? v.filter(Boolean).join(' ') : String(v));
    else if (k === 'text') el.textContent = String(v);
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (PROPS.has(k)) el[k] = !!v;
    else if (allowed.has(k)) el.setAttribute(k, v === true ? '' : String(v));
    else throw new Error(`dom.js: attribute not allowed: ${k}`);
  }
}

/** Replaces a container's children. */
export function mount(container, ...children) {
  container.replaceChildren();
  append(container, children);
  return container;
}

// Icons: inline SVG drawn from these fixed paths (own drawings, 24×24, stroke style). Each status icon has
// a different shape as well as a different colour, and is always next to a word.
const ICONS = {
  ok: ['M12 3a9 9 0 1 0 0 18a9 9 0 1 0 0-18', 'M8 12.5l2.6 2.6L16 9.6'],
  warn: ['M12 3.5L21.5 20h-19z', 'M12 10v4.5', 'M12 17.2v.3'],
  down: ['M8.3 3h7.4L21 8.3v7.4L15.7 21H8.3L3 15.7V8.3z', 'M9 9l6 6', 'M15 9l-6 6'],
  crit: ['M8.3 3h7.4L21 8.3v7.4L15.7 21H8.3L3 15.7V8.3z', 'M12 7.5v5.5', 'M12 16.2v.3'],
  unknown: ['M12 3a9 9 0 1 0 0 18a9 9 0 1 0 0-18', 'M9.6 9.4a2.5 2.5 0 1 1 3.4 2.3c-.7.3-1 .8-1 1.5v.4', 'M12 16.6v.3'],
  overview: ['M4 4h7v7H4z', 'M13 4h7v4h-7z', 'M13 10h7v10h-7z', 'M4 13h7v7H4z'],
  routing: ['M4 7h10a3 3 0 0 1 3 3v0', 'M4 17h10a3 3 0 0 0 3-3v0', 'M17 10v4', 'M14 4l3 3l-3 3', 'M14 14l3 3l-3 3'],
  models: ['M12 3l8 4.5v9L12 21l-8-4.5v-9z', 'M4 7.5l8 4.5l8-4.5', 'M12 12v9'],
  audit: ['M12 3l7 3v5.5c0 4.4-3 8-7 9.5c-4-1.5-7-5.1-7-9.5V6z', 'M9 12l2 2l4-4'],
  logs: ['M5 5h14', 'M5 9.5h14', 'M5 14h9', 'M5 18.5h11'],
  offline: ['M3 3l18 18', 'M8.5 8.8A9 9 0 0 0 4.2 11', 'M12 7a12 12 0 0 1 7.8 4', 'M8 15a5 5 0 0 1 6.1-.6', 'M12 19.2v.3'],
  refresh: ['M20 11a8 8 0 1 0-2.3 5.7', 'M20 5v6h-6'],
  gpu: ['M3 7h18v10H3z', 'M7 17v3', 'M17 17v3', 'M9 12a3 3 0 1 0 6 0a3 3 0 1 0-6 0'],
  cpu: ['M6 6h12v12H6z', 'M9 9h6v6H9z', 'M9 3v3', 'M15 3v3', 'M9 18v3', 'M15 18v3', 'M3 9h3', 'M3 15h3', 'M18 9h3', 'M18 15h3'],
  close: ['M6 6l12 12', 'M18 6L6 18'],
  user: ['M12 4a4 4 0 1 0 0 8a4 4 0 1 0 0-8', 'M4 20c1.5-3.5 4.4-5 8-5s6.5 1.5 8 5'],
};

export function icon(name, cls) {
  const paths = ICONS[name] || ICONS.unknown;
  return s(
    'svg',
    { class: ['icon', cls], viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': '1.8', 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true', focusable: 'false' },
    paths.map((d) => s('path', { d })),
  );
}

/** A status badge: icon shape + word. `key` must already be normalised (explain.statusKey). */
export function badge(key, word) {
  return h('span', { class: ['badge', `st-${key}`] }, icon(key), h('span', { class: 'badge-word' }, word));
}

/** A bar. Decorative: the numbers are always written next to it. `fraction` 0..1 is set through the CSSOM
 * (CSP style-src 'self' blocks style attributes, not element.style). */
export function bar(fraction, { low = false } = {}) {
  const fill = h('span', { class: ['bar-fill', low && 'low'] });
  const f = Number.isFinite(fraction) ? Math.min(1, Math.max(0, fraction)) : 0;
  fill.style.width = `${(f * 100).toFixed(1)}%`;
  return h('span', { class: 'bar', 'aria-hidden': 'true' }, fill);
}
