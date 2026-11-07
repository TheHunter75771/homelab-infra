// The test DOM's HTML parser (dom-shim.mjs): what it builds from markup, and that it stays linear. It parses
// the hostile strings in the mutation check, and its tag pattern once backtracked exponentially (CodeQL js/redos)
// on `<A !=` + `"" !=` x N. The timing runs in a child process, so a pattern that hangs is killed and reported
// instead of hanging the suite (a regex can't be interrupted by a test timeout).

import test from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { installDom, parseHTML } from './dom-shim.mjs';

const doc = installDom();
const tree = (n) => (n.nodeType === 3 ? n.data : [n.localName, Object.fromEntries(n.attributes), ...n.childNodes.map(tree)]);
const parse = (html) => parseHTML(doc, html).map(tree);

test('parser: tags, attributes (quoted, unquoted, bare), entities, void and self-closing tags, end tags', () => {
  assert.deepEqual(parse('<img src=x onerror=alert(1)> javascript:alert(1) "><script>alert(2)</script>'), [
    ['img', { src: 'x', onerror: 'alert(1)' }],
    ' javascript:alert(1) ">',
    ['script', {}, 'alert(2)'],
  ]);
  assert.deepEqual(parse(`<A Title="a &amp; <b>" data-x='1 "2"' hidden href = https://h.example.test/?a=b&amp;c=d>t</a>`), [
    ['a', { title: 'a & <b>', 'data-x': '1 "2"', hidden: '', href: 'https://h.example.test/?a=b&c=d' }, 't'],
  ]);
  assert.deepEqual(parse('<p>a<br>b<span/>c</p>d'), [['p', {}, 'a', ['br', {}], 'b', ['span', {}], 'c'], 'd']);
  assert.deepEqual(parse('<b><i>x</b>y</i>'), [['b', {}, ['i', {}, 'x']], 'y']);
  assert.deepEqual(parse('<a <img src=x>'), ['<a ', ['img', { src: 'x' }]]); // a `<` outside quotes ends a tag
});

test('parser: a comment ends at --> or --!>, at once for <!--> and <!--->, or at the end of the input (WHATWG)', () => {
  const img = ['img', { src: 'x', onerror: 'alert(1)' }];
  assert.deepEqual(parse('a<!-- <img src=x> -->b'), ['a', 'b']);
  assert.deepEqual(parse('a<!-- <b> --->c<!---->d'), ['a', 'c', 'd']);
  // what follows an abruptly or incorrectly closed comment is markup again, as in a browser
  assert.deepEqual(parse('<!--><img src=x onerror=alert(1)>'), [img]);
  assert.deepEqual(parse('<!---><img src=x onerror=alert(1)>'), [img]);
  assert.deepEqual(parse('<!-- x --!><img src=x onerror=alert(1)>'), [img]);
  assert.deepEqual(parse('a<!-- <img src=x onerror=alert(1)>'), ['a']); // never closed
});

// [what, prefix, repeated part, repetitions, suffix]. The first is the CodeQL report; the rest are inputs that
// made the old pattern (or a looser fix that let quotes into names or unquoted values) quadratic or worse.
const SHAPES = [
  ['<A != then "" != x N (CodeQL js/redos)', '<A !=', '"" !=', 50_000, ''],
  ['an unclosed tag of <-names', '<a ', '<a ', 50_000, ''],
  ['unclosed double-quoted values', '', '<a b="', 50_000, ''],
  ['unquoted values running into a <', '', '<a b=c', 50_000, ''],
  ['a closing quote read as a name', '/>', " b='<a '", 50_000, '=z'],
  ['a quote inside an unquoted value', " x'", `x' b=' b="<a=z<a b=`, 20_000, ''],
  ['unclosed comments', '', '<!--', 50_000, ''],
  ['comments closed at once', '', '<!--><!--->', 40_000, ''],
  ['a long run of whitespace', '<a', ' ', 200_000, 'x'],
];

const CHILD = `
const { installDom, parseHTML } = await import(process.argv[1]);
const doc = installDom();
const out = [];
for (const [what, pre, part, n, suf] of JSON.parse(process.argv[2])) {
  const html = pre + part.repeat(n) + suf;
  const t = performance.now();
  const nodes = parseHTML(doc, html);
  out.push({ what, ms: performance.now() - t, length: html.length, nodes: nodes.length, allText: nodes.every((x) => x.nodeType === 3) && nodes.map((x) => x.data).join('') === html });
}
console.log(JSON.stringify(out));`;

test('parser: linear time on inputs that make a backtracking tag pattern blow up (each under 1 s)', () => {
  const r = spawnSync(process.execPath, ['--input-type=module', '-e', CHILD, new URL('./dom-shim.mjs', import.meta.url).href, JSON.stringify(SHAPES)], {
    encoding: 'utf8',
    timeout: 30_000,
  });
  assert.equal(r.signal, null, `the parser did not finish in 30 s (killed with ${r.signal}): super-linear backtracking?`);
  assert.equal(r.status, 0, r.stderr);
  const results = JSON.parse(r.stdout);
  assert.deepEqual(results.map((x) => x.what), SHAPES.map((s) => s[0]));
  for (const x of results) {
    assert.ok(x.length >= 100_000, `${x.what}: a big input (${x.length} chars)`);
    assert.ok(x.ms < 1000, `${x.what}: ${x.ms.toFixed(1)} ms for ${x.length} chars`);
  }
  // The CodeQL input has no closing `>`: no tag, the whole input is text, unchanged.
  assert.equal(results[0].allText, true);
});
