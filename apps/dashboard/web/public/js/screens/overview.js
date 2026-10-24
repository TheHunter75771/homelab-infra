import { html, Icon, Chip, Bar, hrefFor } from '../ui.js';
import {
  healthBanner,
  appsSummary,
  sessionView,
  mediaHealthRows,
  mediaHealthSummary,
  lastDays,
  dayTone,
  alertChip,
  pick,
} from '../model.js';
import {
  fmtBytes,
  fmtHorizon,
  fmtLoad,
  fmtMem,
  fmtPct,
  fmtUptime,
  fmtAgo,
  fmtStamp,
  fmtUptimePct,
  fmtRate,
  fmtDayTime,
  fmtDay,
  plural,
} from '../format.js';

export function HealthBanner({ view }) {
  const b = healthBanner(view);
  return html`<section class=${`banner tone-${b.tone}`} aria-labelledby="health-title">
    <div class="banner-head">
      <div class="banner-ring"><${Icon} name=${b.icon} /></div>
      <div class="banner-text"><h2 id="health-title">${b.title}</h2><p>${b.sub}</p></div>
    </div>
    ${b.issues.map((it) => {
      const icon = html`<${Icon} name=${it.level === 'fail' ? 'x-circle' : 'warning'} cls=${`tc tone-${it.level}`} label=${it.level === 'fail' ? 'Failure:' : 'Warning:'} />`;
      return it.to
        ? html`<a class="issue" href=${hrefFor(it.to)}>${icon}<span>${it.text}</span><${Icon} name="caret-right" cls="chev" /></a>`
        : html`<div class="issue static">${icon}<span>${it.text}</span></div>`;
    })}
  </section>`;
}

export function GateRow({ view }) {
  const g = view.gate;
  return html`<a class="gate-row press" href="#/drive">
    <span class="label">Boot gate</span>
    <${Chip} tone=${g.chip.tone} icon=${g.chip.icon} label=${g.status} mono />
    <span class="gate-line">${g.line}</span>
    <${Icon} name="caret-right" cls="chev" />
  </a>`;
}

export function driveCaption(view) {
  const d = view.drive;
  if (!(d.total > 0)) return 'No drive data yet';
  if (d.stale) return 'Last known · drive offline';
  if (d.pct !== null && d.pct < 1) return 'No media yet';
  const f = d.forecast;
  if (!f) return 'Forecast after 7 days of samples';
  if (!(f.perDayBytes > 0) || !f.fullAt) return 'Not filling up lately';
  const days = (f.fullAt.getTime() - view.now) / 86400000;
  return `Full in ${fmtHorizon(days)}`;
}

function DriveTile({ view }) {
  const d = view.drive;
  const known = d.total > 0;
  const warn = d.pct !== null && d.pct >= view.thresholds.diskWarnPct;
  return html`<a class="tile press" href="#/drive">
    <span class="label"><${Icon} name="hard-drive" />Media drive</span>
    <span class="figure">${known ? `${fmtBytes(d.free)} free${d.stale ? '*' : ''}` : '—'}</span>
    <${Bar} pct=${d.pct ?? 0} tone=${warn ? 'warn' : null} />
    <span class="sub">${driveCaption(view)}</span>
  </a>`;
}

function HostTile({ view }) {
  const h = view.host;
  const rows = [
    ['Load', fmtLoad(h.load)],
    ['RAM', fmtMem(h.memUsed, h.memTotal)],
    ['Root disk', fmtPct(h.rootPct)],
    ['Uptime', fmtUptime(h.uptimeSeconds)],
  ];
  return html`<div class="tile dense">
    <span class="label"><${Icon} name="cpu" />Host</span>
    ${rows.map(([k, v]) => html`<span class="kv"><span>${k}</span><span>${v}</span></span>`)}
    ${h.rebootRequired && html`<span class="chip-reboot"><${Chip} tone="warn" icon="arrows-clockwise" label="Reboot needed" /></span>`}
  </div>`;
}

export function backupLines(view) {
  const b = view.backups;
  const now = view.now;
  const pull = b.lastPull ? `Pulled to PC · ${fmtStamp(b.lastPull, now)}` : 'Not pulled to the PC yet';
  const sized = (d) => `${fmtStamp(d, now)}${Number.isFinite(b.size) ? ` · ${fmtBytes(b.size)}` : ''}`;
  switch (b.status) {
    case 'ok':
      return { tone: 'ok', icon: 'check-circle', head: `Nightly OK · ${fmtAgo(b.lastOk, now)}`, sub: sized(b.lastOk), pull };
    case 'stale':
      return { tone: 'warn', icon: 'warning', head: `Nightly late · ${fmtAgo(b.lastOk, now)}`, sub: sized(b.lastOk), pull };
    case 'failed':
      return { tone: 'fail', icon: 'x-circle', head: 'Last run failed', sub: b.lastOk ? `Last good: ${fmtStamp(b.lastOk, now)}` : 'No good backup yet', pull };
    case 'unknown':
      return { tone: 'warn', icon: 'question', head: "Can't read the backup status", sub: 'status.json is missing or broken', pull };
    default:
      return { tone: 'idle', icon: 'clock-counter-clockwise', head: 'No backup yet', sub: 'First nightly run at 03:30', pull };
  }
}

