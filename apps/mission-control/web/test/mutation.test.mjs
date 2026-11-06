// Mutation check: copies public/ to a temp dir, applies one mutation, and runs the test file that should
// catch it against the copy (MC_WEB_PUBLIC). Each mutant must FAIL with the expected test; the unmutated copy
// must PASS every guarding test, so a failure really comes from the mutation.
//   hostile.test.mjs  card text rendered with innerHTML / insertAdjacentHTML
//   model.test.mjs    the deep link without URL encoding (or with encodeURI); safeHref letting any scheme in
//   sw.test.mjs       the worker ignoring notificationclick, opening the root instead of the card, not telling
//                     an open window, saving error replies as the offline copy
//   render.test.mjs   a card's error state not rendered
//   push.test.mjs     a 410 from the server not treated as "subscription gone" (the panel stays On); unsubscribe
//                     swallowing a server 500; window.open without noopener
//   sw.test.mjs       (also) the worker answering cross-origin requests

import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PUBLIC = path.resolve(HERE, '..', 'public');
const IH = "const __ih = (tag, cls, s) => { const e = document.createElement(tag); e.setAttribute('class', cls); e.innerHTML = String(s); return e; };\n";
const HOSTILE_EXPECT = /unexpected <(img|script)> element|attribute .* carries server text|event-handler attribute/;

const MUTANTS = [
  {
    name: 'card headline via innerHTML',
    file: 'js/view.js',
    from: "parts.push(h('p', { class: 'headline' }, card.headline))",
    to: "parts.push(__ih('p', 'headline', card.headline))",
    prelude: IH,
  },
  {
    name: 'item labels via insertAdjacentHTML',
    file: 'js/view.js',
    from: "h('span', { class: 'item-label' }, it.label),",
    to: "(() => { const e = h('span', { class: 'item-label' }); e.insertAdjacentHTML('beforeend', it.label); return e; })(),",
  },
  {
    name: 'every Text node built with innerHTML (dom.js text())',
    file: 'js/dom.js',
    from: "return document.createTextNode(typeof s === 'string' ? s : String(s ?? ''));",
    to: "const t = document.createElement('span'); t.innerHTML = typeof s === 'string' ? s : String(s ?? ''); return t;",
  },
  {
    name: 'briefing section titles via innerHTML',
    file: 'js/view.js',
    from: "h('h3', null, sec.title)",
    to: "__ih('h3', 'x', sec.title)",
    prelude: IH,
  },
  {
    name: 'error notes via innerHTML',
    file: 'js/view.js',
    from: "h('span', null, ...children));",
    to: "__ih('span', 'x', children.join('')));",
    prelude: IH,
  },
  {
    name: 'deep link question not URL-encoded',
    file: 'js/model.js',
    from: '`&q=${encodeURIComponent(q)}`',
    to: '`&q=${q}`',
    test: 'model.test.mjs',
    expect: /not ok \d+ - deep link/,
  },
  {
    name: 'deep link encoded with encodeURI (leaves & # ? + unescaped)',
    file: 'js/model.js',
    from: '`&q=${encodeURIComponent(q)}`',
    to: '`&q=${encodeURI(q)}`',
    test: 'model.test.mjs',
    expect: /not ok \d+ - deep link/,
  },
  {
    name: 'safeHref lets any scheme through (javascript:, data:)',
    file: 'js/model.js',
    from: "if (u.protocol !== 'https:' && u.protocol !== 'http:') return null;",
    to: '',
    test: 'model.test.mjs',
    expect: /not ok \d+ - (safeHref|deep link: a bad base)/,
  },
  {
    name: 'service worker ignores notificationclick',
    file: 'sw.js',
    from: "self.addEventListener('notificationclick',",
    to: "self.addEventListener('notificationclick-ignored',",
    test: 'sw.test.mjs',
    expect: /not ok \d+ - notificationclick/,
  },
  {
    name: 'notification tap opens the root, not the card',
    file: 'sw.js',
    from: 'return self.clients.openWindow(t.url);',
    to: "return self.clients.openWindow('/');",
    test: 'sw.test.mjs',
    expect: /not ok \d+ - notificationclick with no window open/,
  },
  {
    name: 'notification tap with the app open does not ask it to show the card',
    file: 'sw.js',
    from: "if (t.card && typeof client.postMessage === 'function') client.postMessage({ type: 'open-card', card: t.card });",
    to: '',
    test: 'sw.test.mjs',
    expect: /not ok \d+ - notificationclick with the app open/,
  },
  {
    name: 'service worker saves any reply as the offline copy, not just a 200',
    file: 'sw.js',
    from: 'if (res.status === 200) {',
    to: 'if (true) {',
    test: 'sw.test.mjs',
    expect: /not ok \d+ - \/api\/(cards|briefing): a 200 object/,
  },
  {
    name: "a card's error is not rendered",
    file: 'js/view.js',
    from: 'if (card.error) parts.push(',
    to: 'if (false) parts.push(',
    test: 'render.test.mjs',
    expect: /not ok \d+ - error states/,
  },
  {
    name: 'a 410/404 from the server is not recognised as "subscription gone"',
    file: 'js/push.js',
    from: "return !r.ok && r.fail.kind === 'http' && (r.fail.status === 410 || r.fail.status === 404);",
    to: 'return false;',
    test: 'push.test.mjs',
    expect: /not ok \d+ - (test push answered 410|panel: "Send a test" answered 410)/,
  },
  {
    name: 'the controller ignores "gone": a 410 leaves the panel showing On',
    file: 'js/app.js',
    from: 'if (e && e.gone === true) {',
    to: 'if (false) {',
    test: 'push.test.mjs',
    expect: /not ok \d+ - panel: "Send a test" answered 410/,
  },
  {
    name: 'service worker answers (and caches) cross-origin requests',
    file: 'sw.js',
    from: 'if (url.origin !== self.location.origin) return;',
    to: '',
    test: 'sw.test.mjs',
    expect: /not ok \d+ - cross-origin requests are left to the network/,
  },
  {
    name: 'unsubscribe swallows server errors',
    file: 'js/push.js',
    from: "if (!r.ok && !isGone(r)) throw new Error(serverSaid(r, 'remove this device'));",
    to: '',
    test: 'push.test.mjs',
    expect: /not ok \d+ - (unsubscribe: a server 500|panel: "Turn off" answered 500)/,
  },
  {
    name: 'window.open without noopener',
    file: 'js/app.js',
    from: "win.open(url, '_blank', 'noopener,noreferrer')",
    to: "win.open(url, '_blank')",
    test: 'push.test.mjs',
    expect: /not ok \d+ - every external open is noopener,noreferrer/,
  },
];

