// Synthetic fixtures (public repo): no real IPs, tailnet names or users. Model names are public ones.

const NOW = Date.parse('2026-10-26T20:00:00Z');
export { NOW };
const sec = (minAgo) => (NOW - minAgo * 60_000) / 1000; // the server sends epoch seconds everywhere
const GB = 1e9;

export function normal() {
  return {
    server: { time: sec(0), timeZone: 'America/New_York', version: '0.1.0', identity: { login: 'test-user', name: 'Test User' } },
    routing: { to: 'gpu', reason: 'no_game', avg30: 3.2, game: null, gpu_busy: 41, ollama_share: 39, uid_match: true, threshold: 50, window_s: 1800, gate_error: null, gate_error_kind: null, gate_http_status: null },
    services: [
      { id: 'gate', status: 'ok', detail: 'answering, 360 samples', since: sec(600) },
      { id: 'pc-ollama', status: 'ok', detail: 'v0.12.6', since: sec(600) },
      { id: 'cpu-ollama', status: 'ok', detail: 'healthy', since: sec(1200) },
      { id: 'broker', status: 'ok', detail: 'healthy', since: sec(1200) },
      { id: 'webui', status: 'ok', detail: 'healthy', since: sec(1200) },
      { id: 'helper', status: 'ok', detail: 'socket ready', since: sec(1200) },
      { id: 'agent', status: 'ok', detail: 'answering', since: sec(600) },
    ],
    resources: {
      aibox: { load1: 0.42, mem_total: 8 * GB, mem_avail: 5.1 * GB, swap_used: 0, disk: [{ mount: '/', total: 500 * GB, avail: 410 * GB }] },
      pc: { disk: [{ mount: '/', total: 1000 * GB, avail: 520 * GB }, { mount: '/mnt/share', total: 2000 * GB, avail: 150 * GB }] },
      vram: { model: 'llama3.1:8b', size_vram: 6.1 * GB },
    },
    models: {
      gpu: [
        { name: 'llama3.1:8b', size: 4.9 * GB, quant: 'Q4_K_M', modified: sec(60 * 24 * 3) },
        { name: 'qwen2.5-coder:7b', size: 4.7 * GB, quant: 'Q4_K_M', modified: sec(60 * 24 * 10) },
      ],
      cpu: [{ name: 'llama3.2:3b', size: 2.0 * GB, quant: 'Q4_K_M', modified: sec(60 * 24 * 5) }],
      loaded: { gpu: [{ name: 'llama3.1:8b', size_vram: 6.1 * GB, expires_at: sec(-4) }], cpu: [] },
      alias: { assistant: { gpu: 'llama3.1:8b', cpu: 'llama3.2:3b' } },
    },
    clock: { synced: true, offset_ms: 0.4, source: 'pool' },
    timers: [{ unit: 'homelab-ai-digest.timer', next: sec(-120), last: sec(1320), last_result: 'success' }],
    alerts: [],
  };
}

export function gamingCpu() {
  const s = normal();
  s.routing = { to: 'cpu', reason: 'game_busy', avg30: 62, game: 'Cyberpunk2077.exe', gpu_busy: 97, ollama_share: 0, uid_match: true, threshold: 50, window_s: 1800, gate_error: null, gate_error_kind: null, gate_http_status: null };
  s.resources.vram = null;
  s.models.loaded.gpu = [];
  s.models.loaded.cpu = [{ name: 'llama3.2:3b', size_vram: 0, expires_at: sec(-5) }];
  return s;
}

export function pcOff() {
  const s = normal();
  s.routing = { to: 'cpu', reason: null, avg30: null, game: null, gpu_busy: null, ollama_share: null, uid_match: null, threshold: 50, window_s: 1800, gate_error: 'timed out after 4 s', gate_error_kind: 'network', gate_http_status: null };
  for (const sv of s.services) if (['gate', 'pc-ollama', 'agent'].includes(sv.id)) Object.assign(sv, { status: 'down', detail: 'not answering', since: sec(45) });
  s.resources.pc = { error: 'host agent not answering' };
  s.resources.vram = null;
  s.models.gpu = { error: 'host agent not answering' };
  s.models.loaded.gpu = [];
  s.alerts = [
    { id: 'gate-unreachable', level: 'warn', text: 'The GPU gate is unreachable. Answers go to the CPU.' },
    { id: 'backend-down-pc-ollama', level: 'warn', text: 'Gaming PC Ollama is down.' },
  ];
  return s;
}

