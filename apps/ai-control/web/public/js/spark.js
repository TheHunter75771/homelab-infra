// The GPU sparkline (over the gate's window): inline SVG from /api/gate/history, with a text alternative written out in full.

import { h, s } from './dom.js';
import { historySeries, historyStats } from './state.js';
import { pct, windowText } from './format.js';

const W = 300;
const H = 64;
let uid = 0;

function polyline(series, key) {
  const n = series.length;
  const pts = [];
  series.forEach((p, i) => {
    const v = p[key];
    if (v === null) return;
    const x = n > 1 ? (i / (n - 1)) * W : W / 2;
    const y = H - (v / 100) * H;
    pts.push(`${x.toFixed(1)},${y.toFixed(1)}`);
  });
  return pts.join(' ');
}

/**
 * The sentence that stands in for the graph (also shown under it).
 * `minutes` is the span fetched; `windowS` the gate's averaging window (routing.window_s).
 */
export function sparkSummary(points, { threshold = 50, minutes = 30, windowS } = {}) {
  const series = historySeries(points);
  const span = windowText(minutes * 60);
  if (!series.length) return 'No GPU history yet.';
  const st = historyStats(series);
  const w = windowText(windowS);
  const parts = [
    `Last ${span}: game GPU use is ${pct(st.now)} now, peaked at ${pct(st.peak)}`,
    `the ${w ? `${w} ` : ''}average is ${pct(st.avg)} (limit ${pct(threshold)})`,
  ];
  let tail = '';
  if (st.unavailableCount === 0) tail = 'The GPU was available the whole time.';
  else if (st.unavailableCount === st.count) tail = 'The GPU was not available at any point.';
  else tail = `The GPU was unavailable for ${Math.round((st.unavailableCount / st.count) * 100)}% of this time.`;
  return `${parts.join('; ')}. ${tail}`;
}

/** points → a <figure> with the SVG (decorative to screen readers) and the summary as visible text. */
export function sparkline(points, { threshold = 50, minutes = 30, windowS } = {}) {
  const series = historySeries(points);
  const id = `spark-${++uid}`;
  const summary = sparkSummary(points, { threshold, minutes, windowS });
  const t = Number.isFinite(threshold) ? Math.min(100, Math.max(0, threshold)) : 50;
  const ty = (H - (t / 100) * H).toFixed(1);
  const svg = s(
    'svg',
    { class: 'spark', viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: 'none', role: 'img', 'aria-labelledby': id, focusable: 'false' },
    s('line', { class: 'spark-limit', x1: 0, x2: W, y1: ty, y2: ty, 'stroke-dasharray': '4 4', 'vector-effect': 'non-scaling-stroke' }),
    series.length ? s('polyline', { class: 'spark-game', points: polyline(series, 'game'), fill: 'none', 'vector-effect': 'non-scaling-stroke' }) : null,
    series.length ? s('polyline', { class: 'spark-avg', points: polyline(series, 'avg'), fill: 'none', 'vector-effect': 'non-scaling-stroke' }) : null,
  );
  return h(
    'figure',
    { class: 'spark-fig' },
    svg,
    h(
      'div',
      { class: 'spark-legend', 'aria-hidden': 'true' },
      h('span', { class: 'lg lg-game' }, 'Game GPU use (thin)'),
      h('span', { class: 'lg lg-avg' }, `${windowText(windowS) ? `${windowText(windowS)} ` : ''}average (thick)`),
      h('span', { class: 'lg lg-limit' }, `Limit ${pct(t)} (dashed)`),
    ),
    h('figcaption', { id, class: 'spark-caption' }, summary),
  );
}
