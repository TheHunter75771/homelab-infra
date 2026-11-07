// Plain-language explanations of what the server reports. Pure functions; the output is plain text that the
// views put on the page with textContent. Server strings (game names, errors) are inserted as text, never markup.

import { pct, str, windowText } from './format.js';

export const GPU = "the gaming PC's GPU";
export const CPU = "ai-box's CPU";

/**
 * How the gate failed, from S's structured fields (no text parsing):
 * `gate_error_kind` ∈ http | network | stale | shape | null, `gate_http_status` (int | null), and
 * `gate_error` (S's short text, shown as-is in brackets). A missing or unknown kind gets a generic message.
 * Returns { kind, phrase, row } or null when there's no error. `row` is the Routing tab's "Gate" value.
 */
export function gateProblem(routing) {
  if (!routing || typeof routing !== 'object') return null;
  const err = str(routing.gate_error, '');
  const kind = routing.gate_error_kind;
  if (!err && (kind === null || kind === undefined)) return null;
  const code = Number.isInteger(routing.gate_http_status) && routing.gate_http_status >= 100 && routing.gate_http_status <= 599 ? routing.gate_http_status : null;
  const note = err ? ` (${err})` : '';
  switch (kind) {
    case 'http': {
      // The gate's /status returns 503 only when it has no fresh sample (CONTRACTS §12.9).
      const what = code === 503 ? 'HTTP 503: no fresh sample' : code !== null ? `HTTP ${code}` : null;
      return { kind, phrase: `the GPU gate replied with an error${what ? ` (${what})` : note}`, row: `Replied with an error${what ? `: ${what}` : note}` };
    }
    case 'network':
      return { kind, phrase: `the GPU gate isn't answering${note}`, row: `Not answering${note}` };
    case 'stale':
      return { kind, phrase: `AI Control has no fresh reading from the GPU gate${note}`, row: `No fresh reading${note}` };
    case 'shape':
      return { kind, phrase: `the GPU gate replied with something AI Control can't use${note}`, row: `Unusable reply${note}` };
    default:
      return { kind: 'unknown', phrase: `the GPU gate can't be read${note}`, row: `Can't be read${note}` };
  }
}

/** "over 30 min" from routing.window_s, or "over the gate's averaging window" when the server didn't say. */
export function overWindow(routing) {
  const w = windowText(routing && routing.window_s);
  return w ? `over ${w}` : "over the gate's averaging window";
}

const isNum = (v) => typeof v === 'number' && Number.isFinite(v);

function limitText(threshold) {
  return isNum(threshold) ? pct(threshold) : '50%';
}

function gameText(game) {
  const g = str(game, '');
  return g ? ` (${g})` : '';
}

/**
 * routing (from /api/summary) → { target: 'gpu'|'cpu'|null, headline, sentence, status, consistent }.
 * `status` is 'ok' on the GPU, 'warn' on the CPU (still answering, just slower), 'down' when unknown.
 */