function BackupsTile({ view }) {
  const l = backupLines(view);
  return html`<div class="tile dense">
    <span class="label"><${Icon} name="archive" />Backups</span>
    <span class="status-line"><${Icon} name=${l.icon} cls=${`tc tone-${l.tone}`} />${l.head}</span>
    <span class="sub">${l.sub}</span>
    <span class="pull"><${Icon} name="desktop" />${l.pull}</span>
  </div>`;
}

export function boxDays(view, n = 30) {
  const box = view.uptime?.box || {};
  const byDay = new Map((Array.isArray(box.days) ? box.days : []).map((d) => [String(pick(d, 'date', 'day')), Number(pick(d, 'pct', 'upPct', 'up_pct'))]));
  return lastDays(n, view.now).map((k) => {
    const pct = byDay.has(k) ? byDay.get(k) : NaN;
    return { day: k, pct, tone: dayTone(pct) };
  });
}

// "Oct 20: 99.72% up" / "Oct 20: no data": a day bar's text equivalent.
export function dayText(d) {
  return Number.isFinite(d.pct) ? `${fmtDay(d.day)}: ${fmtUptimePct(d.pct)} up` : `${fmtDay(d.day)}: no data`;
}

export function boxPct(view) {
  const box = view.uptime?.box || {};
  const given = Number(pick(box, 'pct30d', 'pct30'));
  if (Number.isFinite(given)) return given;
  const days = boxDays(view).filter((d) => Number.isFinite(d.pct));
  if (!days.length) return NaN;
  return days.reduce((a, d) => a + d.pct, 0) / days.length;
}

function UptimeTile({ view }) {
  const days = boxDays(view);
  const bad = days.filter((d) => d.tone === 'warn' || d.tone === 'fail').length;
  return html`<a class="tile press" href="#/uptime">
    <span class="label"><${Icon} name="pulse" />Box uptime</span>
    <span class="figure">${fmtUptimePct(boxPct(view))}</span>
    <span class="daybars" aria-hidden="true">${days.map((d) => html`<span class=${d.tone === 'none' ? '' : `tone-${d.tone}`} title=${dayText(d)}></span>`)}</span>
    <span class="sub">Last 30 days<span class="sr-only">, ${bad ? `${bad} with downtime` : 'no downtime'}</span></span>
  </a>`;
}

function AppsTile({ view }) {
  const s = appsSummary(view.apps, view.docker);
  return html`<a class="tile press" href="#/apps">
    <span class="label"><${Icon} name="squares-four" />Apps</span>
    <span class="figure">${s.big}</span>
    <span class="segbar" aria-hidden="true">${view.apps.map((a) => html`<span class=${`tone-${a.st.tone}`}></span>`)}</span>
    <span class="sub">${s.sub}</span>
  </a>`;
}

// Text for a media tile that isn't 'ok': the server's message, or a fallback.
export function tileMessage(name, tile) {
  if (tile.message) return tile.message;
  const label = { jellyfin: 'Jellyfin', sabnzbd: 'SABnzbd', jellyseerr: 'Jellyseerr', prowlarr: 'Prowlarr' }[name] || name;
  switch (tile.state) {
    case 'held':
      return `${label} is held by the gate.`;
    case 'stopped':
      return `${label} is stopped.`;
    case 'down':
      return `${label} isn't running.`;
    case 'unknown':
      return `${label}'s state is unknown.`;
    case 'unconfigured':
      return `Add the ${label} API key in Settings.`;
    case 'error':
      return `${label} isn't answering`;
    case 'pending':
      return 'Checking…';
    default:
      return '';
  }
}

export function playingEmptyText(view) {
  const t = view.media.tiles.jellyfin;
  return t.state === 'ok' ? 'Nothing playing right now.' : tileMessage('jellyfin', t);
}

export function downloadsSummary(view) {
  const t = view.media.tiles.sabnzbd;
  if (t.state === 'error') return 'not answering';
  if (t.state !== 'ok') return { held: 'held', stopped: 'stopped', down: 'down', unknown: 'unknown', unconfigured: 'no API key', pending: '…' }[t.state] || '—';
  const n = Number.isFinite(Number(t.total)) && Number(t.total) > 0 ? Number(t.total) : (Array.isArray(t.items) ? t.items.length : 0);
  if (t.paused) return n ? `${plural(n, 'item')} · paused` : 'paused';
  if (!n) return 'empty';
  return `${plural(n, 'item')} · ${fmtRate(pick(t, 'speedBytesPerSec', 'speedBps'))}`;
}

