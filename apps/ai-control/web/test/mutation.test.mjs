// Mutation check: copies public/ to a temp dir, applies one mutation, and runs the test file that should
// catch it against the copy. Each mutant must FAIL; the unmutated copy must PASS every guarding test (so a
// failure really comes from the mutation).
// - hostile.test.mjs: a textContent-style render swapped for innerHTML / insertAdjacentHTML;
// - sw.test.mjs: a non-200 summary saved as the snapshot;
// - dom.test.mjs: a dangerous attribute added to dom.js's allowlist.

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

const MUTANTS = [
  {
    name: 'every Text node built with innerHTML (dom.js text())',
    file: 'js/dom.js',
    from: "return document.createTextNode(typeof s === 'string' ? s : String(s ?? ''));",
    to: "const t = document.createElement('span'); t.innerHTML = typeof s === 'string' ? s : String(s ?? ''); return t;",
  },
  {
    name: 'log lines via innerHTML',
    file: 'js/tabs/logs.js',
    from: "h('pre', { class: 'log-out', id: 'log-out', tabindex: '0', 'aria-label': 'Log lines' }, shown.join('\\n'))",
    to: "__ih('pre', 'log-out', shown.join('\\n'))",
    prelude: IH,
  },
  {
    name: 'replay output via innerHTML',
    file: 'js/tabs/audit.js',
    from: "h('pre', { class: 'replay-out', id: 'replay-output', tabindex: '0', 'aria-label': 'Session output' }, output || '(no output)')",
    to: "__ih('pre', 'replay-out', output || '(no output)')",
    prelude: IH,
  },
  {
    name: 'alert text via innerHTML',
    file: 'js/tabs/overview.js',
    from: "h('p', null, str(a.text, 'An alert with no text.'))",
    to: "__ih('p', 'x', str(a.text, 'An alert with no text.'))",
    prelude: IH,
  },
  {
    name: 'model name via insertAdjacentHTML',
    file: 'js/tabs/models.js',
    from: "h('p', { class: 'mono model-name' }, str(m.name))",
    to: "(() => { const e = h('p', { class: 'mono model-name' }); e.insertAdjacentHTML('beforeend', str(m.name)); return e; })()",
  },
  {
    name: 'log source labels via innerHTML',
    file: 'js/tabs/logs.js',
    from: "h('option', { value: String(i) }, it.label)",
    to: "__ih('option', 'x', it.label)",
    prelude: IH,
  },
  {
    name: 'service worker saves any summary reply, not just a 200',
    file: 'sw.js',
    from: 'if (res.status === 200) {',
    to: 'if (true) {',
    test: 'sw.test.mjs',
    expect: /only a 200|snapshot is still the 200 summary|Expected values to be strictly/,
  },
  {
    name: 'href added to the dom.js attribute allowlist',
    file: 'js/dom.js',
    from: "'id', 'role',",
    to: "'id', 'href', 'role',",
    test: 'dom.test.mjs',
    expect: /allowlists are exactly the expected sets/,
  },
  {
    name: 'onclick added to the dom.js SVG allowlist',
    file: 'js/dom.js',
    from: "const SVG_ATTRS = new Set(['viewBox',",
    to: "const SVG_ATTRS = new Set(['onclick', 'viewBox',",
    test: 'dom.test.mjs',
    expect: /allowlists are exactly the expected sets/,
  },
  {
    name: 'sudo command line via innerHTML',
    file: 'js/tabs/audit.js',
    from: "h('p', { class: 'cmd-line mono' }, str(c.command, '(no command recorded)'))",
    to: "__ih('p', 'cmd-line mono', str(c.command, '(no command recorded)'))",
    prelude: IH,
  },
  {
    name: 'identity via innerHTML',
    file: 'js/shell.js',
    from: "h('span', { class: 'who-name' }, name)",
    to: "__ih('span', 'who-name', name)",
    prelude: IH,
  },
];

function copyWith(mutant) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'aictl-mut-'));
  fs.cpSync(PUBLIC, dir, { recursive: true });
  if (mutant) {
    const p = path.join(dir, mutant.file);
    const src = fs.readFileSync(p, 'utf8');
    assert.ok(src.includes(mutant.from), `mutation target not found in ${mutant.file}; update mutation.test.mjs`);
    let next = src.replace(mutant.from, mutant.to);
    if (mutant.prelude) {
      // after the last import line
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

const HOSTILE_EXPECT = /unexpected <(img|script)> element|attribute .* carries server text|event-handler attribute/;

function run(testFile, dir, keep = false) {
  const env = { ...process.env, AICTL_WEB_PUBLIC: dir };
  delete env.NODE_TEST_CONTEXT;
  const r = spawnSync(process.execPath, ['--test', '--test-reporter=tap', path.join(HERE, testFile)], { env, encoding: 'utf8', timeout: 60_000 });
  if (!keep) fs.rmSync(dir, { recursive: true, force: true });
  return r;
}

test('control: the unmutated copy passes every guarding test', () => {
  const dir = copyWith(null);
  try {
    for (const f of new Set(MUTANTS.map((m) => m.test || 'hostile.test.mjs'))) {
      const r = run(f, dir, true);
      assert.equal(r.status, 0, `${f}: ${r.stdout}${r.stderr}`);
    }
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

for (const m of MUTANTS) {
  test(`mutant is caught: ${m.name}`, () => {
    const file = m.test || 'hostile.test.mjs';
    const r = run(file, copyWith(m));
    assert.notEqual(r.status, 0, `${file} passed against the mutant "${m.name}"`);
    assert.match(r.stdout, m.expect || HOSTILE_EXPECT);
  });
}
