// Small filesystem helpers shared by the collectors and the dashboard's own state files.
import nodeFs from 'node:fs';
import path from 'node:path';
import { randomBytes } from 'node:crypto';

// File text, or null when it does not exist. Other errors (EACCES, EIO) propagate.
export function readText(file, fs = nodeFs) {
  try {
    return fs.readFileSync(file, 'utf8');
  } catch (err) {
    if (err.code === 'ENOENT' || err.code === 'ENOTDIR') return null;
    throw err;
  }
}

export function exists(file, fs = nodeFs) {
  try {
    fs.statSync(file);
    return true;
  } catch {
    return false;
  }
}

// {value} for valid JSON, {value: null} when missing, {error} when unreadable or invalid.
export function readJson(file, fs = nodeFs) {
  let text;
  try {
    text = readText(file, fs);
  } catch (err) {
    return { value: null, error: err.code || err.message };
  }
  if (text === null) return { value: null, missing: true };
  try {
    return { value: JSON.parse(text) };
  } catch {
    return { value: null, error: 'invalid-json' };
  }
}

// The last `maxLines` JSON objects of a JSON-lines file, oldest first. Bad lines are skipped. Reads at most
// `maxBytes` from the end, so a big history file never costs more than that.
export function readJsonlTail(file, { maxLines = 200, maxBytes = 512 * 1024, fs = nodeFs } = {}) {
  let fd;
  try {
    fd = fs.openSync(file, 'r');
  } catch (err) {
    if (err.code === 'ENOENT') return [];
    throw err;
  }
  try {
    const size = fs.fstatSync(fd).size;
    const start = Math.max(0, size - maxBytes);
    const buf = Buffer.alloc(size - start);
    let off = 0;
    while (off < buf.length) {
      const n = fs.readSync(fd, buf, off, buf.length - off, start + off);
      if (n === 0) break;
      off += n;
    }
    let lines = buf.subarray(0, off).toString('utf8').split('\n');
    if (start > 0) lines = lines.slice(1); // first line is partial
    const out = [];
    for (const line of lines) {
      if (!line.trim()) continue;
      try {
        const v = JSON.parse(line);
        if (v && typeof v === 'object' && !Array.isArray(v)) out.push(v);
      } catch {
        // skip a torn or foreign line
      }
    }
    return out.slice(-maxLines);
  } finally {
    fs.closeSync(fd);
  }
}

// tmp file in the same directory + fsync + rename + directory fsync.
export function writeFileAtomic(file, data, { mode = 0o640, fs = nodeFs } = {}) {
  const dir = path.dirname(file);
  const tmp = path.join(dir, `.${path.basename(file)}.${process.pid}.${randomBytes(6).toString('hex')}.tmp`);
  let fd;
  try {
    fd = fs.openSync(tmp, 'wx', mode);
    fs.fchmodSync(fd, mode);
    fs.writeFileSync(fd, data);
    fs.fsyncSync(fd);
    fs.closeSync(fd);
    fd = undefined;
    fs.renameSync(tmp, file);
  } catch (err) {
    if (fd !== undefined) {
      try {
        fs.closeSync(fd);
      } catch {
        // already failing
      }
    }
    try {
      fs.unlinkSync(tmp);
    } catch {
      // may not exist
    }
    throw err;
  }
  let dfd;
  try {
    dfd = fs.openSync(dir, 'r');
    fs.fsyncSync(dfd);
  } catch {
    // some filesystems refuse directory fsync
  } finally {
    if (dfd !== undefined) fs.closeSync(dfd);
  }
}

export function writeJsonAtomic(file, value, opts) {
  writeFileAtomic(file, `${JSON.stringify(value, null, 2)}\n`, opts);
}

// Append one line with O_APPEND (atomic for lines well under PIPE_BUF-sized writes on local filesystems).
export function appendLine(file, line, { mode = 0o640, fs = nodeFs } = {}) {
  const fd = fs.openSync(file, 'a', mode);
  try {
    fs.writeSync(fd, line.endsWith('\n') ? line : `${line}\n`);
    fs.fsyncSync(fd);
  } finally {
    fs.closeSync(fd);
  }
}

export function ensureDir(dir, mode = 0o750, fs = nodeFs) {
  fs.mkdirSync(dir, { recursive: true, mode });
}
