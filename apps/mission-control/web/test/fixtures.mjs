// Synthetic fixtures (public repo): no real IPs, tailnet names, logins or money. Hosts use *.example.test.

export const NOW = Date.parse('2026-10-28T12:00:00Z');
const sec = (minAgo) => (NOW - minAgo * 60_000) / 1000; // the server sends epoch seconds

export const OPEN_WEBUI = 'https://ai-box.example.test';
// The shape of a VAPID public key (65 bytes: 0x04 + a P-256 point, base64url), built here so no key-like
// literal sits in the repo (gitleaks). Not a real key.
export const VAPID = Buffer.from([4, ...Array.from({ length: 64 }, (_, i) => (i * 37) % 256)]).toString('base64url');

export function cards() {
  return {
    generated_at: sec(0),
    config: { open_webui_url: OPEN_WEBUI, vapid_public_key: VAPID },
    cards: {
      ai: {
        status: 'ok',
        headline: 'Answers go to the gaming PC GPU',
        items: [{ label: 'Open WebUI', value: 'healthy', status: 'ok' }, { label: 'Router', value: 'healthy', status: 'ok' }],
        link: OPEN_WEBUI,
        error: null,
        updated_at: sec(1),
      },
      ai_control: {
        status: 'ok',
        headline: 'All 7 AI services OK',
        items: [{ label: 'GPU gate', value: 'answering', status: 'ok' }],
        link: 'https://ai-box.example.test/',
        error: null,
        updated_at: sec(1),
      },
      money: {
        status: 'warn',
        headline: '2 bills due this week',
        items: [
          { label: 'Bank sync', value: 'OK 2 h ago', status: 'ok' },
          { label: 'Phone bill', value: 'due Thu', status: 'warn' },
          { label: 'Groceries left', value: '61%' },
        ],
        link: 'http://money.example.test:8710/',
        error: null,
        updated_at: sec(2),
      },
      school: {
        status: 'ok',
        headline: '1 deadline in the next 48 h',
        items: [{ label: 'MATH 111 quiz 4', value: 'Wed 23:59', status: 'warn' }],
        link: 'https://erau.example.test/',
        error: null,
        updated_at: sec(3),
      },
      home: { status: 'setup', headline: 'Connect Home Assistant', items: [], link: 'http://ha.example.test:8123/', error: null, updated_at: sec(1) },
      servers: {
        status: 'ok',
        headline: 'Backups fresh',
        items: [
          { label: 'ai-box backup', value: '6 h ago', status: 'ok' },
          { label: 'PC pull', value: '7 h ago', status: 'ok' },
          { label: 'Mediaserver backup', value: 'unknown', status: 'unknown' },
        ],
        link: 'https://dash.example.test/',
        error: null,
        updated_at: sec(1),
      },
      alerts: { status: 'ok', headline: '', items: [], link: null, error: null, updated_at: sec(1) },
    },
  };
}

/** The Apps card (B1): the server sends it only when its config has app_links. */
export function apps() {
  return {
    status: 'ok',
    headline: '3 apps',
    items: [
      { label: 'Open WebUI', value: '', status: null, url: 'https://ai-box.example.test/' },
      { label: 'Money Hub', value: '', status: null, url: 'http://money.example.test:8710/' },
      { label: 'Jellyfin', value: '', status: null, url: 'http://media.example.test:8096/web/' },
    ],
    link: null,
    error: null,
    updated_at: null,
  };
}

export function cardsWithApps() {
  const c = cards();
  c.cards.apps = apps();
  return c;
}

/** Every source failing in a different way, plus cards the server left out or sent oddly. */
export function cardsBroken() {
  const c = cards();
  c.config = { open_webui_url: 'javascript:alert(1)', vapid_public_key: 'not a key' };
  c.cards.ai = { status: 'down', headline: '', items: [], link: null, error: 'Open WebUI /health timed out after 4 s', updated_at: sec(1) };
  c.cards.money = { status: 'down', error: 'HTTP 502 from Money Hub', updated_at: sec(1) };
  c.cards.school = { status: 'purple', headline: 42, items: 'nope', link: 'ftp://x.example.test/', error: { nested: true }, updated_at: '2026-10-28' };
  delete c.cards.servers;
  c.cards.home = null;
  c.cards.alerts = { status: 'down', headline: 'Bank sync failing for 52 h', items: [{ label: 'Bank sync failing for 52 h', status: 'down' }, 'Disk / at 93%', { nope: 1 }], error: null, updated_at: sec(0) };
  return c;
}

export function briefing() {
  return {
    date: '2026-10-28',
    generated_at: sec(390),
    headline: 'Wednesday 28 October',
    summary: '1 deadline, 2 bills, no alerts',
    sections: [
      { id: 'school', title: 'School: next 48 h', items: [{ label: 'MATH 111 quiz 4', value: 'Wed 23:59' }], empty: 'Nothing due.' },
      { id: 'bills', title: 'Bills this week', items: [{ label: 'Phone bill', value: 'Thu' }, { label: 'Car insurance', value: 'Sat' }], empty: 'No bills due.' },
      { id: 'alerts', title: 'Alerts', items: [], empty: 'No alerts.' },
      { id: 'backups', title: 'Backups', items: [{ label: 'All backups fresh', status: 'ok' }], empty: '' },
    ],
  };
}

export const HOSTILE = `<img src=x onerror=alert(1)> javascript:alert(1) "><script>alert(1)</script>`;

/** Every string value (not key) replaced by HOSTILE; links get hostile variants that still parse as URLs. */
export function hostile(v, key) {
  if (key === 'link' || key === 'url') return `https://evil.example.test/"><img src=x onerror=alert(1)>`;
  if (key === 'open_webui_url') return `https://evil.example.test/?a="><script>alert(1)</script>#<img src=x>`;
  if (typeof v === 'string') return HOSTILE;
  if (Array.isArray(v)) return v.map((x) => hostile(x));
  if (v && typeof v === 'object') return Object.fromEntries(Object.entries(v).map(([k, x]) => [k, hostile(x, k)]));
  return v;
}
