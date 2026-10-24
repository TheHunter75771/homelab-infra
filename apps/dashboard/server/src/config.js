// Configuration, lowest to highest precedence:
//   1. built-in defaults (the contract's paths)
//   2. the JSON file named by DASHBOARD_CONFIG (snake_case keys): role `dashboard` renders
//      /etc/homelab-dashboard/config.json from roles/dashboard/templates/config.json.j2
//   3. environment variables: push-core's HOST, PORT, PUSH_DIR, LOG_LEVEL, PUSH_ENDPOINT_HOSTS, and DASHBOARD_*
// File values are type-checked: a bad one is reported (warnings) and its default used. A bad env var is fatal.
// No secrets live here: Jellyfin/Jellyseerr keys are in settings.json, *arr/SABnzbd keys in their own files.
import nodeFs from 'node:fs';
import os from 'node:os';
import { DEFAULT_APPS, DEFAULT_INTEGRATIONS } from './catalog.js';

// DASHBOARD_* env var -> config key (and how to parse it)
export const ENV_KEYS = {
  DASHBOARD_REMOTE_ENABLED: ['remoteEnabled', 'bool'],
  DASHBOARD_DATA_DIR: ['stateDir'],
  DASHBOARD_WEB_DIR: ['webDir'],
  DASHBOARD_DOCKERCTL_SOCKET: ['helperSocket'],
  DASHBOARD_BOOTGATE_DIR: ['gateDir'],
  DASHBOARD_BOOTGATE_RERUN: ['gateRerunFile'],
  DASHBOARD_BOOTGATE_RETRY_TIMER: ['gateRetryTimer'],
  DASHBOARD_BOOTGATE_RETRY_MINUTES: ['gateRetryMinutes', 'int'],
  DASHBOARD_NOTIFY_HISTORY: ['notifyHistoryFile'],
  DASHBOARD_BACKUP_STATUS: ['backupStatusFile'],
  DASHBOARD_MEDIA_MOUNT: ['mediaMount'],
  DASHBOARD_APPS_CONFIG_DIR: ['dataDir'],
  DASHBOARD_REHEARSAL_FLAG: ['rehearsalFlag'],
  DASHBOARD_DISPLAY_NAME: ['displayName'],
  DASHBOARD_TIMEZONE: ['timezone'],
};

const pkgVersion = JSON.parse(nodeFs.readFileSync(new URL('../package.json', import.meta.url), 'utf8')).version;

export const DEFAULTS = Object.freeze({
  listenHost: '127.0.0.1',
  listenPort: 8085,
  displayName: undefined, // the box's own hostname (never committed to the repo)
  timezone: undefined, // resolved at start (util/tz.js)
  timezoneFile: '/etc/timezone',
  runbookBaseUrl: undefined, // https base for runbook links (summary.links.runbooks)
  runbooks: [], // [{title, path}] relative to runbookBaseUrl
  logLevel: 'info',

  remoteEnabled: false, // ADR 0014: v2.0 is tailnet-only
  accessJwt: { teamDomain: '', audience: '' },

  webDir: '/opt/homelab-dashboard/web',
  stateDir: '/var/lib/homelab-dashboard',
  pushDir: '/srv/homelab/push',
  dataDir: '/srv/homelab/data',
  mediaMount: '/media',
  rootPath: '/',
  procDir: '/proc',
  helperSocket: '/run/homelab-dockerctl.sock',
  gateDir: '/var/lib/bootgate',
  gateRerunFile: '/run/bootgate/rerun',
  gateRetryTimer: 'bootgate-retry.timer',
  gateRetryService: undefined, // the timer's service: bootgate-retry.service
  gateRetryMinutes: 5,
  rebootRequiredFile: '/run/reboot-required',
  backupStatusFile: '/var/lib/homelab-backup/status.json',
  notifyHistoryFile: '/var/lib/homelab-notify/history.jsonl',
  rehearsalFlag: '/etc/homelab/rehearsal',

  apps: DEFAULT_APPS,
  pinnedImages: {}, // name -> image ref (from versions.yml); used while Docker is held
  integrations: DEFAULT_INTEGRATIONS,
  integrationTimeoutMs: 5000,
  pushEndpointHosts: undefined,

  // seconds
  intervals: { core: 10, media: 30, sampler: 60, split: 3600, boots: 600, lastPull: 600, reminders: 3600 },
});