export function requestsCount(view) {
  const t = view.media.tiles.jellyseerr;
  if (t.state !== 'ok') return t.state === 'held' ? 'held' : '—';
  const n = Number(t.pending);
  return String(Number.isFinite(n) ? n : Array.isArray(t.requests) ? t.requests.length : 0);
}

function DesktopRow({ view }) {
  const alerts = view.alerts.slice(0, 4);
  const jf = view.media.tiles.jellyfin;
  const sessions = jf.state === 'ok' && Array.isArray(jf.sessions) ? jf.sessions.map(sessionView) : [];
  const mh = mediaHealthSummary(mediaHealthRows(view.media));
  return html`<div class="ov-row desktop-only">
    <section class="ov-card" aria-label="Apps">
      <a class="card-link" href="#/apps"><span>Apps</span><span class="more">All apps</span><${Icon} name="caret-right" /></a>
      ${view.apps.map(
        (a) => html`<a class="app-line" href=${hrefFor({ name: 'app', id: a.name })}>
          <${Icon} name=${a.st.icon} spin=${a.st.spin} cls=${`tc tone-${a.st.tone}`} />
          <span class="name">${a.label}</span>
          <span class=${`word tc tone-${a.st.tone}`}>${a.st.label}</span>
          <span class="up">${a.uptimeSeconds !== null ? fmtUptime(a.uptimeSeconds) : '—'}</span>
        </a>`,
      )}
      ${!view.apps.length && html`<span class="empty alert-line">No containers reported.</span>`}
    </section>
    <section class="ov-card" aria-label="Recent alerts">
      <a class="card-link" href="#/alerts"><span>Recent alerts</span><span class="more">All alerts</span><${Icon} name="caret-right" /></a>
      ${alerts.map((al) => {
        const c = alertChip(al);
        return html`<div class="alert-line">
          <${Icon} name=${c.icon} cls=${`tc tone-${c.tone}`} label=${c.label} />
          <div class="body">
            <div class="head"><b>${al.title}</b><span class="ts">${fmtDayTime(al.ts)}</span></div>
            <span class="text">${al.body}</span>
          </div>
        </div>`;
      })}
      ${!alerts.length && html`<span class="empty alert-line">No alerts yet.</span>`}
    </section>
    <section class="ov-card media" aria-label="Media">
      <a class="card-link" href="#/media"><span>Media</span><span class="more">Details</span><${Icon} name="caret-right" /></a>
      ${sessions.slice(0, 2).map(
        (np) => html`<div class="np">
          <span class="np-head"><${Icon} name="play-circle-fill" /><b>${np.title}</b><span>${np.user}</span></span>
          <${Bar} pct=${np.pct} cls="hair" />
        </div>`,
      )}
      ${!sessions.length && html`<span class="empty">${playingEmptyText(view)}</span>`}
      <div class="ov-media-facts">
        <div><span class="k">Downloads</span><span class="v">${downloadsSummary(view)}</span></div>
        <div><span class="k">Requests pending</span><span class="v">${requestsCount(view)}</span></div>
        <div><span class="k">Media health</span><span class=${`v word tone-${mh.tone}`}>${mh.label}</span></div>
      </div>
    </section>
  </div>`;
}

function NowPlayingStrip({ view }) {
  const jf = view.media.tiles.jellyfin;
  if (jf.state !== 'ok' || !Array.isArray(jf.sessions)) return null;
  const s = jf.sessions.map(sessionView)[0];
  if (!s) return null;
  return html`<a class="np-strip press mobile-only" href="#/media">
    <${Icon} name="play-circle-fill" />
    <span class="body">
      <span class="head"><b>${s.title}</b><span>${s.user}</span></span>
      <${Bar} pct=${s.pct} cls="hair" />
    </span>
  </a>`;
}

export function Overview({ view }) {
  return html`<${HealthBanner} view=${view} />
    <${GateRow} view=${view} />
    <div class="tiles">
      <${DriveTile} view=${view} />
      <${HostTile} view=${view} />
      <${BackupsTile} view=${view} />
      <${UptimeTile} view=${view} />
      <${AppsTile} view=${view} />
    </div>
    <${DesktopRow} view=${view} />
    <${NowPlayingStrip} view=${view} />`;
}

export function LoadingSkeleton() {
  return html`<div class="stack" aria-busy="true" aria-label="Loading">
    <div class="skel banner-sk"></div>
    <div class="skel gate-sk"></div>
    <div class="tiles">${[1, 2, 3, 4, 5].map(() => html`<div class="skel tile-sk"><i></i><i></i><i></i><i></i></div>`)}</div>
  </div>`;
}

