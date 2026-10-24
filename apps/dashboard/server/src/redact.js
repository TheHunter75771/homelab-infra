// Keeps secrets out of anything the dashboard shows or logs. Known secret values (API keys read from the
// apps' config files, the keys in settings, the VAPID private key) are replaced wherever they appear, and
// common "key=value" credential shapes are masked even when the value isn't known.
// "apikey=…", "api_key=…", "API Key: …", "X-Api-Key: …", "token=…", "password: …", "Authorization: …", also
// percent-encoded ("apikey%3D…", "API%20Key%3A%20…": a URL inside another URL or a stack frame). Over-redacting
// an encoded run is fine; leaking a key is not.
const SP = '(?:\\s|%20)';
const CREDENTIAL_RE = new RegExp(
  `((?:x-api-key|api(?:[\\s_-]|%20)?key|apikey|access[_-]?token|token|password|passwd|secret|authorization)["']?${SP}*(?:[:=]|%3[ad])${SP}*(?:bearer${SP}+|mediabrowser${SP}+token=)?["']?)([^\\s"'&,;]{6,})`,
  'gi',
);
// a bare "Bearer <token>" (or "Bearer%20<token>")
const BEARER_RE = /(\bbearer(?:\s|%20)+)([A-Za-z0-9._~+/=%-]{8,})/gi;

export function redactCredentials(text) {
  return String(text).replace(CREDENTIAL_RE, '$1[redacted]').replace(BEARER_RE, '$1[redacted]');
}

export function createSecrets() {
  const values = new Set();
  return {
    add(value) {
      if (typeof value === 'string' && value.length >= 8) values.add(value);
    },
    size: () => values.size,
    redact(text) {
      let out = String(text);
      for (const v of values) if (out.includes(v)) out = out.split(v).join('[redacted]');
      return redactCredentials(out);
    },
  };
}

const ANSI_RE = /\x1b\[[0-9;?]*[ -/]*[@-~]/g;

const LEVELS = [
  [/\b(?:ERROR|ERR|FATAL|FTL|CRIT(?:ICAL)?|PANIC)\b|\|Error\||\[Error\]|\[Fatal\]|::ERROR::/i, 'ERR'],
  [/\b(?:WARN(?:ING)?|WRN)\b|\|Warn\||\[Warn\]|::WARNING::/i, 'WRN'],
  [/\b(?:DEBUG|DBG|TRACE|TRC|VERBOSE|VRB)\b|\|Debug\||\[Debug\]|\[Trace\]|::DEBUG::/i, 'DBG'],
  [/\b(?:INFO|INF|NOTICE)\b|\|Info\||\[Info\]|::INFO::/i, 'INF'],
];

// Level from the first 80 characters (where every app here puts it), INF when none is found.
export function logLevel(line) {
  const head = line.slice(0, 80);
  for (const [re, level] of LEVELS) if (re.test(head)) return level;
  return 'INF';
}

// helper event {type: "line", ts, text} -> {ts, level, msg}
export function parseLogItem(item, secrets) {
  const raw = typeof item?.text === 'string' ? item.text : typeof item?.line === 'string' ? item.line : '';
  const clean = raw.replace(ANSI_RE, '').replace(/[\u0000-\u0008\u000b-\u001f\u007f]/g, '');
  const redacted = secrets ? secrets.redact(clean) : clean; // redact first, then truncate: a cut never leaves part of a key
  const ts = typeof item?.ts === 'string' && !Number.isNaN(Date.parse(item.ts)) ? new Date(item.ts).toISOString() : null;
  return {
    ts,
    level: logLevel(redacted),
    msg: redacted.slice(0, 4000),
  };
}
