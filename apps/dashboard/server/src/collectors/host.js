// Host metrics: /proc/loadavg, /proc/meminfo, statfs /, /proc/uptime, /run/reboot-required(.pkgs).
import nodeFs from 'node:fs';
import path from 'node:path';
import { exists, readText } from '../util/fsx.js';

export function parseLoadavg(text) {
  const parts = String(text ?? '').trim().split(/\s+/).slice(0, 3).map(Number);
  return parts.length === 3 && parts.every(Number.isFinite) ? parts : null;
}

export function parseMeminfo(text) {
  const kb = {};
  for (const line of String(text ?? '').split('\n')) {
    const m = line.match(/^(\w+):\s+(\d+)\s*kB/);
    if (m) kb[m[1]] = Number(m[2]) * 1024;
  }
  if (!kb.MemTotal) return null;
  const available = kb.MemAvailable ?? (kb.MemFree ?? 0) + (kb.Buffers ?? 0) + (kb.Cached ?? 0);
  return { total: kb.MemTotal, used: Math.max(0, kb.MemTotal - available) };
}

export function statfsUsage(st) {
  const total = st.blocks * st.bsize;
  const free = st.bavail * st.bsize; // what unprivileged writers can still use
  const used = (st.blocks - st.bfree) * st.bsize;
  const usedPct = used + free > 0 ? Math.round((used / (used + free)) * 1000) / 10 : 0;
  return { total, used, free, usedPct };
}

// statfs with a timeout, so a dying drive can never hang a refresh. Single-flight per path: while a statfs on a
// path is still pending (a hung USB drive blocks it in a libuv pool thread), later calls wait on that same call
// instead of starting another one, so a hung drive can hold at most one pool thread.
const inflight = new WeakMap(); // statfs fn -> Map(path -> promise)

export function statfsTimed(p, { timeoutMs = 3000, statfs = nodeFs.promises.statfs } = {}) {
  if (!inflight.has(statfs)) inflight.set(statfs, new Map());
  const byPath = inflight.get(statfs);
  let pending = byPath.get(p);
  if (!pending) {
    pending = Promise.resolve()
      .then(() => statfs(p))
      .finally(() => byPath.delete(p));
    byPath.set(p, pending);
  }
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(Object.assign(new Error('statfs timed out'), { code: 'ETIMEDOUT' })), timeoutMs);
  });
  return Promise.race([pending, timeout]).finally(() => clearTimeout(timer));
}

export async function collectHost({ cfg, now = Date.now(), statfs }) {
  const proc = cfg.procDir;
  const load = parseLoadavg(readText(path.join(proc, 'loadavg')));
  const mem = parseMeminfo(readText(path.join(proc, 'meminfo')));
  const uptimeSec = Number.parseFloat(String(readText(path.join(proc, 'uptime')) ?? '').split(/\s+/)[0]);
  let root = null;
  try {
    root = statfsUsage(await statfsTimed(cfg.rootPath, { statfs }));
  } catch {
    root = null;
  }
  const rebootRequired = exists(cfg.rebootRequiredFile);
  const pkgs = rebootRequired ? readText(`${cfg.rebootRequiredFile}.pkgs`) : null;
  return {
    name: cfg.displayName,
    load: load ? load.map((v) => Math.round(v * 100) / 100) : null,
    memTotal: mem?.total ?? null,
    // rounded to 16 MiB so the summary doesn't churn on every page-cache wobble
    memUsed: mem ? Math.round(mem.used / 16_777_216) * 16_777_216 : null,
    rootTotal: root?.total ?? null,
    rootUsed: root?.used ?? null,
    rootUsedPct: root ? Math.round(root.usedPct) : null,
    bootedAt: Number.isFinite(uptimeSec) ? new Date(Math.round((now - uptimeSec * 1000) / 60_000) * 60_000).toISOString() : null,
    rebootRequired,
    rebootPackages: pkgs ? [...new Set(pkgs.split('\n').map((s) => s.trim()).filter(Boolean))].sort() : [],
  };
}
