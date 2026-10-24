// The host's IANA time zone, resolved once at start (summary.server.timeZone, and every server-side calendar
// day). Order: the configured `timezone` (role dashboard renders the inventory value), /etc/timezone,
// `timedatectl show -p Timezone --value`, the process zone (/etc/localtime), then UTC. Each candidate must be
// a zone Intl knows.
import { execFileSync } from 'node:child_process';
import nodeFs from 'node:fs';

export function isTimeZone(value) {
  if (typeof value !== 'string' || !/^[A-Za-z][A-Za-z0-9_+\-/]{0,63}$/.test(value)) return false;
  try {
    new Intl.DateTimeFormat('en-US', { timeZone: value });
    return true;
  } catch {
    return false;
  }
}

export function timedatectlZone() {
  try {
    return execFileSync('timedatectl', ['show', '-p', 'Timezone', '--value'], {
      timeout: 2000,
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'ignore'],
      env: { PATH: '/usr/bin:/bin', LC_ALL: 'C' },
    }).trim();
  } catch {
    return null;
  }
}

export function resolveTimeZone({ configured, file = '/etc/timezone', timedatectl = timedatectlZone, fs = nodeFs } = {}) {
  const fromFile = () => {
    try {
      return fs.readFileSync(file, 'utf8').trim();
    } catch {
      return null;
    }
  };
  const candidates = [
    ['config', () => configured],
    ['file', fromFile],
    ['timedatectl', timedatectl],
    ['process', () => Intl.DateTimeFormat().resolvedOptions().timeZone],
  ];
  for (const [source, get] of candidates) {
    const zone = get();
    if (isTimeZone(zone)) return { timeZone: zone, source };
  }
  return { timeZone: 'UTC', source: 'default' };
}
