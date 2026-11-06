// Static checks on public/: no HTML-from-string APIs, CSP-compatible page, no other origins, the worker
// stamped for the current files, every import resolving.

import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { isStale, shellFiles } from '../scripts/stamp-sw.mjs';

const PUBLIC = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', 'public');
const read = (f) => fs.readFileSync(path.join(PUBLIC, f), 'utf8');
const jsFiles = shellFiles(PUBLIC).filter((f) => f.endsWith('.js')).concat('sw.js');

test('no innerHTML, outerHTML, insertAdjacentHTML, document.write, eval or Function anywhere', () => {
  const banned = /\.innerHTML|\.outerHTML|insertAdjacentHTML|document\.write|\beval\s*\(|new Function|createContextualFragment|DOMParser|srcdoc/;
  const code = (src) => src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:'"])\/\/.*$/gm, '$1'); // comments may name them
  for (const f of jsFiles) assert.doesNotMatch(code(read(f)), banned, f);
});

test('index.html is CSP-clean: no inline scripts, styles or handlers; only local files', () => {
  const html = read('index.html');
  assert.doesNotMatch(html, /<script(?![^>]*\bsrc=)[^>]*>/, 'inline script');
  assert.doesNotMatch(html, /<style|\sstyle=|\son[a-z]+=/i);
  for (const [, ref] of html.matchAll(/(?:src|href)="([^"]+)"/g)) {
    assert.ok(ref.startsWith('/'), `${ref} is same-origin`);
    assert.ok(fs.existsSync(path.join(PUBLIC, ref)), `${ref} exists`);
  }
});

test('no other origins (no CDN, no fonts from elsewhere)', () => {
  for (const f of shellFiles(PUBLIC).concat('sw.js')) {
    if (/\.(png|woff2)$/.test(f)) continue;
    const urls = [...read(f).matchAll(/https?:\/\/[^\s'")]+/g)].map((m) => m[0]);
    for (const u of urls) assert.match(u, /^http:\/\/www\.w3\.org\/(2000\/svg|1999\/xhtml)$/, `${f}: ${u}`);
  }
});

test('every relative import resolves inside public/', () => {
  for (const f of jsFiles) {
    for (const [, spec] of read(f).matchAll(/(?:^|\n)\s*(?:import|export)[^'"]*?from\s+'([^']+)'/g)) {
      assert.ok(spec.startsWith('./') || spec.startsWith('../'), `${f}: ${spec} is relative`);
      const target = path.resolve(path.dirname(path.join(PUBLIC, f)), spec);
      assert.ok(target.startsWith(PUBLIC) && fs.existsSync(target), `${f}: ${spec} exists`);
    }
  }
});

test('sw.js is stamped for the current files', () => {
  assert.equal(isStale(PUBLIC), false, 'run npm run stamp');
});

test('manifest is valid and its icons exist', () => {
  const m = JSON.parse(read('manifest.webmanifest'));
  assert.equal(m.start_url, '/');
  for (const i of m.icons) assert.ok(fs.existsSync(path.join(PUBLIC, i.src)), i.src);
});

test('CSS: focus-visible, reduced motion, 44 px targets, no @import', () => {
  const css = read('css/app.css');
  assert.match(css, /:focus-visible/);
  assert.match(css, /prefers-reduced-motion: reduce/);
  assert.match(css, /--target: 44px/);
  assert.doesNotMatch(css, /@import|url\(/);
});