function copyWith(mutant) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'mc-mut-'));
  fs.cpSync(PUBLIC, dir, { recursive: true });
  if (mutant) {
    const p = path.join(dir, mutant.file);
    const src = fs.readFileSync(p, 'utf8');
    assert.ok(src.includes(mutant.from), `mutation target not found in ${mutant.file}; update mutation.test.mjs`);
    let next = src.replace(mutant.from, mutant.to);
    if (mutant.prelude) {
      const lines = next.split('\n');
      let last = -1;
      lines.forEach((l, i) => {
        if (l.startsWith('import ')) last = i;
      });
      lines.splice(last + 1, 0, mutant.prelude);
      next = lines.join('\n');
    }
    fs.writeFileSync(p, next);
  }
  return dir;
}

function run(testFile, dir) {
  const env = { ...process.env, MC_WEB_PUBLIC: dir };
  delete env.NODE_TEST_CONTEXT;
  return spawnSync(process.execPath, ['--test', '--test-reporter=tap', '--test-timeout=20000', path.join(HERE, testFile)], { env, encoding: 'utf8', timeout: 60_000 });
}

test('control: the unmutated copy passes every guarding test', () => {
  const dir = copyWith(null);
  try {
    for (const f of new Set(MUTANTS.map((m) => m.test || 'hostile.test.mjs'))) {
      const r = run(f, dir);
      assert.equal(r.status, 0, `${f}: ${r.stdout}${r.stderr}`);
    }
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

for (const m of MUTANTS) {
  test(`mutant is caught: ${m.name}`, () => {
    const file = m.test || 'hostile.test.mjs';
    const dir = copyWith(m);
    try {
      const r = run(file, dir);
      assert.notEqual(r.status, 0, `${file} passed against the mutant "${m.name}"`);
      assert.match(r.stdout, m.expect || HOSTILE_EXPECT);
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });
}
