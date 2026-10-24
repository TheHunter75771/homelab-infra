// Entry point for homelab-dashboard.service (CONTRACTS §11.1). Config: DASHBOARD_CONFIG (JSON, see README).
import { loadConfig } from './config.js';
import { createDashboard } from './dashboard.js';
import { createLogger } from './log.js';

let loaded;
try {
  loaded = loadConfig();
} catch (err) {
  createLogger({ write: (line) => process.stderr.write(`${line}\n`) }).error('config error', { error: err.message });
  process.exit(78); // EX_CONFIG: systemd keeps restarting it and the journal says why
}
const { config: cfg } = loaded;
const log = createLogger({ level: cfg.logLevel });

// Crash reports go through the same logger and redactor as every other line: redacted first, then URLs
// removed and clipped (so a cut can never leave part of a key).
const clean = (text, max) => {
  const t = log
    .redact(String(text ?? ''))
    .replace(/https?:\/\/\S+/gi, '<url>')
    .replace(/[\u0000-\u001f\u007f]+/g, ' ')
    .trim();
  return t.length > max ? `${t.slice(0, max)}…` : t;
};
const crashFields = (err) => ({ error: clean(err?.message ?? err, 500), stack: clean(err?.stack ?? '', 4000) });
// Contract §11.3 "Resource limits": every async path is caught; this is the last net. Log and keep serving.
process.on('unhandledRejection', (reason) => log.error('unhandled rejection', crashFields(reason)));
// A synchronous throw nobody caught leaves state unknown: log it (redacted, never Node's raw trace on stderr)
// and exit; systemd (Restart=always) starts a clean process.
process.on('uncaughtException', (err) => {
  log.error('uncaught exception', crashFields(err));
  process.exit(1);
});
if (loaded.missing) log.warn('config file missing; using built-in defaults', { file: loaded.file });
if (loaded.unknown.length) log.warn('unknown config keys ignored', { keys: loaded.unknown });
for (const warning of loaded.warnings ?? []) log.warn('config', { warning });

let dash;
try {
  dash = createDashboard(cfg, { log });
} catch (err) {
  log.error('startup failed', { error: err.message, code: err.code ?? null });
  process.exit(1); // systemd restarts it; the journal has the reason
}

dash.server.listen(cfg.listenPort, cfg.listenHost, () => {
  log.info('listening', { host: cfg.listenHost, port: dash.server.address().port, version: cfg.version, remoteEnabled: cfg.remoteEnabled });
  dash.start().catch((err) => log.error('startup failed', { error: err.message }));
});

let stopping = false;
function shutdown(signal) {
  if (stopping) return;
  stopping = true;
  log.info('shutting down', { signal });
  dash.stop();
  dash.server.close(() => process.exit(0));
  dash.server.closeIdleConnections();
  setTimeout(() => {
    dash.server.closeAllConnections();
    process.exit(0);
  }, 3000).unref();
}
process.on('SIGTERM', () => shutdown('SIGTERM'));
process.on('SIGINT', () => shutdown('SIGINT'));