export function everythingDown() {
  return {
    server: { time: sec(0), timeZone: 'America/New_York', version: '0.1.0', identity: null },
    routing: { to: null, reason: 'broker_down', avg30: null, game: null, gpu_busy: null, ollama_share: null, uid_match: null, threshold: 50, window_s: 1800, gate_error: 'connection refused', gate_error_kind: 'network', gate_http_status: null },
    services: ['gate', 'pc-ollama', 'cpu-ollama', 'broker', 'webui', 'helper', 'agent'].map((id) => ({ id, status: id === 'helper' ? 'unknown' : 'down', detail: 'not answering', since: sec(30) })),
    resources: { error: 'proc read failed' },
    models: { error: 'broker not answering' },
    clock: { error: 'chronyc failed' },
    timers: { error: 'helper not answering' },
    alerts: [
      { id: 'backend-down-cpu', level: 'crit', text: 'ai-box Ollama is down. Nothing can answer.' },
      { id: 'clock', level: 'warn', text: 'The clock is not synced.' },
    ],
  };
}

export function history({ busy = 40, share = 38, minutes = 30, available = true } = {}) {
  const pts = [];
  for (let i = 0; i <= minutes * 6; i += 6) {
    const ts = (NOW - (minutes * 60 - i * 10) * 1000) / 1000;
    pts.push({ ts, gpu_busy: busy + ((i * 7) % 11), ollama_share: share, avg30: Math.max(0, busy - share) + 1, available });
  }
  return pts;
}

export function requests() {
  return [
    { ts: sec(1), path: '/api/chat', alias: 'assistant', model: 'llama3.1:8b', backend: 'gpu', status: 200, duration_ms: 2300, eval_count: 180, eval_tok_s: 41.7, error: null },
    { ts: sec(9), path: '/api/chat', alias: null, model: 'llama3.2:3b', backend: 'cpu', status: 200, duration_ms: 14200, eval_count: 90, eval_tok_s: 7.3, error: null },
    { ts: sec(30), path: '/api/generate', alias: 'assistant', model: 'llama3.1:8b', backend: 'gpu', status: 502, duration_ms: 120, eval_count: null, eval_tok_s: null, error: 'backend closed the connection' },
  ];
}

export function logs() {
  return ['2026-10-26T19:58:01Z ollama[100]: loaded model', '2026-10-26T19:58:02Z ollama[100]: error: context full', '2026-10-26T19:59:00Z ollama[100]: request done'];
}

/** GET /api/logs/sources: [{id, label}] from S's config. */
export function logSources() {
  return [
    { id: 'ollama-cpu', label: 'ai-box Ollama (CPU)' },
    { id: 'pc:ollama', label: 'Gaming PC Ollama (GPU)' },
    { id: 'homelab-ai-stack', label: 'AI stack' },
  ];
}

/** Sudo rows with `event`: "command" | "refused" | "auth_fail" and `ambiguous` (helper X / agent Y). */
export function sudo() {
  return [{ ts: sec(5), user: 'claude', runas: 'root', cwd: '/home/claude', command: 'systemctl status ollama', event: 'command', ambiguous: false }];
}

export function sudoFlagged() {
  return [
    { ts: sec(2), user: 'localai', runas: 'root', cwd: '/home/localai', command: 'cat /etc/shadow', event: 'refused', ambiguous: false },
    { ts: sec(3), user: 'localai', runas: 'root', cwd: '/home/localai', command: 'rm -rf /var/log', event: 'refused', ambiguous: false },
    { ts: sec(4), user: 'localai', runas: 'root', cwd: '/home/localai', command: 'apt-get install nmap', event: 'refused', ambiguous: false },
    { ts: sec(6), user: 'localai', runas: 'root', cwd: '/home/localai', command: 'su -', event: 'auth_fail', ambiguous: false },
    { ts: sec(9), user: 'localai', runas: 'root', cwd: '/home/localai', command: 'systemctl status ollama', event: 'command', ambiguous: false },
  ];
}

