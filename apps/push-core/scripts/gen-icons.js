// Generates the PWA icons from one geometry: a bell with two "ringing" arcs on a dark full-bleed square
// (safe for maskable and iOS masks). No dependencies: shapes are rasterised with 4x4 supersampling and
// written as PNG with node:zlib. Usage: node scripts/gen-icons.js <out-dir>
import fs from 'node:fs';
import path from 'node:path';
import zlib from 'node:zlib';

const BG_TOP = [0x18, 0x22, 0x3f];
const BG_BOTTOM = [0x0b, 0x10, 0x20];
const ACCENT = [0xf5, 0x9e, 0x0b];
const ARC_ALPHA = 0.7;

// Geometry in a 512x512 design space. Everything sits inside the maskable safe zone (r = 205 around centre).
const BELL = {
  circles: [
    [256, 118, 16], // knob
    [256, 214, 92], // dome
    [256, 388, 26], // clapper
  ],
  polygon: [
    [164, 214],
    [348, 214],
    [372, 338],
    [140, 338],
  ],
  rim: { x: 116, y: 330, w: 280, h: 32, r: 16 },
};
const ARCS = { cx: 256, cy: 236, r: 177, width: 18, fromDeg: -25, toDeg: 35 };

const rad = (d) => (d * Math.PI) / 180;
const arcPoint = (side, deg) => [
  ARCS.cx + side * ARCS.r * Math.cos(rad(deg)),
  ARCS.cy - ARCS.r * Math.sin(rad(deg)),
];

