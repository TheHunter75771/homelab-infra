// Static checks on public/: no HTML-from-string APIs, a CSP-clean page, no other origins, imports resolve,
// the worker stamped, a valid manifest with real icons, CSS accessibility basics and colour contrast.

import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { isStale, shellFiles } from '../scripts/stamp-sw.mjs';
import { FILES, rasterize, decodePng, svg } from '../scripts/make-icons.mjs';

const PUBLIC = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', 'public');
const read = (f) => fs.readFileSync(path.join(PUBLIC, f), 'utf8');
const jsFiles = shellFiles(PUBLIC).filter((f) => f.endsWith('.js')).concat('sw.js');
const code = (src) => src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:'"])\/\/.*$/gm, '$1'); // comments may name them

test('no innerHTML, outerHTML, insertAdjacentHTML, document.write, eval or Function anywhere', () => {
  const banned = /\.innerHTML|\.outerHTML|insertAdjacentHTML|document\.write|\beval\s*\(|new Function|createContextualFragment|DOMParser|srcdoc|setHTMLUnsafe|parseHTMLUnsafe/;
  for (const f of jsFiles) assert.doesNotMatch(code(read(f)), banned, f);
});

test('hrefs are only set inside dom.js (extLink / hashLink), never by a view', () => {
  for (const f of jsFiles.filter((x) => x !== 'js/dom.js')) {
    assert.doesNotMatch(code(read(f)), /setAttribute\(\s*['"](href|src|style|on\w+)['"]|\.href\s*=|\.src\s*=/, f);
  }
});

test('index.html is CSP-clean: no inline scripts, styles or handlers; only local files', () => {
  const html = read('index.html');
  assert.doesNotMatch(html, /<script(?![^>]*\bsrc=)[^>]*>/, 'inline script');
  assert.doesNotMatch(html, /<style|\sstyle=|\son[a-z]+=/i);
  for (const [, ref] of html.matchAll(/(?:src|href)="([^"]+)"/g)) {
    assert.ok(ref.startsWith('/'), `${ref} is same-origin`);
    assert.ok(fs.existsSync(path.join(PUBLIC, ref)), `${ref} exists`);
  }
  assert.match(html, /<html lang="en">/);
  assert.match(html, /name="viewport" content="[^"]*viewport-fit=cover/);
  assert.match(html, /rel="apple-touch-icon" href="\/icons\/apple-touch-icon\.png"/);
});

test('no other origins (no CDN, no web fonts)', () => {
  for (const f of shellFiles(PUBLIC).concat('sw.js')) {
    if (/\.png$/.test(f)) continue;
    const urls = [...read(f).matchAll(/https?:\/\/[^\s'")]+/g)].map((m) => m[0]);
    for (const u of urls) assert.match(u, /^http:\/\/www\.w3\.org\/2000\/svg$/, `${f}: ${u}`);
  }
  assert.doesNotMatch(read('css/app.css'), /@import|url\(|@font-face/);
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

function pngSize(file) {
  const d = decodePng(fs.readFileSync(path.join(PUBLIC, file)));
  return { w: d.width, h: d.height, colorType: d.colorType };
}

test('manifest: valid JSON with the fields an installable PWA needs, matching the page', () => {
  const m = JSON.parse(read('manifest.webmanifest'));
  assert.equal(m.name, 'Mission Control');
  assert.equal(m.short_name, 'Mission Control');
  assert.equal(m.display, 'standalone');
  assert.equal(m.start_url, '/');
  assert.equal(m.scope, '/');
  assert.equal(m.id, '/');
  for (const k of ['theme_color', 'background_color']) assert.match(m[k], /^#[0-9a-f]{6}$/i, k);
  const ground = /--ground:\s*(#[0-9a-f]{6})/i.exec(read('css/app.css'))[1];
  assert.equal(m.background_color.toLowerCase(), ground.toLowerCase(), 'splash matches the page ground');
  const html = read('index.html');
  assert.match(html, new RegExp(`name="theme-color" content="${m.theme_color}"`));
  assert.match(html, /rel="manifest" href="\/manifest\.webmanifest"/);
  assert.match(html, /name="apple-mobile-web-app-title" content="Mission Control"/);
});

test('manifest icons: every file exists, PNG sizes are real, 192 + 512 "any" and a 512 maskable', () => {
  const m = JSON.parse(read('manifest.webmanifest'));
  assert.ok(Array.isArray(m.icons) && m.icons.length >= 3);
  for (const i of m.icons) {
    assert.ok(i.src.startsWith('/icons/'), i.src);
    assert.ok(fs.existsSync(path.join(PUBLIC, i.src)), i.src);
    assert.ok(['any', 'maskable'].includes(i.purpose), `${i.src}: purpose`);
    if (i.type === 'image/png') {
      const { w, h, colorType } = pngSize(i.src);
      assert.equal(i.sizes, `${w}x${h}`, `${i.src}: declared size is the real size`);
      assert.equal(colorType, 6, 'RGBA');
    } else {
      assert.equal(i.type, 'image/svg+xml');
      assert.equal(i.sizes, 'any');
    }
  }
  const has = (purpose, size) => m.icons.some((i) => i.type === 'image/png' && i.purpose === purpose && i.sizes === size);
  assert.ok(has('any', '192x192') && has('any', '512x512') && has('maskable', '512x512'));
  assert.deepEqual(pngSize('icons/apple-touch-icon.png'), { w: 180, h: 180, colorType: 6 });
  assert.deepEqual(pngSize('icons/badge-96.png'), { w: 96, h: 96, colorType: 6 });
});

test('icons: the committed files are exactly what scripts/make-icons.mjs draws (run npm run icons)', () => {
  for (const f of FILES) {
    const p = path.join(PUBLIC, 'icons', f.file);
    if (f.svg) assert.equal(fs.readFileSync(p, 'utf8'), svg(f.kind), f.file);
    else assert.ok(decodePng(fs.readFileSync(p)).rgba.equals(rasterize(f.kind, f.size)), f.file);
  }
});

test('icons: the maskable mark stays inside the 80% safe zone; the badge is white on transparent', () => {
  const d = decodePng(fs.readFileSync(path.join(PUBLIC, 'icons/maskable-512.png')));
  let corner = 0;
  for (let y = 0; y < 512; y++)
    for (let x = 0; x < 512; x++) {
      const i = (y * 512 + x) * 4;
      assert.equal(d.rgba[i + 3], 255, 'maskable is opaque (full bleed)');
      if (Math.hypot(x + 0.5 - 256, y + 0.5 - 256) > 0.4 * 512 && d.rgba[i + 2] > 0x50) corner++; // accent strokes are bright blue
    }
  assert.equal(corner, 0, 'no mark pixels outside the safe zone');
  const b = decodePng(fs.readFileSync(path.join(PUBLIC, 'icons/badge-96.png')));
  let opaque = 0;
  for (let i = 0; i < b.rgba.length; i += 4) {
    if (b.rgba[i + 3] > 0) {
      opaque++;
      assert.ok(b.rgba[i] === 255 && b.rgba[i + 1] === 255 && b.rgba[i + 2] === 255, 'badge pixels are white');
    }
  }
  assert.ok(opaque > 300 && opaque < 96 * 96 * 0.6, 'badge has a mark and transparent space');
});

test('CSS: focus-visible, reduced motion, 44 px targets, 16 px inputs (no iOS zoom), dark scheme', () => {
  const css = read('css/app.css');
  assert.match(css, /:focus-visible/);
  assert.match(css, /prefers-reduced-motion: reduce/);
  assert.match(css, /--target: 44px/);
  assert.match(css, /\.ask input \{[^}]*font-size: 16px/);
  assert.match(css, /color-scheme: dark/);
  assert.match(css, /\.sr-only/);
});

// WCAG 2 contrast on the real tokens.
function lum(hex) {
  const c = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255).map((v) => (v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4));
  return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
}
const ratio = (a, b) => {
  const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p);
  return (x + 0.05) / (y + 0.05);
};
function blend(fg, alpha, bg) {
  const ch = (h, i) => parseInt(h.slice(i, i + 2), 16);
  return `#${[1, 3, 5].map((i) => Math.round(ch(fg, i) * alpha + ch(bg, i) * (1 - alpha)).toString(16).padStart(2, '0')).join('')}`;
}

test('colour contrast: text on every surface it sits on is at least 4.5:1', () => {
  const css = read('css/app.css');
  const t = Object.fromEntries([...css.matchAll(/--([a-z0-9-]+):\s*(#[0-9a-f]{6})/gi)].map((m) => [m[1], m[2]]));
  const tint = (name, a, on) => blend(t[name], a, on);
  const pairs = [
    ['text', t.text, t.ground],
    ['text on card', t.text, t.surface],
    ['text-2 on card', t['text-2'], t.surface],
    ['muted on card', t.muted, t.surface],
    ['muted on ground', t.muted, t.ground],
    ['accent-text link on card', t['accent-text'], t.surface],
    ['white on primary button', '#ffffff', t['accent-strong']],
    ['ok badge', t.ok, tint('ok', 0.13, t.surface)],
    ['warn badge', t.warn, tint('warn', 0.13, t.surface)],
    ['down badge', t.fail, tint('fail', 0.14, t.surface)],
    ['text on error note', t['text-2'], tint('fail', 0.14, t.surface)],
    ['text on warning banner', t.text, tint('warn', 0.13, t.ground)],
    ['muted placeholder on input', t.muted, t.deep],
  ];
  for (const [name, fg, bg] of pairs) assert.ok(ratio(fg, bg) >= 4.5, `${name}: ${ratio(fg, bg).toFixed(2)}:1`);
});