// snake_case file key -> camelCase config key. Unknown keys are reported, not silently accepted.
export const CONFIG_KEYS = {
  listen_host: 'listenHost',
  listen_port: 'listenPort',
  display_name: 'displayName',
  timezone: 'timezone',
  timezone_file: 'timezoneFile',
  runbook_base_url: 'runbookBaseUrl',
  runbooks: 'runbooks',
  log_level: 'logLevel',
  remote_enabled: 'remoteEnabled',
  access_jwt: 'accessJwt',
  web_dir: 'webDir',
  state_dir: 'stateDir',
  push_dir: 'pushDir',
  data_dir: 'dataDir',
  media_mount: 'mediaMount',
  root_path: 'rootPath',
  proc_dir: 'procDir',
  helper_socket: 'helperSocket',
  gate_dir: 'gateDir',
  gate_rerun_file: 'gateRerunFile',
  gate_retry_timer: 'gateRetryTimer',
  gate_retry_service: 'gateRetryService',
  gate_retry_minutes: 'gateRetryMinutes',
  reboot_required_file: 'rebootRequiredFile',
  backup_status_file: 'backupStatusFile',
  notify_history_file: 'notifyHistoryFile',
  rehearsal_flag: 'rehearsalFlag',
  apps: 'apps',
  pinned_images: 'pinnedImages',
  integrations: 'integrations',
  integration_timeout_ms: 'integrationTimeoutMs',
  push_endpoint_hosts: 'pushEndpointHosts',
  intervals: 'intervals',
};

const camel = (s) => s.replace(/_([a-z])/g, (_, c) => c.toUpperCase());

const isAbsPath = (v) => typeof v === 'string' && v.startsWith('/') && !v.includes('\0') && v.length <= 4096;
const intIn = (lo, hi) => (v) => Number.isInteger(v) && v >= lo && v <= hi;
const str = (max = 256) => (v) => typeof v === 'string' && v.length > 0 && v.length <= max;
const PATH_KEYS = [
  'webDir', 'stateDir', 'pushDir', 'dataDir', 'mediaMount', 'rootPath', 'procDir', 'helperSocket', 'gateDir',
  'gateRerunFile', 'rebootRequiredFile', 'backupStatusFile', 'notifyHistoryFile', 'rehearsalFlag', 'timezoneFile',
];
// camelCase key -> [check, what it must be]
const CHECKS = {
  listenHost: [str(255), 'a host name or address'],
  listenPort: [intIn(0, 65535), 'a whole number 0-65535'],
  displayName: [str(64), 'a string of 1-64 characters'],
  timezone: [(v) => v === null || str(64)(v), 'an IANA zone name or null'],
  logLevel: [(v) => ['debug', 'info', 'warn', 'error'].includes(v), 'debug, info, warn or error'],
  remoteEnabled: [(v) => typeof v === 'boolean', 'true or false (a JSON boolean)'],
  gateRetryTimer: [(v) => typeof v === 'string' && /^[A-Za-z0-9@_.:-]{1,128}\.timer$/.test(v), 'a systemd .timer unit name'],
  gateRetryService: [(v) => typeof v === 'string' && /^[A-Za-z0-9@_.:-]{1,128}\.service$/.test(v), 'a systemd .service unit name'],
  gateRetryMinutes: [intIn(1, 1440), 'a whole number of minutes 1-1440'],
  integrationTimeoutMs: [intIn(100, 60_000), 'a whole number of milliseconds 100-60000'],
  pushEndpointHosts: [(v) => v === null || typeof v === 'string', 'a string'],
  runbookBaseUrl: [(v) => v === null || str(500)(v), 'an https:// URL'],
  runbooks: [Array.isArray, 'a list of {title, path}'],
  pinnedImages: [(v) => v && typeof v === 'object' && !Array.isArray(v) && Object.values(v).every((x) => typeof x === 'string'), 'a map of name -> image reference'],
  ...Object.fromEntries(PATH_KEYS.map((k) => [k, [isAbsPath, 'an absolute path']])),
};
// seconds; the minimums stop a typo from becoming a hot loop
export const INTERVAL_LIMITS = { core: [2, 3600], media: [10, 3600], sampler: [10, 3600], split: [60, 86_400], boots: [60, 86_400], lastPull: [60, 86_400], reminders: [60, 86_400] };

