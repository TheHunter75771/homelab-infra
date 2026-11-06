#!/usr/bin/env node
// Draws Mission Control's app icons from code: a radar mark (rings, a crosshair, a fading sweep, one blip) on
// the Nocturne ground. The same geometry is written as SVG and rasterised to PNG here, with node:zlib only (no
// image library, no dependencies), so the icons can be rebuilt anywhere and the tests can check them.
//
//   node scripts/make-icons.mjs           write public/icons/*
//   node scripts/make-icons.mjs --check   exit 1 if a PNG's pixels or an SVG differ from what this draws
//
// Files: icon.svg (any), icon-192.png, icon-512.png (any: rounded square), maskable-512.png (full bleed, the
// mark inside the 80% safe zone), apple-touch-icon.png (180, full bleed: iOS rounds it), badge-96.png (white
// on transparent: Android uses only the alpha for the status-bar badge).

import fs from 'node:fs';
import path from 'node:path';
import zlib from 'node:zlib';
import { fileURLToPath } from 'node:url';

export const ICON_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', 'public', 'icons');

const GROUND = [0x16, 0x18, 0x26];
const GROUND_HI = [0x22, 0x25, 0x3a];
const ACCENT = [0x91, 0x84, 0xd9];
const ACCENT_TEXT = [0xd2, 0xce, 0xfd];
const WHITE = [0xff, 0xff, 0xff];

const LEAD = (-40 * Math.PI) / 180; // the sweep's leading edge (y points down, so this is up and to the right)
const TRAIL = (70 * Math.PI) / 180; // how far the fading sweep reaches behind it
const STEPS = 7;

/**
 * The drawing as a list of layers in a 512×512 space. kind: 'any' | 'maskable' | 'badge'.
 * Layer: {shape, ..., color: [r,g,b], alpha}.
 */
export function layers(kind) {
  const C = 256;
  const R = kind === 'maskable' ? 138 : kind === 'badge' ? 200 : 158;
  const out = [];
  if (kind === 'any') {
    out.push({ shape: 'rrect', x: 16, y: 16, w: 480, h: 480, r: 108, color: GROUND, alpha: 1 });
    out.push({ shape: 'glow', cx: C, cy: C, r: 230, color: GROUND_HI, alpha: 1, clip: 'rrect' });
  } else if (kind === 'maskable') {
    out.push({ shape: 'rect', color: GROUND, alpha: 1 });
    out.push({ shape: 'glow', cx: C, cy: C, r: 260, color: GROUND_HI, alpha: 1 });
  }
  const tone = kind === 'badge' ? WHITE : ACCENT;
  const bright = kind === 'badge' ? WHITE : ACCENT_TEXT;
  if (kind !== 'badge') {
    for (let i = 0; i < STEPS; i++) {
      const a1 = LEAD - (TRAIL * i) / STEPS;
      out.push({ shape: 'sector', cx: C, cy: C, r: R, from: a1 - TRAIL / STEPS, to: a1, color: ACCENT, alpha: 0.42 * (1 - i / STEPS) ** 1.6 });
    }
    out.push({ shape: 'line', x1: C - R, y1: C, x2: C + R, y2: C, w: R * 0.026, color: tone, alpha: 0.32 });
    out.push({ shape: 'line', x1: C, y1: C - R, x2: C, y2: C + R, w: R * 0.026, color: tone, alpha: 0.32 });
    out.push({ shape: 'ring', cx: C, cy: C, r: R * 0.34, w: R * 0.04, color: tone, alpha: 0.5 });
    out.push({ shape: 'ring', cx: C, cy: C, r: R * 0.67, w: R * 0.04, color: tone, alpha: 0.6 });
  }
  out.push({ shape: 'ring', cx: C, cy: C, r: R, w: R * (kind === 'badge' ? 0.1 : 0.055), color: tone, alpha: 0.95 });
  out.push({ shape: 'line', x1: C, y1: C, x2: C + R * Math.cos(LEAD), y2: C + R * Math.sin(LEAD), w: R * (kind === 'badge' ? 0.1 : 0.055), color: bright, alpha: 1 });
  const ba = LEAD - 0.42;
  const bx = C + R * 0.56 * Math.cos(ba);
  const by = C + R * 0.56 * Math.sin(ba);
  if (kind !== 'badge') out.push({ shape: 'circle', cx: bx, cy: by, r: R * 0.15, color: bright, alpha: 0.22 });
  out.push({ shape: 'circle', cx: bx, cy: by, r: R * (kind === 'badge' ? 0.11 : 0.075), color: bright, alpha: 1 });
  out.push({ shape: 'circle', cx: C, cy: C, r: R * (kind === 'badge' ? 0.09 : 0.06), color: bright, alpha: 1 });
  return out;
}

