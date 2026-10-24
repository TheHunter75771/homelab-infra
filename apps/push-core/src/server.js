// Entry point. Env: PUSH_DIR (default /data), PORT (default 8085), HOST (default 0.0.0.0),
// LOG_LEVEL (debug|info|warn|error), PUSH_ENDPOINT_HOSTS (comma-separated push-service host suffixes, or "*").
import http from 'node:http';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import webpush from 'web-push';
import { createApp } from './app.js';
import { createLogger } from './log.js';
import { createStore, parseAllowedHosts, StoreCorruptError } from './store.js';
import { createVapidSource } from './vapid.js';

const here = path.dirname(fileURLToPath(import.meta.url));
const pushDir = process.env.PUSH_DIR || '/data';
const port = Number.parseInt(process.env.PORT ?? '8085', 10);
const host = process.env.HOST || '0.0.0.0';
const log = createLogger({ level: process.env.LOG_LEVEL || 'info' });

const store = createStore({ dir: pushDir, log, allowedHosts: parseAllowedHosts(process.env.PUSH_ENDPOINT_HOSTS) });
const vapid = createVapidSource({ file: path.join(pushDir, 'vapid.json') });
const handler = createApp({ store, vapid, webpush, log, publicDir: path.join(here, '..', 'public') });

const server = http.createServer({ requestTimeout: 15_000, headersTimeout: 10_000 }, handler);

server.listen(port, host, () => {
  log.info('listening', { host, port: server.address().port, pushDir });
  store.cleanupStaleTmp();
  try {
    vapid.get();
    log.info('vapid keys loaded', {});
  } catch (err) {
    log.error('vapid keys unavailable', { reason: err.reason ?? null, error: err.message });
  }
  try {
    log.info('store loaded', { subscriptions: store.list().length, writable: store.writable() });
  } catch (err) {
    const reason = err instanceof StoreCorruptError ? err.message : err.code;
    log.error('store unreadable; writes disabled until fixed', { reason });
  }
});

let stopping = false;
function shutdown(signal) {
  if (stopping) return;
  stopping = true;
  log.info('shutting down', { signal });
  server.close(() => process.exit(0));
  server.closeIdleConnections();
  setTimeout(() => {
    server.closeAllConnections();
    process.exit(0);
  }, 5000).unref();
}
process.on('SIGTERM', () => shutdown('SIGTERM'));
process.on('SIGINT', () => shutdown('SIGINT'));
