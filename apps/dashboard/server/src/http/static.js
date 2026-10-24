// Serves module G's web build from one directory (config `web_dir`), loaded into memory at start (a deploy
// restarts the service). Client routes without a file extension fall back to index.html.
import nodeFs from 'node:fs';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { SECURITY_HEADERS, send } from './common.js';

const TYPES = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.webmanifest': 'application/manifest+json; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.map': 'application/json; charset=utf-8',
  '.png': 'image/png',
  '.svg': 'image/svg+xml',
  '.ico': 'image/x-icon',
  '.woff2': 'font/woff2',
  '.txt': 'text/plain; charset=utf-8',
};

const MISSING_PAGE =
  '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">' +
  '<title>Homelab dashboard</title></head><body><h1>Dashboard web build not installed</h1>' +
  '<p>The API is running. Install the web build into the configured web directory and restart the service.</p></body></html>';

export function createStatic({ webDir, log, maxFiles = 2000 }) {
  const files = new Map();
  let available = false;
  try {
    // Only regular files whose real path is inside web_dir: a symlinked file or directory pointing elsewhere
    // (readdir's recursion follows directory symlinks) is skipped.
    const root = nodeFs.realpathSync(webDir);
    const entries = nodeFs.readdirSync(webDir, { recursive: true });
    for (const rel of entries.slice(0, maxFiles)) {
      const relPath = String(rel);
      if (relPath.split(path.sep).some((part) => part.startsWith('.'))) continue;
      const abs = path.join(webDir, relPath);
      const st = nodeFs.lstatSync(abs);
      if (!st.isFile()) continue; // no symlinks, no dirs
      let real;
      try {
        real = nodeFs.realpathSync(abs);
      } catch {
        continue;
      }
      if (real !== path.join(root, relPath) || !real.startsWith(root + path.sep)) continue;
      const body = nodeFs.readFileSync(abs);
      files.set(`/${relPath.split(path.sep).join('/')}`, {
        body,
        type: TYPES[path.extname(relPath).toLowerCase()] ?? 'application/octet-stream',
        etag: `"${createHash('sha256').update(body).digest('hex').slice(0, 20)}"`,
      });
    }
    available = files.has('/index.html');
    if (!available) log?.error('web build has no index.html', { webDir });
  } catch (err) {
    log?.error('web build not found', { webDir, code: err.code ?? null });
  }

  function serveFile(req, res, file) {
    const headers = { 'Content-Type': file.type, 'Cache-Control': 'no-cache', ETag: file.etag };
    if (req.headers['if-none-match'] === file.etag) {
      res.writeHead(304, { ...SECURITY_HEADERS, ...headers });
      return res.end();
    }
    res.writeHead(200, { ...SECURITY_HEADERS, ...headers, 'Content-Length': file.body.length });
    res.end(file.body);
  }

  return {
    available: () => available,
    fileCount: () => files.size,
    // true when handled
    handle(req, res, pathname) {
      if (pathname.startsWith('/api/')) return false;
      const direct = files.get(pathname === '/' ? '/index.html' : pathname);
      if (direct) {
        serveFile(req, res, direct);
        return true;
      }
      const last = pathname.split('/').pop();
      if (last.includes('.')) return false; // a missing asset is a 404, not the app shell
      if (!available) {
        send(res, 503, MISSING_PAGE, { 'Content-Type': TYPES['.html'], 'Cache-Control': 'no-store' });
        return true;
      }
      serveFile(req, res, files.get('/index.html'));
      return true;
    },
  };
}