// --- coverage tests (one sample point) -----------------------------------------------------------

function inRRect(x, y, L) {
  if (x < L.x || y < L.y || x > L.x + L.w || y > L.y + L.h) return false;
  const dx = Math.max(L.x + L.r - x, 0, x - (L.x + L.w - L.r));
  const dy = Math.max(L.y + L.r - y, 0, y - (L.y + L.h - L.r));
  return dx * dx + dy * dy <= L.r * L.r;
}

function angleIn(a, from, to) {
  const norm = (v) => ((v % (2 * Math.PI)) + 2 * Math.PI) % (2 * Math.PI);
  const span = norm(to - from);
  return norm(a - from) <= span;
}

function segDist(x, y, L) {
  const vx = L.x2 - L.x1;
  const vy = L.y2 - L.y1;
  const t = Math.max(0, Math.min(1, ((x - L.x1) * vx + (y - L.y1) * vy) / (vx * vx + vy * vy)));
  return Math.hypot(x - (L.x1 + t * vx), y - (L.y1 + t * vy));
}

/** The alpha a layer paints at one sample point (0 when it doesn't cover it). */
function cover(L, x, y, all) {
  switch (L.shape) {
    case 'rect':
      return L.alpha;
    case 'rrect':
      return inRRect(x, y, L) ? L.alpha : 0;
    case 'glow': {
      if (L.clip === 'rrect' && !inRRect(x, y, all[0])) return 0;
      const d = Math.hypot(x - L.cx, y - L.cy) / L.r;
      return d >= 1 ? 0 : L.alpha * (1 - d) ** 2;
    }
    case 'ring':
      return Math.abs(Math.hypot(x - L.cx, y - L.cy) - L.r) <= L.w / 2 ? L.alpha : 0;
    case 'circle':
      return Math.hypot(x - L.cx, y - L.cy) <= L.r ? L.alpha : 0;
    case 'line':
      return segDist(x, y, L) <= L.w / 2 ? L.alpha : 0;
    case 'sector':
      return Math.hypot(x - L.cx, y - L.cy) <= L.r && angleIn(Math.atan2(y - L.cy, x - L.cx), L.from, L.to) ? L.alpha : 0;
    default:
      throw new Error(`unknown shape ${L.shape}`);
  }
}

/** Rasterises a kind at `size` px: RGBA bytes, 4×4 supersampled, premultiplied while compositing. */
export function rasterize(kind, size) {
  const all = layers(kind);
  const out = Buffer.alloc(size * size * 4);
  const N = 4;
  const scale = 512 / size;
  for (let py = 0; py < size; py++) {
    for (let px = 0; px < size; px++) {
      let r = 0, g = 0, b = 0, a = 0;
      for (let sy = 0; sy < N; sy++) {
        for (let sx = 0; sx < N; sx++) {
          const x = (px + (sx + 0.5) / N) * scale;
          const y = (py + (sy + 0.5) / N) * scale;
          let cr = 0, cg = 0, cb = 0, ca = 0; // premultiplied
          for (const L of all) {
            const al = cover(L, x, y, all);
            if (!al) continue;
            cr = L.color[0] * al + cr * (1 - al);
            cg = L.color[1] * al + cg * (1 - al);
            cb = L.color[2] * al + cb * (1 - al);
            ca = al + ca * (1 - al);
          }
          r += cr; g += cg; b += cb; a += ca;
        }
      }
      const n = N * N;
      const i = (py * size + px) * 4;
      const A = a / n;
      out[i] = A ? Math.round(r / n / A) : 0;
      out[i + 1] = A ? Math.round(g / n / A) : 0;
      out[i + 2] = A ? Math.round(b / n / A) : 0;
      out[i + 3] = Math.round(A * 255);
    }
  }
  return out;
}

