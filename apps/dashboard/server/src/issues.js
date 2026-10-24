// The overview banner's issue list, decided server-side so every client agrees. Each issue: {severity:
// fail|warn, text, target: drive|apps|media|alerts|backups|settings, app?}. Fail issues come first.
//
// Not issues: an app stopped on purpose, a reboot-required flag (shown on the host tile), a clean start with no
// backup yet, Prowlarr indexers near their limit (shown in Media health).
import { DEFAULT_APPS } from './catalog.js';
import { shortTime } from './util/time.js';

const pretty = (name) => DEFAULT_APPS[name]?.label ?? name;

function ago(iso, now) {
  const h = Math.round((now - Date.parse(iso)) / 3_600_000);
  return h < 48 ? `${h} h ago` : `${Math.round(h / 24)} days ago`;
}

export function buildIssues(s, { now = Date.now(), timeZone } = {}) {
  const out = [];
  const add = (severity, text, target, extra = {}) => out.push({ severity, text, target, ...extra });
  const held = s.apps.filter((a) => a.status === 'held').length;
  const appsWord = (n) => (n === 1 ? '1 app' : `${n} apps`);

  switch (s.gate.status) {
    case 'failed':
      add('fail', `Media drive failed${s.gate.powercycled ? ' after a power-cycle' : ''}. ${appsWord(held)} held.`, 'drive');
      break;
    case 'media-lost':
      add('fail', `Media drive lost${s.gate.since ? ` at ${shortTime(s.gate.since, timeZone)}` : ''}. ${appsWord(held)} held.`, 'drive');
      break;
    case 'waiting':
      add('warn', 'Waiting for the media drive. The boot check is still running.', 'drive');
      break;
    case 'unknown':
      add('warn', 'Boot gate status is unknown.', 'drive');
      break;
    default:
      break;
  }

  if (s.helper.reachable === false) add('warn', "Can't reach the Docker helper. App status is unknown.", 'apps');
  else if (s.docker.state !== 'active' && !['failed', 'waiting'].includes(s.gate.status)) {
    // media-lost holds only the media stack: Docker being down then is its own problem and is reported
    // one issue for the whole of Docker, never one per app (with live-restore the apps may still be serving)
    add('fail', "Docker isn't running.", 'apps');
  }

  for (const a of s.apps) {
    if (a.status === 'down') add('fail', `${pretty(a.name)} is down.`, 'apps', { app: a.name });
    else if (a.status === 'unhealthy') add('warn', `${pretty(a.name)} is unhealthy.`, 'apps', { app: a.name });
  }

  if (s.drive.warn) add('warn', `Media drive is ${Math.round(s.drive.usedPct)}% full.`, 'drive');

  for (const [app, tile] of Object.entries(s.media)) {
    if (app === 'checks' || !tile) continue;
    if (tile.state === 'error') add('warn', `${pretty(app)}'s API isn't answering.`, 'media', { app });
  }
  for (const ix of s.media.prowlarr?.indexers ?? []) {
    if (ix.status === 'fail') add('warn', `${ix.name}: ${ix.detail.replace(/^Failing/, 'failing')}.`, 'media');
  }
  for (const c of s.media.checks ?? []) {
    if (c.key === 'usenet-login' && c.status === 'fail') add('warn', 'Usenet login is failing.', 'media');
    if (c.key === 'library-folders' && c.status !== 'ok' && c.status !== 'idle' && s.drive.mounted) add('warn', 'A library folder is missing.', 'media');
  }

  if (s.backups.status === 'failed') add('fail', 'The last nightly backup failed.', 'backups');
  else if (s.backups.status === 'stale' && s.backups.lastOk) add('warn', `Last good backup was ${ago(s.backups.lastOk, now)}.`, 'backups');

  if (s.push.storeError) add('fail', 'The push subscription list is unreadable. Alerts may not arrive.', 'alerts');

  for (const sub of s.subscriptions) {
    if (sub.daysLeft < 0) add('warn', `${sub.name} renewal date has passed.`, 'settings');
    else if (sub.daysLeft <= 7) add('warn', `${sub.name} renews in ${sub.daysLeft === 1 ? '1 day' : `${sub.daysLeft} days`}.`, 'settings');
  }

  return [...out.filter((i) => i.severity === 'fail'), ...out.filter((i) => i.severity !== 'fail')];
}
