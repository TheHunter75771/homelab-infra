// A fake homelab-dockerctl on a real Unix socket, speaking the wire protocol of module H's helper
// (roles/dashboard/files/homelab-dockerctl/homelab_dockerctl.py):
//   unary  {"ok": true, "op": OP, "data": {...}} | {"ok": false, "error": {"code", "message"}}
//   stream {"ok": true, "op": OP, "stream": true, ...}, then {"type": "line"|"step"|"output", ...},
//          always ending with {"type": "end", "ok": BOOL, "reason": TEXT}
// It records every request, so tests can prove that no Docker operation is sent while Docker is inactive.
import fs from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';

export const DOCKER_OPS = new Set(['ps', 'start', 'stop', 'restart', 'logs', 'update']);

export function container(name, overrides = {}) {
  const ref = name === 'wol-relay' ? 'homelab/wol-relay:2026.07' : `example/${name}:1.0.0@sha256:${'a'.repeat(64)}`;
  return {
    name,
    state: 'running',
    health: name === 'wol-relay' ? null : 'healthy',
    started_at: '2026-10-21T10:02:00Z',
    restart_count: 0,
    image: ref,
    pinned: ref,
    stack: name === 'cloudflared' ? 'edge' : name === 'wol-relay' ? 'utils' : 'media',
    ...overrides,
  };
}

export const ALL_APPS = ['cloudflared', 'jellyfin', 'jellyseerr', 'prowlarr', 'radarr', 'sabnzbd', 'sonarr', 'wol-relay'];

// What the deployed compose files declare (the helper's `declared`).
export function declaredFor(containers) {
  return containers.map((c) => ({ name: c.name, stack: c.stack, service: c.name, pinned: c.pinned, local_build: c.name === 'wol-relay' }));
}

export function defaultHelperState(overrides = {}) {
  return {
    dockerState: 'active',
    containers: ALL_APPS.map((n) => container(n)),
    declared: undefined, // defaults to declaredFor(containers)
    lastPull: '2026-10-23T21:40:00Z',
    split: { sizes: { tv: 812e9, movies: 486e9, downloads: 62e9 }, computed_at: '2026-10-24T03:00:00Z', cached: true, stale: false },
    boots: [],
    logs: {}, // name -> [{ts, text}]
    logsFollow: false, // keep the stream open after the tail (until the client closes)
    updateSteps: ['pulling', 'recreating', 'waiting-health', 'done'],
    updateStepMs: 5,
    fail: {}, // op -> {code, message}
    delay: {}, // op -> ms
    hang: new Set(), // ops that never answer
    // called after start/stop/restart: mutate containers like Docker would
    onControl(op, name, state) {
      const c = state.containers.find((x) => x.name === name);
      if (!c) return;
      if (op === 'stop') {
        c.state = 'exited';
        c.health = null;
      } else {
        c.state = 'running';
        c.health = 'healthy';
        c.started_at = new Date().toISOString();
      }
    },
    ...overrides,
  };
}