function inPolygon(x, y, pts) {
  let inside = false;
  for (let i = 0, j = pts.length - 1; i < pts.length; j = i++) {
    const [xi, yi] = pts[i];
    const [xj, yj] = pts[j];
    if (yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

function inRoundRect(x, y, { x: rx, y: ry, w, h, r }) {
  const cx = Math.min(Math.max(x, rx + r), rx + w - r);
  const cy = Math.min(Math.max(y, ry + r), ry + h - r);
  return (x - cx) ** 2 + (y - cy) ** 2 <= r * r;
}

function inBell(x, y) {
  if (BELL.circles.some(([cx, cy, r]) => (x - cx) ** 2 + (y - cy) ** 2 <= r * r)) return true;
  return inPolygon(x, y, BELL.polygon) || inRoundRect(x, y, BELL.rim);
}

function inArcs(x, y) {
  const half = ARCS.width / 2;
  for (const side of [-1, 1]) {
    const dx = (x - ARCS.cx) * side;
    const dy = ARCS.cy - y;
    const dist = Math.hypot(dx, dy);
    const deg = (Math.atan2(dy, dx) * 180) / Math.PI;
    if (Math.abs(dist - ARCS.r) <= half && deg >= ARCS.fromDeg && deg <= ARCS.toDeg) return true;
    for (const d of [ARCS.fromDeg, ARCS.toDeg]) {
      const [px, py] = arcPoint(side, d);
      if ((x - px) ** 2 + (y - py) ** 2 <= half * half) return true;
    }
  }
  return false;
}

function render(size, { monochrome = false } = {}) {
  const ss = 4;
  const scale = 512 / size;
  const px = Buffer.alloc(size * size * 4);
  for (let py = 0; py < size; py++) {
    const t = py / (size - 1);
    const bg = BG_TOP.map((c, i) => c + (BG_BOTTOM[i] - c) * t);
    for (let x = 0; x < size; x++) {
      let bell = 0;
      let arc = 0;
      for (let sy = 0; sy < ss; sy++) {
        for (let sx = 0; sx < ss; sx++) {
          const ux = (x + (sx + 0.5) / ss) * scale;
          const uy = (py + (sy + 0.5) / ss) * scale;
          if (inBell(ux, uy)) bell += 1;
          else if (!monochrome && inArcs(ux, uy)) arc += 1;
        }
      }
      const a = (bell + arc * ARC_ALPHA) / (ss * ss);
      const o = (py * size + x) * 4;
      if (monochrome) {
        px[o] = 255;
        px[o + 1] = 255;
        px[o + 2] = 255;
        px[o + 3] = Math.round(a * 255);
      } else {
        for (let i = 0; i < 3; i++) px[o + i] = Math.round(bg[i] * (1 - a) + ACCENT[i] * a);
        px[o + 3] = 255;
      }
    }
  }
  return px;
}

const CRC_TABLE = Array.from({ length: 256 }, (_, n) => {
  let c = n;
  for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
  return c >>> 0;
});
function crc32(buf) {
  let c = 0xffffffff;
  for (const b of buf) c = CRC_TABLE[(c ^ b) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

function chunk(type, data) {
  const len = Buffer.alloc(4);
  len.writeUInt32BE(data.length);
  const body = Buffer.concat([Buffer.from(type, 'ascii'), data]);
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(crc32(body));
  return Buffer.concat([len, body, crc]);
}

export function encodePng(size, rgba) {
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(size, 0);
  ihdr.writeUInt32BE(size, 4);
  ihdr[8] = 8; // bit depth
  ihdr[9] = 6; // RGBA
  const raw = Buffer.alloc(size * (size * 4 + 1));
  for (let y = 0; y < size; y++) rgba.copy(raw, y * (size * 4 + 1) + 1, y * size * 4, (y + 1) * size * 4);
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk('IHDR', ihdr),
    chunk('IDAT', zlib.deflateSync(raw, { level: 9 })),
    chunk('IEND', Buffer.alloc(0)),
  ]);
}

function svg() {
  const hex = (c) => `#${c.map((v) => v.toString(16).padStart(2, '0')).join('')}`;
  const f = (n) => Number(n.toFixed(1));
  const arc = (side) => {
    const [x1, y1] = arcPoint(side, ARCS.toDeg);
    const [x2, y2] = arcPoint(side, ARCS.fromDeg);
    return `<path d="M${f(x1)} ${f(y1)}A${ARCS.r} ${ARCS.r} 0 0 ${side === 1 ? 1 : 0} ${f(x2)} ${f(y2)}"/>`;
  };
  const { x, y, w, h, r } = BELL.rim;
  return [
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">',
    '<defs><linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">',
    `<stop offset="0" stop-color="${hex(BG_TOP)}"/><stop offset="1" stop-color="${hex(BG_BOTTOM)}"/>`,
    '</linearGradient></defs>',
    '<rect width="512" height="512" fill="url(#bg)"/>',
    `<g fill="${hex(ACCENT)}">`,
    ...BELL.circles.map(([cx, cy, cr]) => `<circle cx="${cx}" cy="${cy}" r="${cr}"/>`),
    `<polygon points="${BELL.polygon.map((p) => p.join(',')).join(' ')}"/>`,
    `<rect x="${x}" y="${y}" width="${w}" height="${h}" rx="${r}"/>`,
    '</g>',
    `<g fill="none" stroke="${hex(ACCENT)}" stroke-width="${ARCS.width}" stroke-linecap="round" opacity="${ARC_ALPHA}">`,
    arc(-1),
    arc(1),
    '</g>',
    '</svg>',
    '',
  ].join('\n');
}

const outDir = process.argv[2] ?? 'public/icons';
fs.mkdirSync(outDir, { recursive: true });
const outputs = [
  ['icon-192.png', 192, {}],
  ['icon-512.png', 512, {}],
  ['apple-touch-icon.png', 180, {}],
  ['badge-96.png', 96, { monochrome: true }],
];
for (const [name, size, opts] of outputs) {
  fs.writeFileSync(path.join(outDir, name), encodePng(size, render(size, opts)));
}
fs.writeFileSync(path.join(outDir, 'icon.svg'), svg());
console.log(`wrote ${outputs.length + 1} icons to ${outDir}`);
