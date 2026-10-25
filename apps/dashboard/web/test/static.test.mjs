// The deployable tree (public/): reproducible vendoring, licenses, no CDN, CSP-safe markup, imports that
// resolve inside public/, a sane manifest.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { buildVendorFiles } from '../scripts/vendor.mjs';
import { stamp, shellFiles } from '../scripts/stamp-sw.mjs';
import { inlineCodeProblems } from './helpers.mjs';
import { ICONS } from '../public/js/icons.js';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const PUBLIC = path.join(ROOT, 'public');

function files(dir = PUBLIC) {
  const out = [];
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) out.push(...files(p));
    else out.push(p);
  }
  return out;
}
const rel = (p) => path.relative(PUBLIC, p).split(path.sep).join('/');
const TEXT = /\.(html|js|mjs|css|svg|webmanifest)$/;

test('vendored files match the lockfile-pinned packages (npm ci && npm run vendor reproduces them)', () => {
  const built = buildVendorFiles();
  const stale = [];
  for (const [r, content] of built) {
    const disk = path.join(PUBLIC, r);
    const buf = Buffer.isBuffer(content) ? content : Buffer.from(content, 'utf8');
    if (!fs.existsSync(disk) || !fs.readFileSync(disk).equals(buf)) stale.push(r);
  }
  assert.deepEqual(stale, [], 'run: npm ci && npm run vendor');
});

test('sw.js is stamped with the current shell (npm run stamp): a changed file means a new version', () => {
  assert.deepEqual(stamp(PUBLIC, { check: true }), { changed: false }, 'run: npm run stamp');
  const sw = fs.readFileSync(path.join(PUBLIC, 'sw.js'), 'utf8');
  const listed = JSON.parse(`[${/const SHELL = \[([\s\S]*?)\];/.exec(sw)[1].replace(/'/g, '"').replace(/,\s*$/, '')}]`);
  assert.deepEqual(listed, shellFiles(PUBLIC).map((f) => (f === 'index.html' ? '/' : `/${f}`)));
});

