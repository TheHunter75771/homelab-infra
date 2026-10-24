// Once a minute: record every app's status into the uptime rollups, and once per local day append a drive
// sample (only while the drive is really mounted). Uses the latest core refresh when it is fresh, otherwise
// refreshes first, so a sample never describes stale state.
import { localDate } from './util/time.js';

export function createSampler({ summary, uptimeStore, driveState, cfg, clock = () => Date.now(), timeZone = cfg.timezone, log }) {
  let timer = null;

  async function tick() {
    const now = clock();
    let core = summary.core();
    if (!core || now - core.at > 2 * cfg.intervals.core * 1000) {
      await summary.refreshCore();
      core = summary.core();
    }
    if (!core) return { recorded: false, sampled: false };
    const statuses = Object.fromEntries(core.apps.map((a) => [a.name, a.status]));
    let recorded = false;
    try {
      recorded = uptimeStore.record(now, statuses);
    } catch (err) {
      log?.error('uptime rollup write failed', { code: err.code ?? null });
    }
    let sampled = false;
    const usage = core.drive._usage;
    if (usage && core.gate.status === 'ok' && core.drive.mounted) {
      try {
        sampled = driveState.addSample({ date: localDate(now, timeZone), used: usage.used, total: usage.total, now });
      } catch (err) {
        log?.error('drive sample write failed', { code: err.code ?? null });
      }
    }
    return { recorded, sampled };
  }

  return {
    tick,
    start() {
      timer = setInterval(() => {
        tick().catch(() => {});
      }, cfg.intervals.sampler * 1000);
      timer.unref();
    },
    stop() {
      clearInterval(timer);
    },
  };
}
