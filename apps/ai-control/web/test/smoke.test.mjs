// Renders the shell and every tab from fake summaries (normal, gaming → CPU, PC off, everything down) in
// the DOM shim, and checks what Noah would read on each.

import test from 'node:test';
import assert from 'node:assert/strict';
import { loadModules, buildState, renderAll, fx } from './render.mjs';
import { allElements } from './dom-shim.mjs';

const mods = await loadModules();
const SCENARIOS = {
  normal: fx.normal,
  'gaming → CPU': fx.gamingCpu,
  'PC off': fx.pcOff,
  'everything down': fx.everythingDown,
};

function texts(root) {
  return root.textContent;
}

for (const [name, make] of Object.entries(SCENARIOS)) {
  test(`every tab renders: ${name}`, () => {
    const roots = renderAll(mods, buildState(mods, make()));
    for (const [tab, root] of Object.entries(roots)) {
      const t = texts(root);
      assert.ok(t.length > 20, `${tab} has content`);
      assert.doesNotMatch(t, /undefined|NaN|\[object Object\]|null/, `${tab} shows no raw JS values`);
    }
  });
}

test('normal: GPU, no game, services OK, sparkline with a text alternative', () => {
  const r = renderAll(mods, buildState(mods, fx.normal()));
  assert.match(r.overview.textContent, /Answers are going to the gaming PC's GPU because no game is running\./);
  assert.match(r.overview.textContent, /GPU gate.*OK/);
  const svg = allElements(r.overview).find((e) => e.localName === 'svg' && e.getAttribute('class') === 'spark');
  assert.ok(svg, 'sparkline svg present');
  assert.equal(svg.getAttribute('role'), 'img');
  const cap = allElements(r.overview).find((e) => e.id === svg.getAttribute('aria-labelledby'));
  assert.ok(cap, 'svg is labelled by its caption');
  assert.match(cap.textContent, /^Last 30 min: game GPU use is \d+% now, peaked at \d+%; the 30 min average is \d+% \(limit 50%\)\./);
  assert.match(r.overview.textContent, /41\.7 tok\/s/);
  assert.match(r.overview.textContent, /llama3\.2:3bCPU/, 'alias null → the model name is shown');
  assert.match(r.overview.textContent, /Failed 502/);
  assert.match(r.models.textContent, /On the GPU.*llama3\.1:8b.*Installed/);
  assert.match(r.models.textContent, /Loaded/);
  assert.match(r.shell.textContent, /Test User/);
  assert.match(r.logs.textContent, /3 lines/);
  assert.match(r.logs.textContent, /homelab-ai-digest\.timer.*Last run OK/);
  assert.match(r.audit.textContent, /systemctl status ollama/);
});

test('gaming → CPU: the sentence names the game, the average and the limit', () => {
  const r = renderAll(mods, buildState(mods, fx.gamingCpu()));
  const want = "Answers are going to ai-box's CPU because a game is running (Cyberpunk2077.exe) and the GPU has averaged 62% over 30 min (limit 50%).";
  assert.ok(r.overview.textContent.includes(want));
  assert.ok(r.routing.textContent.includes(want));
  assert.match(r.routing.textContent, /Game runningCyberpunk2077\.exe/);
  assert.match(r.overview.textContent, /No model is loaded on the GPU\./);
});

test('PC off: gate unreachable explained, PC sections show their errors, alerts shown', () => {
  const r = renderAll(mods, buildState(mods, fx.pcOff()));
  assert.match(r.overview.textContent, /Answers are going to ai-box's CPU because the GPU gate isn't answering \(timed out after 4 s\)/);
  assert.match(r.overview.textContent, /Gaming PC disks couldn't be loaded: host agent not answering/);
  assert.match(r.overview.textContent, /WarningThe GPU gate is unreachable/);
  assert.match(r.models.textContent, /Gaming PC \(GPU\) model list couldn't be loaded/);
  assert.match(r.models.textContent, /Can't check/);
  assert.match(r.overview.textContent, /Gaming PC Ollama \(GPU\)Down/);
});

test('everything down: each section fails on its own, nothing crashes', () => {
  const r = renderAll(mods, buildState(mods, fx.everythingDown(), { history: [], requests: [] }));
  assert.match(r.overview.textContent, /Criticalai-box Ollama is down/);
  assert.match(r.overview.textContent, /No answers.*The router \(broker\) is down\. AI answers aren't available\./);
  assert.match(r.overview.textContent, /Resources couldn't be loaded: proc read failed/);
  assert.match(r.models.textContent, /Models couldn't be loaded: broker not answering/);
  assert.match(r.logs.textContent, /Scheduled jobs couldn't be loaded/);
  assert.match(r.overview.textContent, /No GPU history yet\./);
  assert.doesNotMatch(r.shell.textContent, /Test User/, 'no identity shown when there is none');
});

test('status is never colour alone: every badge has an icon and a word', () => {
  const r = renderAll(mods, buildState(mods, fx.pcOff()));
  for (const root of Object.values(r)) {
    for (const b of allElements(root).filter((e) => (e.getAttribute('class') || '').split(' ').includes('badge'))) {
      assert.ok(b.children.some((c) => c.localName === 'svg'), 'badge has an icon');
      assert.ok(b.textContent.trim().length > 1, 'badge has a word');
    }
  }
});

test('audit replay: command line by default, then output in a <pre> with the cap notice', () => {
  const session = fx.sessions()[0];
  let r = renderAll(mods, buildState(mods, fx.normal(), { replay: { session, data: fx.replay(false) } }));
  assert.match(r.audit.textContent, /apt-get update/);
  assert.ok(allElements(r.audit).some((e) => e.localName === 'button' && e.textContent === 'Show output'));
  assert.ok(!allElements(r.audit).some((e) => e.getAttribute('class') === 'replay-out'), 'no output until asked');
  r = renderAll(mods, buildState(mods, fx.normal(), { replay: { session, output: true, data: fx.replay(true) } }));
  const pre = allElements(r.audit).find((e) => e.getAttribute('class') === 'replay-out');
  assert.equal(pre.localName, 'pre');
  assert.match(pre.textContent, /Reading package lists/);
  assert.match(r.audit.textContent, /Replays are capped at 262 KB; this one is complete\./);
  r = renderAll(mods, buildState(mods, fx.normal(), { replay: { session, output: true, data: { ...fx.replay(true), output_truncated: true, output_damaged: true } } }));
  assert.match(r.audit.textContent, /Output was cut off\. The full recording stays on ai-box\./);
  assert.doesNotMatch(r.audit.textContent, /262 KB; this one is complete/);
  assert.match(r.audit.textContent, /Damaged Part of this recording couldn't be read/);
});

test('audit: invalid session ids get no Replay button; the PC has no session list', () => {
  let r = renderAll(mods, buildState(mods, fx.normal()));
  const buttons = allElements(r.audit).filter((e) => e.localName === 'button' && e.textContent === 'Replay');
  assert.equal(buttons.length, 1, 'only the valid id');
  assert.match(r.audit.textContent, /Can't replay/);
  const { reduce } = mods.state;
  const s = reduce(buildState(mods, fx.normal()), { type: 'audit/options', host: 'pc' });
  r = renderAll(mods, s);
  assert.match(r.audit.textContent, /kept on ai-box only/);
});

test('audit: refused and wrong-password rows stand out with an icon + word, and a count leads the list', () => {
  const r = renderAll(mods, buildState(mods, fx.normal(), { sudo: fx.sudoFlagged() }));
  const t = r.audit.textContent;
  assert.match(t, /Attention 3 refused, 1 wrong-password attempt in this period\./);
  const rows = allElements(r.audit).filter((e) => (e.getAttribute('class') || '').split(' ').includes('cmd-flagged'));
  assert.equal(rows.length, 4, 'every non-command row is flagged');
  const words = rows.map((row) => allElements(row).find((e) => (e.getAttribute('class') || '').split(' ').includes('badge')));
  assert.deepEqual(words.map((b) => b.textContent), ['Refused', 'Refused', 'Refused', 'Wrong password']);
  assert.ok(words.every((b) => b.children.some((c) => c.localName === 'svg')), 'each badge has an icon');
  assert.notEqual(words[0].getAttribute('class'), words[3].getAttribute('class'), 'refused and wrong password look different');
  const plain = renderAll(mods, buildState(mods, fx.normal()));
  assert.match(plain.audit.textContent, /All 1 entry was an allowed command\./);
  assert.equal(allElements(plain.audit).filter((e) => (e.getAttribute('class') || '').includes('cmd-flagged')).length, 0);
  const odd = renderAll(mods, buildState(mods, fx.normal(), { sudo: [{ ...fx.sudo()[0], event: 'something_new' }] }));
  assert.match(odd.audit.textContent, /Unusual entry/);
  assert.match(odd.audit.textContent, /1 unusual entry in this period/);
});

test('audit focus: Replay moves focus to the replay heading; Show output moves it to the output', async () => {
  const { installDom } = await import('./dom-shim.mjs');
  const doc = installDom();
  const { reduce } = mods.state;
  const tab = mods.tabs.audit({});
  doc.body.appendChild(tab.el);
  let s = buildState(mods, fx.normal());
  tab.update(s, fx.NOW);
  const session = fx.sessions()[0];
  s = reduce(s, { type: 'replay/open', session });
  tab.update(s, fx.NOW);
  assert.equal(doc.activeElement && doc.activeElement.id, 'au-replay-h');
  assert.equal(doc.activeElement.getAttribute('tabindex'), '-1');
  s = reduce(s, { type: 'replay/loaded', session, output: false, data: { ...fx.replay(false), output: null } });
  tab.update(s, fx.NOW);
  assert.equal(doc.activeElement.id, 'au-replay-h', 'the command-only reply does not move focus');
  const show = doc.getElementById('replay-show-output');
  show.focus();
  s = reduce(s, { type: 'replay/output' });
  tab.update(s, fx.NOW);
  const busy = doc.getElementById('replay-show-output');
  assert.ok(busy, 'the button keeps its id while loading, so focus can stay on it');
  assert.equal(busy.getAttribute('aria-disabled'), 'true');
  s = reduce(s, { type: 'replay/loaded', session, output: true, data: fx.replay(true) });
  tab.update(s, fx.NOW);
  assert.equal(doc.activeElement.id, 'replay-output');
  assert.equal(doc.activeElement.localName, 'pre');
  // Back to the list, open again, and this time the output request fails.
  s = reduce(s, { type: 'replay/close' });
  tab.update(s, fx.NOW);
  s = reduce(s, { type: 'replay/open', session });
  tab.update(s, fx.NOW);
  s = reduce(s, { type: 'replay/output' });
  tab.update(s, fx.NOW);
  doc.getElementById('replay-show-output').focus();
  s = reduce(s, { type: 'replay/failed', session, error: 'HTTP 500' });
  tab.update(s, fx.NOW);
  assert.equal(doc.activeElement.id, 'au-replay-h', 'if the output fails, focus goes to the heading');
});

test('audit: ambiguous rows get "Check session recording" (icon + word) with help text, and count in the summary', () => {
  const r = renderAll(mods, buildState(mods, fx.normal(), { sudo: fx.sudoAmbiguous() }));
  const t = r.audit.textContent;
  assert.match(t, /Attention 1 refused, 2 need checking in this period\./);
  const sudoCard = allElements(r.audit).find((e) => e.getAttribute('aria-labelledby') === 'au-sudo-h');
  const rows = allElements(sudoCard).filter((e) => e.localName === 'li');
  assert.equal(rows.length, 5);
  const checks = rows.map((row) => allElements(row).filter((e) => (e.getAttribute('class') || '').split(' ').includes('badge') && e.textContent === 'Check session recording'));
  assert.deepEqual(checks.map((c) => c.length), [1, 1, 0, 0, 0], 'only ambiguous === true; "true" and 1 count as false');
  for (const [b] of checks.slice(0, 2)) assert.ok(b.children.some((c) => c.localName === 'svg'), 'the badge has an icon');
  assert.equal(t.split("This line's details can't be trusted on their own. The recorded session is authoritative.").length - 1, 2);
  assert.ok(allElements(rows[1]).some((e) => e.textContent === 'Refused'), 'a refused row keeps its Refused badge too');
  assert.ok((rows[0].getAttribute('class') || '').includes('cmd-flagged'));
  const one = renderAll(mods, buildState(mods, fx.normal(), { sudo: [fx.sudoAmbiguous()[0]] }));
  assert.match(one.audit.textContent, /Attention 1 needs checking in this period\./);
  const none = renderAll(mods, buildState(mods, fx.normal(), { sudo: fx.sudoAmbiguous().slice(2) }));
  assert.match(none.audit.textContent, /All 3 entries were allowed commands\./);
});

test('audit: sessions come wrapped with a skipped count, shown only when > 0', () => {
  let r = renderAll(mods, buildState(mods, fx.normal(), { sessions: fx.sessionsReply(3) }));
  assert.match(r.audit.textContent, /Skipped 3 session logs couldn't be read and were skipped\./);
  assert.match(r.audit.textContent, /apt-get update/, 'the readable sessions still show');
  r = renderAll(mods, buildState(mods, fx.normal(), { sessions: fx.sessionsReply(1) }));
  assert.match(r.audit.textContent, /1 session log couldn't be read and was skipped\./);
  for (const skipped of [0, -2, '5', 2.5, null, undefined]) {
    r = renderAll(mods, buildState(mods, fx.normal(), { sessions: { sessions: fx.sessions(), skipped } }));
    assert.doesNotMatch(r.audit.textContent, /skipped/, `skipped=${String(skipped)} shows nothing`);
  }
  r = renderAll(mods, buildState(mods, fx.normal(), { sessions: { sessions: [], skipped: 4 } }));
  assert.match(r.audit.textContent, /4 session logs couldn't be read.*No sessions in this period\./);
});

test('replay 422 (damaged session log): the error shows, no Show output, focus on the heading', async () => {
  const { installDom } = await import('./dom-shim.mjs');
  const doc = installDom();
  const { reduce } = mods.state;
  const tab = mods.tabs.audit({});
  doc.body.appendChild(tab.el);
  let s = buildState(mods, fx.normal());
  tab.update(s, fx.NOW);
  const session = fx.sessions()[0];
  s = reduce(s, { type: 'replay/open', session });
  tab.update(s, fx.NOW);
  doc.getElementById('replay-close').focus(); // focus elsewhere in the panel while it loads
  s = reduce(s, { type: 'replay/failed', session, error: 'this session log is damaged' });
  tab.update(s, fx.NOW);
  assert.match(tab.el.textContent, /The replay couldn't be loaded: this session log is damaged/);
  assert.equal(doc.getElementById('replay-show-output'), null, 'no Show output for an unreadable session');
  assert.equal(doc.activeElement.id, 'au-replay-h');
});

test('logs: client-side filter and counts', () => {
  const { reduce } = mods.state;
  const s = reduce(buildState(mods, fx.normal()), { type: 'logs/filter', filter: 'ERROR' });
  const r = renderAll(mods, s);
  assert.match(r.logs.textContent, /1 of 3 lines match/);
  const pre = allElements(r.logs).find((e) => e.localName === 'pre');
  assert.equal(pre.textContent, '2026-10-26T19:58:02Z ollama[100]: error: context full');
  const none = renderAll(mods, reduce(s, { type: 'logs/filter', filter: 'zzz' }));
  assert.match(none.logs.textContent, /No lines match the filter\./);
  const auto = allElements(r.logs).find((e) => e.id === 'log-auto');
  assert.equal(auto.checked, false, 'auto-refresh off by default');
});

test('logs: the source list comes from the server; if it fails, the error shows and there is no fallback', () => {
  let r = renderAll(mods, buildState(mods, fx.normal()));
  const opts = allElements(r.logs).filter((e) => e.localName === 'option' && e.parentNode.id === 'log-source');
  assert.deepEqual(opts.map((o) => o.textContent), ['ai-box Ollama (CPU)', 'Gaming PC Ollama (GPU)', 'AI stack']);
  assert.doesNotMatch(r.logs.textContent, /homelab-ai-stack-start|Docker|Tailscale/, 'no hard-coded sources');
  r = renderAll(mods, buildState(mods, fx.normal(), { logSourcesError: 'HTTP 502' }));
  assert.match(r.logs.textContent, /The list of log sources couldn't be loaded: HTTP 502/);
  const sel = allElements(r.logs).find((e) => e.id === 'log-source');
  assert.equal(sel.childNodes.length, 0, 'no options');
  assert.equal(sel.hidden, true);
  assert.doesNotMatch(r.logs.textContent, /lines$/);
});

test('replay with output=0 sends output: null; the Show output button is offered', () => {
  const session = fx.sessions()[0];
  const r = renderAll(mods, buildState(mods, fx.normal(), { replay: { session, data: { ...fx.replay(false), output: null } } }));
  assert.ok(allElements(r.audit).some((e) => e.localName === 'button' && e.textContent === 'Show output'));
  assert.doesNotMatch(r.audit.textContent, /no output/);
});

test('Routing tab: the Gate row follows gate_error_kind', () => {
  const s = fx.pcOff();
  let r = renderAll(mods, buildState(mods, s));
  assert.match(r.routing.textContent, /GateNot answering \(timed out after 4 s\)/);
  s.routing = { ...s.routing, gate_error: 'gate has no fresh sample (HTTP 503)', gate_error_kind: 'http', gate_http_status: 503 };
  r = renderAll(mods, buildState(mods, s));
  assert.match(r.routing.textContent, /GateReplied with an error: HTTP 503: no fresh sample/);
  assert.match(r.overview.textContent, /the GPU gate replied with an error \(HTTP 503: no fresh sample\)/);
});

test('the window shows from routing.window_s', () => {
  const s = fx.gamingCpu();
  s.routing.window_s = 3600;
  const r = renderAll(mods, buildState(mods, s));
  assert.match(r.overview.textContent, /averaged 62% over 1 h \(limit 50%\)/);
  assert.match(r.routing.textContent, /Average of game use \(1 h\)62%/);
  assert.match(r.routing.textContent, /over the last 1 h\./);
});

test('offline banner shows the age of the saved data', () => {
  const { reduce } = mods.state;
  let s = buildState(mods, fx.normal());
  s = { ...s, summaryAt: fx.NOW - 3 * 60_000 };
  s = reduce(s, { type: 'summary/failed', error: 'unreachable' });
  const r = renderAll(mods, s);
  const banner = allElements(r.shell).find((e) => /\bbanner\b/.test(e.getAttribute('class') || ''));
  assert.equal(banner.hidden, false);
  assert.match(banner.textContent, /Showing what it reported 3 min ago/);
  const online = renderAll(mods, buildState(mods, fx.normal()));
  assert.equal(allElements(online.shell).find((e) => /\bbanner\b/.test(e.getAttribute('class') || '')).hidden, true);
});

test('no summary yet: tabs show a loading line instead of failing', () => {
  const r = renderAll(mods, mods.state.initialState());
  for (const [name, root] of Object.entries(r)) assert.ok(root.textContent.length > 0, name);
});