export function sessions() {
  return [
    { id: '00000A', ts: sec(5), runas: 'root', tty: 'pts/0', cwd: '/home/claude', command: 'apt-get update' },
    { id: 'bad id with spaces', ts: sec(6), runas: 'root', tty: 'pts/0', cwd: '/', command: 'ls' },
  ];
}

/** The helper's sudo-replay object plus id and user; `output` only when output=1 was asked for. */
/** GET /api/audit/sessions: {"sessions": [...], "skipped": N} (N session logs couldn't be read). */
/** `ambiguous: true` rows (cwd/command may be spoofed or cut) and non-bool values that must count as false. */
export function sudoAmbiguous() {
  return [
    { ts: sec(2), user: 'claude', runas: 'root', cwd: '/tmp/x\n/home/claude', command: 'systemctl restart ollama', event: 'command', ambiguous: true },
    { ts: sec(3), user: 'claude', runas: 'root', cwd: '/root', command: 'cat /etc/shadow', event: 'refused', ambiguous: true },
    { ts: sec(4), user: 'claude', runas: 'root', cwd: '/', command: 'ls', event: 'command', ambiguous: 'true' },
    { ts: sec(5), user: 'claude', runas: 'root', cwd: '/', command: 'id', event: 'command', ambiguous: 1 },
    { ts: sec(6), user: 'claude', runas: 'root', cwd: '/', command: 'uptime', event: 'command' },
  ];
}

export function sessionsReply(skipped = 0) {
  return { sessions: sessions(), skipped };
}

export function replay(output = true) {
  return { id: '00000A', user: 'claude', ts: sec(5), command: 'apt-get update', cwd: '/home/claude', runas: 'root', tty: 'pts/0', output_truncated: false, ...(output ? { output: 'Hit:1 archive\nReading package lists... Done\n' } : {}) };
}

export function localai() {
  return [{ ts: sec(3), unit: 'session-4.scope', comm: 'python3', message: 'started a job' }];
}

export const HOSTILE = '<img src=x onerror=alert(1)> javascript:alert(1) "><script>alert(2)</script>';

/** Every string in `v` replaced by HOSTILE, recursively (keys stay). */
export function hostile(v) {
  if (typeof v === 'string') return HOSTILE;
  if (Array.isArray(v)) return v.map(hostile);
  if (v && typeof v === 'object') return Object.fromEntries(Object.entries(v).map(([k, x]) => [k, hostile(x)]));
  return v;
}

/** A hostile summary that also keeps a few fields valid so the renderers reach their data paths. */
export function hostileSummary() {
  const s = hostile(normal());
  s.routing.to = 'cpu'; // a valid route, with a hostile reason and game
  s.routing.reason = 'game_busy';
  s.routing.game = HOSTILE;
  Object.assign(s.routing, { gate_error: HOSTILE, gate_error_kind: HOSTILE, gate_http_status: HOSTILE });
  s.services.push({ id: 'gate', status: 'ok', detail: HOSTILE, since: HOSTILE });
  s.models.alias.assistant = { gpu: HOSTILE, cpu: HOSTILE };
  s.models.gpu.push({ name: HOSTILE, size: 1, quant: HOSTILE, modified: HOSTILE });
  s.models.loaded.gpu = [{ name: HOSTILE, size_vram: 1, expires_at: HOSTILE }];
  s.alerts = [{ id: HOSTILE, level: 'crit', text: HOSTILE }, { id: HOSTILE, level: HOSTILE, text: HOSTILE }];
  s.timers = [{ unit: HOSTILE, next: HOSTILE, last: HOSTILE, last_result: HOSTILE }];
  return s;
}