export async function startFakeHelper(t, overrides = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dash-helper-'));
  const socketPath = path.join(dir, 'h.sock');
  const state = defaultHelperState(overrides);
  const calls = [];
  const openStreams = new Set();
  const server = net.createServer((sock) => {
    let buf = '';
    sock.setEncoding('utf8');
    sock.on('error', () => {});
    sock.on('data', async (chunk) => {
      buf += chunk;
      const i = buf.indexOf('\n');
      if (i === -1) return;
      const line = buf.slice(0, i);
      buf = '';
      let req;
      try {
        req = JSON.parse(line);
      } catch {
        sock.end(`${JSON.stringify({ ok: false, error: { code: 'bad-request', message: 'invalid JSON' } })}\n`);
        return;
      }
      const askedAt = state.dockerState; // an answer reflects the moment it was asked
      calls.push({ ...req, dockerStateAtCall: askedAt });
      if (state.hang.has(req.op)) return;
      if (state.delay[req.op]) await new Promise((r) => setTimeout(r, state.delay[req.op]));
      const send = (obj) => !sock.destroyed && sock.write(`${JSON.stringify(obj)}\n`);
      const reply = (data) => !sock.destroyed && sock.end(`${JSON.stringify({ ok: true, op: req.op, data })}\n`);
      const refuse = (code, message) => !sock.destroyed && sock.end(`${JSON.stringify({ ok: false, error: { code, message } })}\n`);
      if (state.fail[req.op]) return refuse(state.fail[req.op].code, state.fail[req.op].message);
      // The real helper refuses Docker ops while docker.service is down; so does the fake.
      if (DOCKER_OPS.has(req.op) && req.op !== 'ps' && state.dockerState !== 'active') {
        return refuse('docker-inactive', 'docker.service is not active');
      }
      const known = (name) => state.containers.some((c) => c.name === name);
      switch (req.op) {
        case 'docker-state':
          return reply({ state: askedAt, unit_state: askedAt });
        case 'ps':
          return reply({
            docker: askedAt,
            containers: askedAt === 'active' ? structuredClone(state.containers) : [],
            declared: state.declared ?? declaredFor(state.containers),
          });
        case 'last-pull':
          return reply({ last_pull: state.lastPull, started_at: null, duration_seconds: null, last_attempt: state.lastPull });
        case 'disk-split':
          return reply(state.split);
        case 'boots':
          return reply({ boots: state.boots });
        case 'start':
        case 'stop':
        case 'restart': {
          if (!known(req.name)) return refuse('not-allowed', `${req.name} is not a watched container`);
          state.onControl(req.op, req.name, state);
          return reply({ name: req.name, state: state.containers.find((c) => c.name === req.name).state });
        }
        case 'logs': {
          if (!known(req.name)) return refuse('not-allowed', `${req.name} is not a watched container`);
          send({ ok: true, op: 'logs', stream: true, name: req.name, follow: req.follow });
          for (const l of (state.logs[req.name] ?? []).slice(-(req.tail ?? 200))) send({ type: 'line', ts: l.ts ?? null, text: l.text });
          if (req.follow && state.logsFollow) {
            openStreams.add(sock);
            sock.on('close', () => openStreams.delete(sock));
            return; // stays open; tests push more lines via pushLog()
          }
          return sock.end(`${JSON.stringify({ type: 'end', ok: true, reason: 'eof' })}\n`);
        }
        case 'update': {
          if (!known(req.name)) return refuse('not-allowed', `${req.name} is not a watched container`);
          send({ ok: true, op: 'update', stream: true, name: req.name, pinned: state.containers.find((c) => c.name === req.name).pinned });
          let ok = true;
          let reason = 'done';
          for (const step of state.updateSteps) {
            if (step === 'failed') {
              const failedStep = state.updateSteps[state.updateSteps.indexOf('failed') - 1] ?? 'update';
              send({ type: 'step', step: 'failed', at: new Date().toISOString(), failed_step: failedStep, message: 'manifest unknown' });
              ok = false;
              reason = `${failedStep}: manifest unknown`;
              break;
            }
            send({ type: 'step', step, at: new Date().toISOString() });
            send({ type: 'output', text: `${step}...` });
            await new Promise((r) => setTimeout(r, state.updateStepMs));
          }
          return sock.end(`${JSON.stringify({ type: 'end', ok, reason })}\n`);
        }
        default:
          return refuse('unknown-op', `unknown op ${req.op}`);
      }
    });
  });
  await new Promise((resolve) => server.listen(socketPath, resolve));
  t.after(async () => {
    for (const s of openStreams) s.destroy();
    await new Promise((resolve) => server.close(resolve));
    fs.rmSync(dir, { recursive: true, force: true });
  });
  return {
    socketPath,
    state,
    calls,
    dockerCalls: () => calls.filter((c) => DOCKER_OPS.has(c.op)),
    openStreams: () => openStreams.size,
    // bytes the helper is holding for the dashboard (grows when the dashboard stops reading)
    streamBacklog: () => Math.max(0, ...[...openStreams].map((s) => s.writableLength)),
    pushLog(entry) {
      for (const s of openStreams) s.write(`${JSON.stringify({ type: 'line', ts: entry.ts ?? null, text: entry.text })}\n`);
    },
  };
}
