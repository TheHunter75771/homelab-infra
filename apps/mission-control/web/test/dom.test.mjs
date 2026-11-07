// Pins dom.js: the attribute allowlists, and the only two ways a link gets an href.
import test from 'node:test';
import assert from 'node:assert/strict';
import { loadModules } from './render.mjs';
import { installDom } from './dom-shim.mjs';

const { dom } = await loadModules();

const HTML = [
  'aria-atomic', 'aria-busy', 'aria-controls', 'aria-current', 'aria-describedby', 'aria-disabled', 'aria-expanded', 'aria-hidden',
  'aria-invalid', 'aria-label', 'aria-labelledby', 'aria-live', 'autocapitalize', 'autocomplete', 'datetime', 'enterkeyhint', 'for', 'id',
  'inputmode', 'lang', 'maxlength', 'name', 'novalidate', 'placeholder', 'role', 'rows', 'spellcheck', 'tabindex', 'title', 'type', 'value',
];
const SVG = ['aria-hidden', 'cx', 'cy', 'd', 'fill', 'focusable', 'opacity', 'r', 'stroke', 'stroke-linecap', 'stroke-linejoin', 'stroke-width', 'viewBox', 'x1', 'x2', 'y1', 'y2'];

test('the attribute allowlists are exactly the expected sets', () => {
  const a = dom.allowedAttributes();
  assert.deepEqual(a.html, [...HTML].sort());
  assert.deepEqual(a.svg, [...SVG].sort());
  assert.deepEqual(a.props, ['disabled', 'hidden']);
  for (const list of [a.html, a.svg]) {
    for (const bad of ['href', 'src', 'srcdoc', 'style', 'action', 'formaction', 'xlink:href', 'srcset', 'data', 'target', 'rel'])
      assert.ok(!list.includes(bad), `${bad} is not allowed`);
    assert.ok(!list.some((n) => /^on/i.test(n)), 'no event-handler attributes');
  }
});

test('h() and s() refuse anything off the list', () => {
  installDom();
  assert.throws(() => dom.h('a', { href: 'https://a.example.test' }), /attribute not allowed: href/);
  assert.throws(() => dom.h('div', { style: 'x' }), /not allowed/);
  assert.throws(() => dom.h('div', { onclick: 'alert(1)' }), /not allowed/, 'a string handler is an attribute, refused');
  assert.throws(() => dom.s('svg', { 'xlink:href': '#x' }), /not allowed/);
  const t = dom.h('p', null, '<b>x</b>');
  assert.equal(t.childNodes[0].nodeType, 3, 'strings become Text nodes');
});

test('extLink: only http(s) links, normalised, new context without opener or referrer', () => {
  installDom();
  const a = dom.extLink('https://money.example.test/x y', null, 'Open');
  assert.equal(a.getAttribute('href'), 'https://money.example.test/x%20y');
  assert.equal(a.getAttribute('target'), '_blank');
  assert.equal(a.getAttribute('rel'), 'noopener noreferrer');
  for (const bad of ['javascript:alert(1)', ' javascript:alert(1)', 'data:text/html,x', 'vbscript:x', '/relative', '', null, {}, 'https://u:p@x.example.test'])
    assert.equal(dom.extLink(bad, null, 'x'), null, String(bad));
});

test('hashLink: literal fragment ids only', () => {
  installDom();
  assert.equal(dom.hashLink('#notify', null, 'x').getAttribute('href'), '#notify');
  for (const bad of ['javascript:alert(1)', '#"><img>', 'https://x.example.test/#a', '#', '#A']) assert.throws(() => dom.hashLink(bad, null, 'x'), /bad in-page link/, bad);
});
