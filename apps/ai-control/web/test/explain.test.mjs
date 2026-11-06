import test from 'node:test';
import assert from 'node:assert/strict';
import { explainRouting, ruleSentences, uidMatchNote, serviceLabel, statusKey, statusWord, backendLabel, gateProblem } from '../public/js/explain.js';

const base = { avg30: 12, game: null, gpu_busy: 40, ollama_share: 38, uid_match: true, threshold: 50, window_s: 1800, gate_error: null, gate_error_kind: null, gate_http_status: null };

test('no game → GPU', () => {
  const r = explainRouting({ ...base, to: 'gpu', reason: 'no_game' });
  assert.equal(r.target, 'gpu');
  assert.equal(r.status, 'ok');
  assert.equal(r.sentence, "Answers are going to the gaming PC's GPU because no game is running.");
});

test('game running but light → GPU, with the numbers', () => {
  const r = explainRouting({ ...base, to: 'gpu', reason: 'below_threshold', game: 'Stardew Valley.exe', avg30: 21.4 });
  assert.equal(r.sentence, "Answers are going to the gaming PC's GPU: a game is running (Stardew Valley.exe), but the GPU has averaged 21% over 30 min (limit 50%).");
});

test('game busy → CPU, with the average and the limit', () => {
  const r = explainRouting({ ...base, to: 'cpu', reason: 'game_busy', game: 'Cyberpunk2077.exe', avg30: 62 });
  assert.equal(r.target, 'cpu');
  assert.equal(r.status, 'warn');
  assert.equal(r.sentence, "Answers are going to ai-box's CPU because a game is running (Cyberpunk2077.exe) and the GPU has averaged 62% over 30 min (limit 50%).");
});

test('game busy without a name still reads well', () => {
  const r = explainRouting({ ...base, to: 'cpu', reason: 'game_busy', avg30: 62 });
  assert.equal(r.sentence, "Answers are going to ai-box's CPU because a game is running and the GPU has averaged 62% over 30 min (limit 50%).");
});

test('a custom threshold is used', () => {
  const r = explainRouting({ ...base, to: 'cpu', reason: 'game_busy', avg30: 45, threshold: 40 });
  assert.match(r.sentence, /\(limit 40%\)/);
});