// Converts the field names of each map value ({jellyfin: {base_url}} -> {jellyfin: {baseUrl}}), not the map keys.
function camelValues(map) {
  const out = {};
  for (const [k, v] of Object.entries(map ?? {})) {
    if (v && typeof v === 'object' && !Array.isArray(v)) {
      out[k] = Object.fromEntries(Object.entries(v).map(([fk, fv]) => [camel(fk), fv]));
    } else out[k] = v;
  }
  return out;
}

const isObj = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);

export function buildConfig(file = {}, env = {}) {
  const unknown = [];
  const warnings = [];
  const bad = (key, why) => warnings.push(`${key}: must be ${why}; using the default`);
  const cfg = structuredClone({ ...DEFAULTS, apps: { ...DEFAULTS.apps }, integrations: { ...DEFAULTS.integrations } });
  for (const [key, value] of Object.entries(file)) {
    if (!Object.hasOwn(CONFIG_KEYS, key)) {
      unknown.push(key);
      continue;
    }
    const target = CONFIG_KEYS[key];
    if (target === 'apps') {
      // Full replacement: the rendered list is the set of watched apps.
      if (!isObj(value) || !Object.entries(value).every(([n, v]) => /^[a-z0-9][a-z0-9_.-]{0,63}$/.test(n) && isObj(v))) bad(key, 'a map of app name -> settings');
      else cfg.apps = camelValues(value);
    } else if (target === 'integrations') {
      if (!isObj(value)) bad(key, 'a map of app -> {base_url, key_file}');
      else {
        for (const [name, v] of Object.entries(camelValues(value))) {
          const okUrl = v?.baseUrl === undefined || (typeof v.baseUrl === 'string' && /^https?:\/\//.test(v.baseUrl));
          const okKey = v?.keyFile === undefined || (typeof v.keyFile === 'string' && !v.keyFile.includes('..'));
          if (!isObj(v) || !okUrl || !okKey) bad(`${key}.${name}`, 'an object with an http(s) base_url and a key_file');
          else cfg.integrations[name] = { ...cfg.integrations[name], ...v };
        }
      }
    } else if (target === 'accessJwt') {
      const v = isObj(value) ? Object.fromEntries(Object.entries(value).map(([k, x]) => [camel(k), x])) : null;
      if (!v || !Object.values(v).every((x) => typeof x === 'string')) bad(key, 'an object of strings {team_domain, audience}');
      else cfg.accessJwt = { ...cfg.accessJwt, ...v };
    } else if (target === 'intervals') {
      if (!isObj(value)) bad(key, 'a map of name -> seconds');
      else {
        for (const [k, v] of Object.entries(value)) {
          const name = camel(k);
          const lim = Object.hasOwn(INTERVAL_LIMITS, name) ? INTERVAL_LIMITS[name] : null;
          if (!lim) warnings.push(`intervals.${k}: unknown interval; ignored`);
          else if (!Number.isInteger(v) || v < lim[0] || v > lim[1]) bad(`intervals.${k}`, `a whole number of seconds ${lim[0]}-${lim[1]}`);
          else cfg.intervals[name] = v;
        }
      }
    } else if (CHECKS[target] && !CHECKS[target][0](value)) {
      bad(key, CHECKS[target][1]);
    } else {
      cfg[target] = value;
    }
  }
  if (env.HOST) cfg.listenHost = env.HOST;
  if (env.PORT !== undefined && env.PORT !== '') cfg.listenPort = Number.parseInt(env.PORT, 10);
  if (env.PUSH_DIR) cfg.pushDir = env.PUSH_DIR;
  if (env.LOG_LEVEL) cfg.logLevel = env.LOG_LEVEL;
  if (env.PUSH_ENDPOINT_HOSTS !== undefined) cfg.pushEndpointHosts = env.PUSH_ENDPOINT_HOSTS;
  for (const [name, [key, type]] of Object.entries(ENV_KEYS)) {
    const v = env[name];
    if (v === undefined || v === '') continue;
    if (type === 'bool') {
      if (!['true', 'false', '1', '0', 'yes', 'no'].includes(v.toLowerCase())) throw new Error(`${name} must be true or false`);
      cfg[key] = ['true', '1', 'yes'].includes(v.toLowerCase());
    } else if (type === 'int') {
      const n = Number.parseInt(v, 10);
      if (!Number.isInteger(n) || n <= 0) throw new Error(`${name} must be a positive whole number`);
      cfg[key] = n;
    } else cfg[key] = v;
  }
  if (!Number.isInteger(cfg.listenPort) || cfg.listenPort < 0 || cfg.listenPort > 65535) throw new Error('PORT must be 0-65535');
  cfg.gateRetryService ??= cfg.gateRetryTimer.replace(/\.timer$/, '.service');
  const rb = runbookLinks(cfg.runbookBaseUrl, cfg.runbooks);
  cfg.runbookLinks = rb.links;
  warnings.push(...rb.warnings);
  cfg.displayName ??= os.hostname().split('.')[0] || 'homelab';
  cfg.version = pkgVersion;
  return { config: cfg, unknown, warnings };
}

// Runbook links: an https base plus relative paths. A bad base or entry is dropped with a warning (logged at
// start), never fatal: a typo in a link must not keep the dashboard down on a bad night.
// relative, no "." or ".." segments, a Markdown file, optional #anchor
const SAFE_PATH = /^(?!\/)(?!.*(?:^|\/)\.{1,2}(?:\/|$|#))[A-Za-z0-9._~\-/]*\.md(?:#[A-Za-z0-9._~-]{1,64})?$/;

export function runbookLinks(baseUrl, entries) {
  const warnings = [];
  if (entries === undefined || entries === null || (Array.isArray(entries) && entries.length === 0)) return { links: [], warnings };
  if (!Array.isArray(entries)) return { links: [], warnings: ['runbooks must be a list; no runbook links shown'] };
  let base;
  try {
    base = new URL(String(baseUrl));
  } catch {
    return { links: [], warnings: ['runbook_base_url is missing or not a URL; no runbook links shown'] };
  }
  if (base.protocol !== 'https:' || base.username || base.password || base.search || base.hash) {
    return { links: [], warnings: ['runbook_base_url must be a plain https:// URL; no runbook links shown'] };
  }
  if (!base.pathname.endsWith('/')) base.pathname += '/';
  const links = [];
  entries.slice(0, 50).forEach((e, i) => {
    const title = typeof e?.title === 'string' ? e.title.replace(/[\u0000-\u001f\u007f]/g, ' ').trim() : '';
    const p = typeof e?.path === 'string' ? e.path : '';
    if (!title || title.length > 80) return warnings.push(`runbooks[${i}]: title must be 1-80 characters; skipped`);
    if (p.length > 200 || !SAFE_PATH.test(p)) return warnings.push(`runbooks[${i}]: path must be a relative .md path; skipped`);
    const url = new URL(p, base);
    if (url.origin !== base.origin || !url.pathname.startsWith(base.pathname)) {
      return warnings.push(`runbooks[${i}]: path leaves the base URL; skipped`);
    }
    links.push({ title, url: url.href });
  });
  if (entries.length > 50) warnings.push('only the first 50 runbooks are shown');
  return { links, warnings };
}

export function loadConfig(env = process.env, fs = nodeFs) {
  const file = env.DASHBOARD_CONFIG || null;
  let parsed = {};
  let missing = false;
  if (file) {
    try {
      parsed = JSON.parse(fs.readFileSync(file, 'utf8'));
    } catch (err) {
      if (err.code === 'ENOENT') missing = true;
      else throw new Error(`cannot load ${file}: ${err.message}`);
    }
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error(`${file} must hold a JSON object`);
  const { config, unknown, warnings } = buildConfig(parsed, env);
  return { config, unknown, warnings, file, missing };
}
