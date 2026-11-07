// Pins dom.js's attribute allowlists: adding href, src, style or an on* name must fail here.
import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { PUBLIC } from './render.mjs';
import { installDom } from './dom-shim.mjs';

const dom = await import(pathToFileURL(path.join(PUBLIC, 'js', 'dom.js')).href);

const HTML = [
  'aria-atomic', 'aria-busy', 'aria-controls', 'aria-current', 'aria-describedby', 'aria-disabled', 'aria-expanded', 'aria-hidden',
  'aria-label', 'aria-labelledby', 'aria-live', 'aria-pressed', 'aria-selected', 'aria-valuemax', 'aria-valuemin', 'aria-valuenow',
  'aria-valuetext', 'autocomplete', 'datetime', 'enterkeyhint', 'for', 'id', 'inputmode', 'lang', 'maxlength', 'name',
  'placeholder', 'role', 'spellcheck', 'tabindex', 'title', 'type', 'value',
];
const SVG = [
  'aria-hidden', 'aria-labelledby', 'cx', 'cy', 'd', 'fill', 'focusable', 'height', 'id', 'points', 'preserveAspectRatio', 'r', 'role',
  'stroke', 'stroke-dasharray', 'stroke-linecap', 'stroke-linejoin', 'stroke-width', 'vector-effect', 'viewBox', 'width', 'x',
  'x1', 'x2', 'y', 'y1', 'y2',
];

test('the attribute allowlists are exactly the expected sets', () => {
  const a = dom.allowedAttributes();
  assert.deepEqual(a.html, [...HTML].sort());
  assert.deepEqual(a.svg, [...SVG].sort());
  assert.deepEqual(a.props, ['checked', 'disabled', 'hidden', 'selected']);
  for (const list of [a.html, a.svg]) {
    for (const bad of ['href', 'src', 'srcdoc', 'style', 'action', 'formaction', 'xlink:href', 'srcset', 'data', 'poster', 'background'])
      assert.ok(!list.includes(bad), `${bad} is not allowed`);
    assert.ok(!list.some((n) => /^on/i.test(n)), 'no event-handler attributes');
  }
});

test('h() and s() refuse anything off the list', () => {
  installDom();
  assert.throws(() => dom.h('a', { href: 'javascript:alert(1)' }), /attribute not allowed: href/);
  assert.throws(() => dom.h('div', { style: 'x' }), /not allowed/);
  assert.throws(() => dom.h('div', { onclick: 'alert(1)' }), /not allowed/, 'a string handler is an attribute, refused');
  assert.throws(() => dom.s('svg', { 'xlink:href': '#x' }), /not allowed/);
  const el = dom.h('p', null, '<b>x</b>');
  assert.equal(el.childNodes[0].nodeType, 3, 'a string child is a Text node');
});