test('the window comes from routing.window_s', () => {
  const hour = explainRouting({ ...base, to: 'cpu', reason: 'game_busy', avg30: 62, window_s: 3600 });
  assert.match(hour.sentence, /averaged 62% over 1 h \(limit 50%\)/);
  const ninety = explainRouting({ ...base, to: 'gpu', reason: 'below_threshold', avg30: 20, window_s: 5400 });
  assert.match(ninety.sentence, /averaged 20% over 90 min \(limit 50%\)/);
  const none = explainRouting({ ...base, to: 'cpu', reason: 'game_busy', avg30: 62, window_s: undefined });
  assert.match(none.sentence, /averaged 62% over the gate's averaging window \(limit 50%\)/);
  assert.doesNotMatch(none.sentence, /30 min/);
  assert.match(ruleSentences({ threshold: 50, window_s: 7200 })[1], /over the last 2 h\./);
});

test('game, no samples → CPU', () => {
  const r = explainRouting({ ...base, to: 'cpu', reason: 'game_no_samples', game: 'gamescope', avg30: null });
  assert.match(r.sentence, /^Answers are going to ai-box's CPU because a game is running \(gamescope\) and the gate has no GPU samples yet/);
});

test('gate unreachable, broker on CPU → says why', () => {
  const r = explainRouting({ ...base, to: 'cpu', reason: null, gate_error: 'timed out after 4 s', gate_error_kind: 'network' });
  assert.equal(r.target, 'cpu');
  assert.equal(r.sentence, "Answers are going to ai-box's CPU because the GPU gate isn't answering (timed out after 4 s). The broker treats a gate it can't use as \"GPU not available\".");
});

test('gate failures are classified from gate_error_kind / gate_http_status, never from the text', () => {
  const g = (kind, status, err) => ({ ...base, gate_error_kind: kind, gate_http_status: status, gate_error: err });
  const http = explainRouting({ ...g('http', 503, 'gate has no fresh sample (HTTP 503)'), to: 'cpu', reason: null });
  assert.equal(http.sentence, "Answers are going to ai-box's CPU because the GPU gate replied with an error (HTTP 503: no fresh sample). The broker treats a gate it can't use as \"GPU not available\".");
  assert.equal(gateProblem(g('http', 500, 'x')).phrase, 'the GPU gate replied with an error (HTTP 500)');
  assert.equal(gateProblem(g('http', null, 'odd')).phrase, 'the GPU gate replied with an error (odd)');
  assert.equal(gateProblem(g('http', 99999, 'odd')).phrase, 'the GPU gate replied with an error (odd)', 'an impossible status is ignored');
  assert.equal(gateProblem(g('network', null, 'connection refused or unreachable')).phrase, "the GPU gate isn't answering (connection refused or unreachable)");
  assert.equal(gateProblem(g('stale', null, 'gate sample is stale')).phrase, 'AI Control has no fresh reading from the GPU gate (gate sample is stale)');
  assert.equal(gateProblem(g('shape', 200, "gate answer has no boolean 'available'")).phrase, "the GPU gate replied with something AI Control can't use (gate answer has no boolean 'available')");
  // The text is not parsed: "HTTP 503" in a network error stays a network error.
  assert.equal(gateProblem(g('network', null, 'HTTP 503 timed out')).kind, 'network');
  // Missing or unknown kind → the generic message, even if the text looks like an HTTP error.
  assert.equal(gateProblem({ ...base, gate_error: 'gate has no fresh sample (HTTP 503)', gate_error_kind: undefined }).phrase, "the GPU gate can't be read (gate has no fresh sample (HTTP 503))");
  assert.equal(gateProblem(g('teapot', 418, 'x')).phrase, "the GPU gate can't be read (x)");
  assert.equal(gateProblem(g(null, null, null)), null);
  assert.equal(gateProblem(g('network', null, null)).phrase, "the GPU gate isn't answering");
  const gpu = explainRouting({ ...g('http', 503, 'gate has no fresh sample (HTTP 503)'), to: 'gpu', reason: 'no_game' });
  assert.match(gpu.sentence, /replied with an error \(HTTP 503: no fresh sample\), so this may be out of date\.$/);
});

test('broker down → no answers at all', () => {
  const r = explainRouting({ ...base, to: null, reason: 'broker_down', gate_error: 'connection refused' });
  assert.equal(r.target, null);
  assert.equal(r.status, 'down');
  assert.equal(r.headline, 'No answers');
  assert.equal(r.sentence, "The router (broker) is down. AI answers aren't available.");
});

test('no destination and no known reason → unknown, with the gate error', () => {
  const r = explainRouting({ ...base, to: null, reason: null, gate_error: 'gate has no fresh sample (HTTP 503)', gate_error_kind: 'http', gate_http_status: 503 });
  assert.equal(r.status, 'unknown');
  assert.equal(r.sentence, "AI Control can't tell where answers are going right now: the GPU gate replied with an error (HTTP 503: no fresh sample).");
});

test('a destination with a gate error says the data may be stale', () => {
  const r = explainRouting({ ...base, to: 'gpu', reason: 'no_game', gate_error: 'gate has no fresh sample (HTTP 503)', gate_error_kind: 'http', gate_http_status: 503 });
  assert.match(r.sentence, /may be out of date/);
});

test('a reason that contradicts the destination is flagged, an unknown reason is quoted', () => {
  const r = explainRouting({ ...base, to: 'cpu', reason: 'no_game' });
  assert.equal(r.consistent, false);
  assert.match(r.sentence, /points the other way/);
  const u = explainRouting({ ...base, to: 'gpu', reason: 'manual_override' });
  assert.equal(u.consistent, true);
  assert.match(u.sentence, /"manual_override"/);
});

test('missing or failed routing section', () => {
  assert.equal(explainRouting(undefined).target, null);
  assert.match(explainRouting({ error: 'gate section failed' }).sentence, /gate section failed/);
});

test('rule sentences, uid note, labels', () => {
  assert.equal(ruleSentences({ threshold: 40 }).length, 4);
  assert.match(ruleSentences({ threshold: 40 })[1], /under 40%/);
  assert.equal(uidMatchNote({ uid_match: true }), null);
  assert.match(uidMatchNote({ uid_match: false }), /process name only/);
  assert.equal(serviceLabel('pc-ollama'), 'Gaming PC Ollama (GPU)');
  assert.equal(serviceLabel('toString'), 'toString', 'prototype keys are not labels');
  assert.equal(statusKey('ok'), 'ok');
  assert.equal(statusKey('x onerror=1'), 'unknown');
  assert.equal(statusWord('down'), 'Down');
  assert.equal(statusWord('constructor'), 'Unknown');
  assert.equal(backendLabel('gpu'), 'GPU');
});
