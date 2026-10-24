import { html, Icon, Btn } from '../ui.js';
import { useState, useEffect, useRef, useLayoutEffect, useMemo } from '../../vendor/hooks.mjs';
import { ActionRow, StatusChip } from './apps.js';
import { fmtUptime, fmtDayTime, shortImage } from '../format.js';
import { streamSSE, appPath } from '../api.js';
import { parseLogEvent, filterLines, lineText, MAX_LINES } from '../logs.js';

const RENDER_MAX = 1000;

function healthText(app) {
  if (app.statusKey === 'held' || app.statusKey === 'stopped' || app.state !== 'running') return '—';
  const every = app.healthInterval ? ` · ${app.healthInterval}` : '';
  switch (app.health) {
    case 'healthy':
      return `passing${every}`;
    case 'unhealthy':
      return `failing${every}`;
    case 'starting':
      return 'starting';
    default:
      return 'none';
  }
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    try {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.setAttribute('readonly', '');
      ta.className = 'sr-only';
      document.body.append(ta);
      ta.select();
      const ok = document.execCommand('copy');
      ta.remove();
      return ok;
    } catch {
      return false;
    }
  }
}

export function LogViewer({ app, lock, actions }) {
  const [lines, setLines] = useState([]);
  const [follow, setFollow] = useState(true);
  const [frozen, setFrozen] = useState(null);
  const [q, setQ] = useState('');
  const [status, setStatus] = useState('connecting');
  const [problem, setProblem] = useState(null);
  const [epoch, setEpoch] = useState(0);
  const bodyRef = useRef(null);

  useEffect(() => {
    setLines([]);
    setProblem(null);
    if (lock) {
      setStatus('locked');
      return undefined;
    }
    const ctl = new AbortController();
    let buf = [];
    let flushTimer = null;
    let end = null;
    const flush = () => {
      flushTimer = null;
      const add = buf;
      buf = [];
      if (!add.length) return;
      setLines((prev) => {
        const next = prev.concat(add);
        return next.length > MAX_LINES ? next.slice(-MAX_LINES) : next;
      });
    };
    setStatus('connecting');
    streamSSE(`${appPath(app.name, 'logs')}?follow=1&tail=500`, {
      signal: ctl.signal,
      onOpen: () => setStatus('live'),
      onEvent(ev) {
        const r = parseLogEvent(ev);
        if (r.end) end = r.end;
        if (!r.lines.length) return;
        buf.push(...r.lines);
        if (!flushTimer) flushTimer = setTimeout(flush, 100);
      },
    })
      .then(() => {
        if (ctl.signal.aborted) return;
        flush();
        if (end?.reason === 'error') {
          setProblem(end.message);
          setStatus('error');
        } else setStatus(end?.reason === 'max-duration' ? 'timeout' : 'ended');
      })
      .catch((err) => {
        if (ctl.signal.aborted) return;
        flush();
        setProblem(err?.status ? err.message : null);
        setStatus('error');
      });
    return () => {
      ctl.abort();
      if (flushTimer) clearTimeout(flushTimer);
    };
  }, [app.name, lock, epoch]);

  const source = follow ? lines : frozen || lines;
  const matching = useMemo(() => filterLines(source, q), [source, q]);
  const shown = matching.length > RENDER_MAX ? matching.slice(-RENDER_MAX) : matching;
  const newSincePause = !follow && frozen ? Math.max(0, lines.length - frozen.length) : 0;

  // Keep the newest line in view while following. Fonts and wrapping can grow the content after this
  // render, so pin again on the next frame and once the web fonts are in.
  useLayoutEffect(() => {
    const el = bodyRef.current;
    if (!follow || !el) return undefined;
    const pin = () => {
      el.scrollTop = el.scrollHeight;
    };
    pin();
    const raf = globalThis.requestAnimationFrame?.(pin);
    globalThis.document?.fonts?.ready?.then(pin).catch(() => {});
    return () => raf && globalThis.cancelAnimationFrame?.(raf);
  });

  const pause = () => {
    setFrozen(lines);
    setFollow(false);
  };
  const resume = () => {
    setFrozen(null);
    setFollow(true);
    if (status === 'ended' || status === 'error' || status === 'timeout') setEpoch((e) => e + 1);
  };
  const onScroll = (e) => {
    const el = e.currentTarget;
    if (follow && el.scrollHeight - el.scrollTop - el.clientHeight > 48) pause();
  };
  const copy = async () => {
    const ok = await copyText(matching.map(lineText).join('\n'));
    actions.toast(ok ? 'ok' : 'fail', ok ? `Copied ${matching.length} lines` : "Couldn't copy to the clipboard", ok ? 'copy' : undefined);
  };

  const right = !follow
    ? `Paused${newSincePause ? ` · ${newSincePause} new` : ''}`
    : {
        connecting: 'Connecting…',
        live: 'Following live',
        ended: 'Stream ended',
        timeout: 'Stopped following after 30 min · press Follow',
        error: "Can't reach the log stream",
        locked: 'Not available',
      }[status];
  let emptyText = null;
  if (status === 'locked') emptyText = `Logs aren't available: ${lock.replace(/\.$/, '').replace(/^./, (c) => c.toLowerCase())}.`;
  else if (!source.length) {
    if (status === 'error') emptyText = `${problem || "Can't reach the log stream."} Press Follow to retry.`;
    else emptyText = status === 'connecting' ? 'Loading lines…' : 'No log lines yet.';
  } else if (status === 'ended' && follow) emptyText = null;
  else if (!matching.length) emptyText = `No lines match “${q.trim()}”.`;

  return html`<section class="card logs" aria-label="Logs">
    <div class="log-tools">
      <span class="label">Logs</span>
      ${follow && !['ended', 'timeout', 'error'].includes(status)
        ? html`<${Btn} icon="pause" label="Pause" primary onClick=${pause} />`
        : html`<${Btn} icon="play" label="Follow" onClick=${resume} reason=${status === 'locked' ? lock : null} />`}
      <${Btn} icon="copy" label="Copy" onClick=${copy} reason=${matching.length ? null : 'Nothing to copy'} />
      <input
        class="input"
        type="search"
        value=${q}
        onInput=${(e) => setQ(e.currentTarget.value)}
        placeholder="Filter lines (e.g. ERR, health)"
        aria-label="Filter log lines"
        autocomplete="off"
        autocapitalize="off"
        spellcheck=${false}
      />
    </div>
    <div class="log-body" ref=${bodyRef} onScroll=${onScroll} role="log" aria-live="off" tabindex="0">
      ${shown.map(
        (l) => html`<div class="log-line" key=${l.key}>
          <span class="t">${l.t}</span><span class=${`lv lv-${l.lv}`}>${l.lv}</span><span class="m">${l.m}</span>
        </div>`,
      )}
      ${emptyText && html`<span class="muted">${emptyText}</span>`}
    </div>
    <div class="log-foot"><span>${matching.length} lines${q.trim() ? ' match' : ''}</span><span>${right}</span></div>
  </section>`;
}

