// The URLs W asks for: fixed paths, only the documented params, ids in a query (never a path).
import test from 'node:test';
import assert from 'node:assert/strict';
import { paths } from '../public/js/api.js';

test('replay is /api/audit/replay with user, id and output as query params', () => {
  assert.equal(paths.replay('00000A', 'claude', false), '/api/audit/replay?user=claude&id=00000A&output=0');
  assert.equal(paths.replay('claude/00/00/0A', 'localai', true), '/api/audit/replay?user=localai&id=claude%2F00%2F00%2F0A&output=1');
  assert.throws(() => paths.replay('../x', 'claude', false), /unexpected session id/);
  assert.throws(() => paths.replay('<img src=x>', 'claude', true), /unexpected session id/);
});

test('every other path, with only its documented params', () => {
  assert.equal(paths.summary(), '/api/summary');
  assert.equal(paths.logSources(), '/api/logs/sources');
  assert.equal(paths.history(30), '/api/gate/history?minutes=30');
  assert.equal(paths.requests(50), '/api/requests?limit=50');
  assert.equal(paths.logs('pc:ollama', 200), '/api/logs?source=pc%3Aollama&lines=200');
  assert.equal(paths.sudo('pc', 'claude', 1793000000), '/api/audit/sudo?host=pc&user=claude&since=1793000000&limit=200');
  assert.equal(paths.sessions('claude'), '/api/audit/sessions?user=claude');
  assert.equal(paths.localai(1793000000), '/api/audit/localai?since=1793000000&limit=200');
  assert.equal(paths.events(), '/api/events');
});