// --- PNG ---------------------------------------------------------------------------------------

const CRC_TABLE = Array.from({ length: 256 }, (_, n) => {
  let c = n;
  for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
  return c >>> 0;
});
export function crc32(buf) {
  let c = 0xffffffff;
  for (const byte of buf) c = CRC_TABLE[(c ^ byte) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

function chunk(type, data) {
  const len = Buffer.alloc(4);
  len.writeUInt32BE(data.length);
  const td = Buffer.concat([Buffer.from(type, 'ascii'), data]);
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(crc32(td));
  return Buffer.concat([len, td, crc]);
}

export const PNG_SIG = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);

export function encodePng(size, rgba) {
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(size, 0);
  ihdr.writeUInt32BE(size, 4);
  ihdr[8] = 8; // bit depth
  ihdr[9] = 6; // RGBA
  const rows = Buffer.alloc(size * (size * 4 + 1));
  for (let y = 0; y < size; y++) rgba.copy(rows, y * (size * 4 + 1) + 1, y * size * 4, (y + 1) * size * 4); // filter 0
  return Buffer.concat([PNG_SIG, chunk('IHDR', ihdr), chunk('IDAT', zlib.deflateSync(rows, { level: 9 })), chunk('IEND', Buffer.alloc(0))]);
}

/** Reads a PNG this script wrote: {width, height, colorType, rgba}, checking every chunk's CRC. */
export function decodePng(buf) {
  if (!buf.subarray(0, 8).equals(PNG_SIG)) throw new Error('not a PNG');
  let off = 8;
  let ihdr = null;
  const idat = [];
  let ended = false;
  while (off < buf.length) {
    const len = buf.readUInt32BE(off);
    const type = buf.toString('ascii', off + 4, off + 8);
    const data = buf.subarray(off + 8, off + 8 + len);
    if (crc32(buf.subarray(off + 4, off + 8 + len)) !== buf.readUInt32BE(off + 8 + len)) throw new Error(`bad CRC in ${type}`);
    if (type === 'IHDR') ihdr = { width: data.readUInt32BE(0), height: data.readUInt32BE(4), depth: data[8], colorType: data[9] };
    else if (type === 'IDAT') idat.push(data);
    else if (type === 'IEND') ended = true;
    off += 12 + len;
  }
  if (!ihdr || !ended) throw new Error('missing IHDR or IEND');
  const rows = zlib.inflateSync(Buffer.concat(idat));
  const stride = ihdr.width * 4 + 1;
  const rgba = Buffer.alloc(ihdr.width * ihdr.height * 4);
  for (let y = 0; y < ihdr.height; y++) {
    if (rows[y * stride] !== 0) throw new Error('only filter 0 is written here');
    rows.copy(rgba, y * ihdr.width * 4, y * stride + 1, (y + 1) * stride);
  }
  return { ...ihdr, rgba };
}

// --- SVG ---------------------------------------------------------------------------------------

const hex = (c) => `#${c.map((v) => v.toString(16).padStart(2, '0')).join('')}`;
const n = (v) => String(Math.round(v * 100) / 100);

function svgLayer(L) {
  const fill = `fill="${hex(L.color)}"`;
  const op = L.alpha < 1 ? ` opacity="${n(L.alpha)}"` : '';
  switch (L.shape) {
    case 'rect':
      return `<rect width="512" height="512" ${fill}/>`;
    case 'rrect':
      return `<rect x="${L.x}" y="${L.y}" width="${L.w}" height="${L.h}" rx="${L.r}" ${fill}/>`;
    case 'glow':
      return `<circle cx="${L.cx}" cy="${L.cy}" r="${L.r}" fill="url(#glow)"${L.clip ? ' clip-path="url(#tile)"' : ''}/>`;
    case 'ring':
      return `<circle cx="${n(L.cx)}" cy="${n(L.cy)}" r="${n(L.r)}" fill="none" stroke="${hex(L.color)}" stroke-width="${n(L.w)}"${op}/>`;
    case 'circle':
      return `<circle cx="${n(L.cx)}" cy="${n(L.cy)}" r="${n(L.r)}" ${fill}${op}/>`;
    case 'line':
      return `<line x1="${n(L.x1)}" y1="${n(L.y1)}" x2="${n(L.x2)}" y2="${n(L.y2)}" stroke="${hex(L.color)}" stroke-width="${n(L.w)}" stroke-linecap="round"${op}/>`;
    case 'sector': {
      const p = (a) => `${n(L.cx + L.r * Math.cos(a))} ${n(L.cy + L.r * Math.sin(a))}`;
      return `<path d="M${n(L.cx)} ${n(L.cy)}L${p(L.from)}A${n(L.r)} ${n(L.r)} 0 0 1 ${p(L.to)}Z" ${fill}${op}/>`;
    }
    default:
      throw new Error(`unknown shape ${L.shape}`);
  }
}

export function svg(kind) {
  const all = layers(kind);
  const defs = all.some((L) => L.shape === 'glow')
    ? `<defs><radialGradient id="glow"><stop offset="0" stop-color="${hex(GROUND_HI)}"/><stop offset="0.5" stop-color="${hex(GROUND_HI)}" stop-opacity="0.25"/><stop offset="1" stop-color="${hex(GROUND_HI)}" stop-opacity="0"/></radialGradient>` +
      (all.some((L) => L.clip) ? `<clipPath id="tile"><rect x="16" y="16" width="480" height="480" rx="108"/></clipPath>` : '') +
      '</defs>'
    : '';
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">${defs}${all.map(svgLayer).join('')}</svg>\n`;
}

export const FILES = [
  { file: 'icon.svg', kind: 'any', svg: true },
  { file: 'icon-192.png', kind: 'any', size: 192 },
  { file: 'icon-512.png', kind: 'any', size: 512 },
  { file: 'maskable-512.png', kind: 'maskable', size: 512 },
  { file: 'apple-touch-icon.png', kind: 'maskable', size: 180 },
  { file: 'badge-96.png', kind: 'badge', size: 96 },
];

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const check = process.argv.includes('--check');
  let stale = 0;
  fs.mkdirSync(ICON_DIR, { recursive: true });
  for (const f of FILES) {
    const p = path.join(ICON_DIR, f.file);
    if (f.svg) {
      const want = svg(f.kind);
      if (check) {
        if (!fs.existsSync(p) || fs.readFileSync(p, 'utf8') !== want) (stale++, console.error(`stale: ${f.file}`));
      } else fs.writeFileSync(p, want);
      continue;
    }
    const rgba = rasterize(f.kind, f.size);
    if (check) {
      const ok = fs.existsSync(p) && (() => {
        const d = decodePng(fs.readFileSync(p));
        return d.width === f.size && d.height === f.size && d.rgba.equals(rgba);
      })();
      if (!ok) (stale++, console.error(`stale: ${f.file}`));
    } else fs.writeFileSync(p, encodePng(f.size, rgba));
  }
  if (check && stale) process.exit(1);
  console.log(check ? 'icons are up to date' : `wrote ${FILES.length} icons to public/icons/`);
}
