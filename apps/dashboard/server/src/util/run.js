// Runs a read-only host command (only `systemctl show` today) with a timeout. Never throws for a non-zero exit.
import { execFile } from 'node:child_process';

export function createRunner({ timeoutMs = 5000 } = {}) {
  return function run(cmd, args) {
    return new Promise((resolve) => {
      execFile(
        cmd,
        args,
        {
          timeout: timeoutMs,
          maxBuffer: 256 * 1024,
          env: { PATH: '/usr/sbin:/usr/bin:/sbin:/bin', LC_ALL: 'C', SYSTEMD_COLORS: '0', SYSTEMD_PAGER: '' },
        },
        (err, stdout, stderr) => {
          resolve({
            code: err ? (typeof err.code === 'number' ? err.code : 1) : 0,
            stdout: String(stdout ?? ''),
            stderr: String(stderr ?? ''),
            error: err && typeof err.code !== 'number' ? String(err.code || err.message) : null,
          });
        },
      );
    });
  };
}

// "Key=Value" lines (systemctl show) -> object
export function parseKeyValues(text) {
  const out = {};
  for (const line of String(text).split('\n')) {
    const i = line.indexOf('=');
    if (i > 0) out[line.slice(0, i)] = line.slice(i + 1).trim();
  }
  return out;
}