export function explainRouting(routing) {
  if (!routing || typeof routing !== 'object' || 'error' in routing) {
    const err = routing && str(routing.error, '');
    return {
      target: null,
      headline: 'Unknown',
      status: 'unknown',
      consistent: true,
      sentence: `AI Control can't tell where answers are going right now${err ? `: ${err}` : '.'}`,
    };
  }
  const { to, reason, avg30, game, threshold } = routing;
  const limit = limitText(threshold);
  const over = overWindow(routing);
  const avg = isNum(avg30) ? pct(avg30) : null;

  if (to !== 'gpu' && to !== 'cpu') {
    if (reason === 'broker_down') {
      return { target: null, headline: 'No answers', status: 'down', consistent: true, sentence: "The router (broker) is down. AI answers aren't available." };
    }
    const gp = gateProblem(routing);
    return {
      target: null,
      headline: 'Unknown',
      status: 'unknown',
      consistent: true,
      sentence: `AI Control can't tell where answers are going right now${gp ? `: ${gp.phrase}` : ''}.`,
    };
  }

  const where = to === 'gpu' ? GPU : CPU;
  const headline = to === 'gpu' ? 'Gaming PC GPU' : 'ai-box CPU';
  const status = to === 'gpu' ? 'ok' : 'warn';
  const base = { target: to, headline, status, consistent: true };

  const gp = gateProblem(routing);
  if (gp) {
    if (to === 'cpu') {
      return {
        ...base,
        sentence: `Answers are going to ${CPU} because ${gp.phrase}. The broker treats a gate it can't use as "GPU not available".`,
      };
    }
    return {
      ...base,
      sentence: `Answers are going to ${where}, but ${gp.phrase}, so this may be out of date.`,
    };
  }

  if (to === 'gpu' && reason === 'no_game') {
    return { ...base, sentence: `Answers are going to ${GPU} because no game is running.` };
  }
  if (to === 'gpu' && reason === 'below_threshold') {
    return {
      ...base,
      sentence:
        `Answers are going to ${GPU}: a game is running${gameText(game)}, but the GPU has averaged ` +
        `${avg ?? 'less than the limit'} ${over} (limit ${limit}).`,
    };
  }
  if (to === 'cpu' && reason === 'game_busy') {
    return {
      ...base,
      sentence:
        `Answers are going to ${CPU} because a game is running${gameText(game)} and the GPU has averaged ` +
        `${avg ?? 'at least the limit'} ${over} (limit ${limit}).`,
    };
  }
  if (to === 'cpu' && reason === 'game_no_samples') {
    return {
      ...base,
      sentence:
        `Answers are going to ${CPU} because a game is running${gameText(game)} and the gate has no GPU ` +
        `samples yet to average, so it plays safe.`,
    };
  }
  const code = str(reason, 'none');
  const known = ['no_game', 'below_threshold', 'game_busy', 'game_no_samples'].includes(reason);
  return {
    ...base,
    consistent: !known,
    sentence: known
      ? `Answers are going to ${where}, but the gate's reason ("${code}") points the other way. The broker may have failed over, or the gate just changed its mind.`
      : `Answers are going to ${where}. The gate gave a reason AI Control doesn't know: "${code}".`,
  };
}

/** The rule itself, in words, for the Routing tab. */
export function ruleSentences(routing) {
  const limit = limitText(routing && routing.threshold);
  const w = windowText(routing && routing.window_s);
  return [
    `The gaming PC lends its GPU to the AI whenever no game is running.`,
    `While a game runs, the GPU stays available only if the game's GPU use has averaged under ${limit} ${w ? `over the last ${w}` : "over the gate's averaging window"}.`,
    `"Game GPU use" is how busy the GPU is minus the share Ollama itself uses, so AI work never counts against the game.`,
    `When the GPU isn't available, answers go to ${CPU}: slower, but always there. The gate also unloads models from the GPU so the game gets its memory back.`,
  ];
}

export function uidMatchNote(routing) {
  if (!routing || routing.uid_match !== false) return null;
  return "The gate couldn't match Ollama by its user id, so it recognises Ollama by process name only. Ollama's GPU share may be less exact.";
}

const SERVICE_LABELS = {
  gate: 'GPU gate',
  'pc-ollama': 'Gaming PC Ollama (GPU)',
  'cpu-ollama': 'ai-box Ollama (CPU)',
  broker: 'Broker',
  webui: 'Open WebUI',
  helper: 'ai-box helper',
  agent: 'Gaming PC agent',
};

export function serviceLabel(id) {
  return Object.prototype.hasOwnProperty.call(SERVICE_LABELS, id) ? SERVICE_LABELS[id] : str(id, 'Unknown service');
}

const STATUS_WORDS = { ok: 'OK', warn: 'Warning', down: 'Down', unknown: 'Unknown', crit: 'Critical' };

/** Status → the word shown next to the icon (status is never colour alone). Unknown values read "Unknown". */
export function statusWord(status) {
  return Object.prototype.hasOwnProperty.call(STATUS_WORDS, status) ? STATUS_WORDS[status] : 'Unknown';
}

/** Normalises a status to one of the four the UI knows, so a hostile value can't pick a CSS class. */
export function statusKey(status) {
  return ['ok', 'warn', 'down', 'unknown', 'crit'].includes(status) ? status : 'unknown';
}

export function backendLabel(backend) {
  if (backend === 'gpu') return 'GPU';
  if (backend === 'cpu') return 'CPU';
  if (backend === 'broker') return 'Broker';
  return str(backend);
}
