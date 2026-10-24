// Builds the whole service from a config. Every host interaction can be replaced through `deps` (tests).
import http from 'node:http';
import path from 'node:path';
import webpush from 'web-push';
import { createAccess } from './access.js';
import { createActions } from './actions.js';
import { createDriveState } from './collectors/drive.js';
import { createMedia } from './collectors/media.js';
import { createDocker, createHelperClient } from './helper/client.js';
import { createApp } from './http/app.js';
import { createStatic } from './http/static.js';
import { createLogger } from './log.js';
import { createPushService } from './push/service.js';
import { createStore, parseAllowedHosts, StoreCorruptError } from './push/store.js';
import { createVapidSource } from './push/vapid.js';
import { createSecrets } from './redact.js';
import { createReminders } from './reminders.js';
import { createSampler } from './sampler.js';
import { createSettings } from './settings.js';
import { createSummaryService } from './summary.js';
import { createUptimeStore } from './uptime.js';
import { ensureDir, exists } from './util/fsx.js';
import { createRunner } from './util/run.js';
import { resolveTimeZone } from './util/tz.js';

export function createDashboard(cfg, deps = {}) {
  const log = deps.log ?? createLogger({ level: cfg.logLevel });
  const clock = deps.clock ?? (() => Date.now());
  // Resolved once: summary.server.timeZone and every server-side calendar day use the same zone.
  const tz = deps.timeZone ? { timeZone: deps.timeZone, source: 'deps' } : resolveTimeZone({ configured: cfg.timezone, file: cfg.timezoneFile, timedatectl: deps.timedatectl });
  const timeZone = tz.timeZone;
  ensureDir(cfg.stateDir, 0o750);

  const secrets = createSecrets();
  log.setRedactor?.((line) => secrets.redact(line)); // every log line, including crash reports, knows every key
  const store = createStore({ dir: cfg.pushDir, log, allowedHosts: parseAllowedHosts(cfg.pushEndpointHosts) });
  const vapid = createVapidSource({ file: path.join(cfg.pushDir, 'vapid.json') });
  const settings = createSettings({ stateDir: cfg.stateDir, log });
  const registerKeys = (s) => {
    secrets.add(s.integrations.jellyfinApiKey);
    secrets.add(s.integrations.jellyseerrApiKey);
  };
  registerKeys(settings.get());
  settings.onChange(registerKeys);

  const helper = deps.helper ?? createHelperClient({ socketPath: cfg.helperSocket });
  const docker = createDocker({ helper });
  const run = deps.run === undefined ? createRunner() : deps.run;
  const wp = deps.webpush ?? webpush;
  const driveState = createDriveState({ stateDir: cfg.stateDir });
  const uptimeStore = createUptimeStore({ stateDir: cfg.stateDir, timeZone });
  const media = createMedia({ cfg, settings, secrets, timeZone, clock, fetchImpl: deps.fetch ?? globalThis.fetch });
  const pushService = createPushService({ store, vapid, webpush: wp, log });
  const reminders = createReminders({
    stateDir: cfg.stateDir,
    settings,
    push: pushService,
    isRehearsal: () => exists(cfg.rehearsalFlag),
    timeZone,
    clock,
    log,
  });

  let summary = null;
  const actions = createActions({
    cfg,
    docker,
    run,
    log,
    clock,
    summary: {
      refreshCore: () => summary.refreshCore(),
      refreshSoon: () => summary.refreshSoon(),
      touch: () => summary.touch(),
    },
    ...(deps.actionOptions ?? {}),
  });
  summary = createSummaryService({
    cfg,
    docker,
    store,
    settings,
    actions,
    driveState,
    uptimeStore,
    media,
    reminders,
    run,
    statfs: deps.statfs,
    log,
    clock,
    timeZone,
  });
  const sampler = createSampler({ summary, uptimeStore, driveState, cfg, clock, timeZone, log });
  const staticFiles = createStatic({ webDir: cfg.webDir, log });
  const handler = createApp({
    cfg,
    log,
    store,
    vapid,
    webpush: wp,
    summary,
    actions,
    docker,
    settings,
    reminders,
    access: deps.access ?? createAccess({ cfg, fetchImpl: deps.fetch ?? globalThis.fetch, clock }),
    staticFiles,
    secrets,
    refreshSecrets: () => media.registerKeys(),
    clock,
    ...(deps.appOptions ?? {}),
  });
  // requestTimeout bounds receiving a request only; SSE responses stay open as long as the client wants.
  // Every request promise is caught here too: a bug in a handler can cost that request, never the process.
  const server = http.createServer({ requestTimeout: 15_000, headersTimeout: 10_000 }, (req, res) => {
    handler(req, res).catch((err) => {
      log.error('request failed', { error: err?.message ?? String(err) });
      try {
        if (!res.headersSent) {
          res.writeHead(500, { 'Content-Type': 'application/json; charset=utf-8' });
          res.end('{"error":"internal error"}');
        } else res.destroy();
      } catch {
        // nothing left to do for this request
      }
    });
  });
  server.keepAliveTimeout = 5000;
  const intervals = [];

  return {
    cfg,
    log,
    timeZone,
    server,
    store,
    vapid,
    settings,
    summary,
    actions,
    sampler,
    reminders,
    secrets,
    staticFiles,
    docker,
    httpStats: () => handler.stats(),

    // Startup checks + collectors + timers. `background: false` (tests) skips the timers.
    async start({ background = true } = {}) {
      store.cleanupStaleTmp();
      try {
        secrets.add(vapid.get().privateKey);
        log.info('vapid keys loaded', {});
      } catch (err) {
        log.error('vapid keys unavailable', { reason: err.reason ?? null, error: err.message });
      }
      try {
        log.info('store loaded', { subscriptions: store.list().length, writable: store.writable() });
      } catch (err) {
        log.error('store unreadable; writes disabled until fixed', { reason: err instanceof StoreCorruptError ? err.message : err.code });
      }
      log.info('web build', { available: staticFiles.available(), files: staticFiles.fileCount() });
      log.info('time zone', { timeZone, source: tz.source });
      if (!background) {
        await summary.refreshBoots();
        await summary.refreshLastPull();
        await summary.refreshCore();
        await summary.refreshSplit();
        await summary.refreshMedia();
        return;
      }
      await summary.start();
      sampler.start();
      const remind = () => reminders.check().catch(() => {});
      intervals.push(setTimeout(remind, 30_000), setInterval(remind, cfg.intervals.reminders * 1000));
      for (const t of intervals) t.unref();
      log.info('collectors running', {});
    },

    stop() {
      summary.stop();
      sampler.stop();
      for (const t of intervals) clearTimeout(t);
    },
  };
}