export function AppDetail({ view, route, ctx, actions }) {
  const app = view.apps.find((a) => a.name === route.id);
  if (!app) {
    return html`<div class="card">
      <span class="empty">There's no watched container called “${route.id}”.</span>
      <a class="link-row" href="#/apps"><${Icon} name="caret-left" /><span>All apps</span></a>
    </div>`;
  }
  const facts = [
    ['Status', app.st.long || app.st.label],
    ['Uptime', app.uptimeSeconds !== null ? fmtUptime(app.uptimeSeconds) : '—'],
    ['Restarts', app.restartCount === null ? '—' : String(app.restartCount)],
    ['Health check', healthText(app)],
    ['Image', shortImage(app.image || app.pinned)],
    ['Started', app.startedAt && app.statusKey !== 'held' ? fmtDayTime(app.startedAt) : '—'],
  ];
  // The server's lock (Docker held/down); without one, Docker not active means no logs.
  const logLock = app.locks?.logs ?? (view.docker !== 'active' ? (view.gate.status === 'failed' ? 'Held by the boot gate until the media drive is back.' : "Docker isn't running.") : null);
  return html`<div class="grid grid-380">
    <section class="card" aria-label=${`${app.label} details`}>
      <div class="app-desc-row"><span>${app.desc}</span><${StatusChip} app=${app} /></div>
      <div class="facts two">${facts.map(([k, v]) => html`<div class="fact"><span>${k}</span><span>${v}</span></div>`)}</div>
      ${app.next &&
      html`<span class="app-next"><${Icon} name="arrow-circle-up" />Pinned version ${app.next} is newer than the running image</span>`}
      <${ActionRow} app=${app} ctx=${ctx} actions=${actions} />
    </section>
    <${LogViewer} key=${app.name} app=${app} lock=${logLock} actions=${actions} />
  </div>`;
}