test('licenses ship next to the vendored code, fonts and icons', () => {
  const lic = (p) => fs.readFileSync(path.join(PUBLIC, p), 'utf8');
  assert.match(lic('fonts/LICENSE-inter.txt'), /SIL Open Font License, Version 1\.1/);
  assert.match(lic('icons/LICENSE-phosphor.txt'), /MIT License/);
  assert.match(lic('vendor/LICENSE-preact.txt'), /The MIT License/);
  assert.match(lic('vendor/LICENSE-htm.txt'), /Apache License/);
  for (const f of ['vendor/preact.mjs', 'vendor/hooks.mjs', 'vendor/htm.mjs', 'css/fonts.css', 'icons/phosphor.svg']) {
    assert.match(fs.readFileSync(path.join(PUBLIC, f), 'utf8').slice(0, 200), /\(MIT|\(Apache|OFL|LICENSE/i, `${f} names its license`);
  }
});

test('no CDN or any other origin at runtime', () => {
  // XML namespace identifiers (Preact, the sprite) are names, never fetched.
  const allowed = new Set(['http://www.w3.org/2000/svg', 'http://www.w3.org/1999/xhtml', 'http://www.w3.org/1998/Math/MathML']);
  const hits = [];
  for (const f of files().filter((p) => TEXT.test(p))) {
    const text = fs.readFileSync(f, 'utf8');
    for (const m of text.matchAll(/(?:https?:)?\/\/[a-z0-9.-]+\.[a-z]{2,}[^\s"'`)]*/gi)) {
      if (!allowed.has(m[0])) hits.push(`${rel(f)}: ${m[0]}`);
    }
  }
  assert.deepEqual(hits, []);
});

test('the CSP guard catches every way to sneak inline code into HTML', () => {
  const bad = {
    'inline script, closing tag with a space': '<script>alert(1)</script >',
    'inline script, closing tag with junk': '<script>alert(1)</script foo="x">',
    'inline script never closed': '<p>x</p><script>alert(1)',
    'empty inline script': '<script></script>',
    'src only as part of another attribute': '<script data-src="/a.js"></script>',
    'external script tag never finished': '<script src="/a.js"',
    'external tag never finished, after a closed one': '<script src="/a.js"></script><script src="/b.js"',
    'external tag never finished, after a stray closing tag': '</script><script src="/a.js"',
    'opening tag never finished': '<script type="module"',
    'upper-case inline script': '<SCRIPT>alert(1)</SCRIPT>',
    'inline module script': '<script type="module">import "./x.js"</script>',
    'second script inline': '<script src="/a.js"></script><script >alert(1)</script>',
    'external script with inline content': '<script src="/a.js">alert(1)</script >',
    'external script never closed': '<script src="/a.js">',
    'onload handler': '<body onload="go()">',
    'handler with spaces and odd case': '<img src="/x.png" OnError = "go()">',
    'handler in an unfinished tag': '<div onclick="go()"',
    'javascript: URL': '<a href="javascript:alert(1)">x</a>',
    'javascript: URL with spaces and case': '<a href="JavaScript :alert(1)">x</a>',
    'style element': '<style>p{}</style>',
    'style attribute': '<p style="color:red">x</p>',
  };
  for (const [name, html] of Object.entries(bad)) assert.notDeepEqual(inlineCodeProblems(html), [], `must be caught: ${name}`);
  const good = [
    '<script type="module" src="/js/main.js"></script>',
    '<script src=/js/a.js></script >',
    '<SCRIPT SRC="/js/a.js"></SCRIPT>',
    '<link rel="modulepreload" href="/js/boot.js"><meta name="description" content="online status">',
  ];
  for (const html of good) assert.deepEqual(inlineCodeProblems(html), [], html);
});

test('CSP-safe: no inline scripts, styles or handlers in the HTML; no style strings in templates', () => {
  const pages = files().filter((p) => p.endsWith('.html'));
  assert.ok(pages.length >= 1);
  for (const p of pages) assert.deepEqual(inlineCodeProblems(fs.readFileSync(p, 'utf8')), [], rel(p));
  for (const f of files(path.join(PUBLIC, 'js'))) {
    const text = fs.readFileSync(f, 'utf8');
    assert.doesNotMatch(text, /style="/, `${rel(f)}: use style=\${{...}} (CSSOM), never a style attribute string`);
    assert.doesNotMatch(text, /setAttribute\(\s*['"]style/, rel(f));
    assert.doesNotMatch(text, /\beval\(|new Function\(/, rel(f));
  }
});

test('index.html only references files that exist; the shell is absolute-path, same-origin', () => {
  const html = fs.readFileSync(path.join(PUBLIC, 'index.html'), 'utf8');
  const refs = [...html.matchAll(/\s(?:href|src)="([^"]+)"/g)].map((m) => m[1]);
  assert.ok(refs.length >= 10);
  for (const r of refs) {
    assert.match(r, /^\/[^/]/, `${r} is a same-origin absolute path`);
    assert.ok(fs.existsSync(path.join(PUBLIC, r)), `${r} exists`);
  }
  assert.match(html, /viewport-fit=cover/, 'safe areas');
  assert.match(html, /apple-mobile-web-app-capable/);
  assert.match(html, /apple-touch-icon/);
});

test('every module import resolves to a file inside public/ (no bare specifiers, no build step)', () => {
  const graph = [];
  for (const f of files().filter((p) => /\.(m?js)$/.test(p) && !p.endsWith('sw.js'))) {
    const text = fs.readFileSync(f, 'utf8');
    for (const m of text.matchAll(/(?:\bimport\b[^'"]*?|\bfrom\s*)['"]([^'"]+)['"]/g)) {
      const spec = m[1];
      assert.match(spec, /^\.\.?\//, `${rel(f)} imports "${spec}"`);
      const target = path.resolve(path.dirname(f), spec);
      assert.ok(target.startsWith(PUBLIC + path.sep) && fs.existsSync(target), `${rel(f)} → ${spec}`);
      graph.push(spec);
    }
  }
  assert.ok(graph.length > 40);
});

test('every icon named in the source is in the vendored sprite', () => {
  const sprite = fs.readFileSync(path.join(PUBLIC, 'icons/phosphor.svg'), 'utf8');
  for (const name of ICONS) assert.ok(sprite.includes(`<symbol id="${name}"`), name);
  const used = new Set();
  for (const f of files(path.join(PUBLIC, 'js'))) {
    const text = fs.readFileSync(f, 'utf8');
    for (const m of text.matchAll(/\b(?:icon|name)(?:=|:\s*)['"]([a-z][a-z0-9-]*[a-z0-9])['"]/g)) used.add(m[1]);
    for (const m of text.matchAll(/'([a-z]+(?:-[a-z0-9]+)+)'\s*:\s*'([a-z]+(?:-[a-z0-9]+)*)'/g)) used.add(m[2]);
  }
  const notIcons = new Set(['dashboard', 'app', 'apps', 'drive', 'media', 'alerts', 'uptime', 'settings', 'more', 'overview', 'jellyfin', 'jellyseerr', 'sonarr', 'radarr', 'prowlarr', 'sabnzbd', 'cloudflared', 'wol-relay', 'test', 'comment', 'ping', 'end', 'summary', 'stop', 'update', 'restart', 'start', 'remove', 'rerun', 'held']);
  const missing = [...used].filter((n) => !notIcons.has(n) && !ICONS.has(n) && /^[a-z-]+$/.test(n) && n.includes('-'));
  assert.deepEqual(missing, []);
});

test('manifest: same id and scope as push-core (installed apps keep their subscriptions), icons exist', () => {
  const m = JSON.parse(fs.readFileSync(path.join(PUBLIC, 'manifest.webmanifest'), 'utf8'));
  assert.equal(m.id, '/');
  assert.equal(m.scope, '/');
  assert.equal(m.start_url, '/');
  assert.equal(m.display, 'standalone');
  assert.equal(m.background_color, '#161826');
  for (const i of m.icons) assert.ok(fs.existsSync(path.join(PUBLIC, i.src)), i.src);
  assert.ok(m.icons.some((i) => i.purpose === 'maskable'));
});

test('the deployable tree holds only what the server should serve', () => {
  const unexpected = files()
    .map(rel)
    .filter((r) => !/^(index\.html|sw\.js|manifest\.webmanifest|css\/[\w-]+\.css|js\/(screens\/)?[\w-]+\.js|vendor\/[\w.-]+|fonts\/[\w.-]+|icons\/[\w.-]+)$/.test(r));
  assert.deepEqual(unexpected, []);
  assert.ok(!files().some((p) => path.basename(p).startsWith('.')), 'the server skips dotfiles');
});
