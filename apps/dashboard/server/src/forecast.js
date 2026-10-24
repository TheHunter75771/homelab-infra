// Space forecast (CONTRACTS §11.3): least-squares fit of used bytes over the daily samples of the last 30
// days. Fewer than 7 samples in that window gives no forecast. Thresholds are projected from the latest
// sample along the fitted slope.
import { addDays, daysBetween } from './util/time.js';

export const WINDOW_DAYS = 30;
export const MIN_SAMPLES = 7;

// points: [{x, y}] -> {slope, intercept} or null when x has no spread.
export function leastSquares(points) {
  const n = points.length;
  if (n < 2) return null;
  let sx = 0;
  let sy = 0;
  for (const p of points) {
    sx += p.x;
    sy += p.y;
  }
  const mx = sx / n;
  const my = sy / n;
  let sxx = 0;
  let sxy = 0;
  for (const p of points) {
    sxx += (p.x - mx) ** 2;
    sxy += (p.x - mx) * (p.y - my);
  }
  if (sxx === 0) return null;
  const slope = sxy / sxx;
  return { slope, intercept: my - slope * mx };
}

function crossing(fraction, latest, slope, today) {
  const target = fraction * latest.total;
  const fromLatest = (target - latest.used) / slope; // days after the latest sample
  if (latest.used >= target) return { date: latest.date, inDays: daysBetween(today, latest.date), reached: true };
  const date = addDays(latest.date, Math.ceil(fromLatest));
  return { date, inDays: daysBetween(today, date), reached: false };
}

// samples: [{date: 'YYYY-MM-DD', used, total}] in any order; today: 'YYYY-MM-DD' (host-local).
export function forecastDrive(samples, today, { windowDays = WINDOW_DAYS, minSamples = MIN_SAMPLES } = {}) {
  const valid = samples
    .filter((s) => typeof s.date === 'string' && Number.isFinite(s.used) && Number.isFinite(s.total) && s.total > 0)
    .filter((s) => {
      const age = daysBetween(s.date, today);
      return age >= 0 && age < windowDays;
    })
    .sort((a, b) => (a.date < b.date ? -1 : 1));
  // one sample per day (the last one wins)
  const byDay = new Map(valid.map((s) => [s.date, s]));
  const window = [...byDay.values()];
  if (window.length < minSamples) {
    return { forecast: null, reason: 'not-enough-samples', samplesInWindow: window.length, minSamples };
  }
  const first = window[0].date;
  const fit = leastSquares(window.map((s) => ({ x: daysBetween(first, s.date), y: s.used })));
  const latest = window.at(-1);
  if (!fit || fit.slope <= 0) {
    return {
      forecast: null,
      reason: 'not-growing',
      samplesInWindow: window.length,
      minSamples,
      slopeBytesPerDay: fit ? Math.round(fit.slope) : 0,
    };
  }
  return {
    forecast: {
      method: 'least-squares',
      windowDays,
      samplesInWindow: window.length,
      slopeBytesPerDay: Math.round(fit.slope),
      full: crossing(1, latest, fit.slope, today),
      at90: crossing(0.9, latest, fit.slope, today),
      at97: crossing(0.97, latest, fit.slope, today),
    },
    reason: null,
    samplesInWindow: window.length,
    minSamples,
  };
}
