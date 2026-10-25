'use strict';

// wol-relay: sends Wake-on-LAN magic packets from the HOST network namespace.
// A container on a Docker bridge cannot broadcast onto the physical LAN, so this runs
// with network_mode: host. It is token-gated (X-Relay-Token, from WOL_RELAY_TOKEN) and
// the host firewall only admits its port on the tailnet interface.
//
// HTTP:  POST /wake  {"mac": "aa:bb:cc:dd:ee:ff", "broadcast"?: "x.x.x.255", "port"?: 9}
//        GET  /health
// Queue: optional. If WOL_QUEUE names a directory, *.json request files dropped there
//        (same body as /wake) are sent and deleted. Unset = no queue polling.
// Bind:  LISTEN_ADDR (e.g. the tailnet address) limits the listener to one address; unset = every
//        interface (the old behaviour). If that address isn't on the host yet (tailscaled still
//        starting at boot), the relay keeps retrying instead of exiting: no crash loop, and the
//        healthcheck fails until it binds. Any other bind error exits non-zero.
// No MAC or address is built in: callers supply them per request.

const http = require('http');
const dgram = require('dgram');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const PORT = parseInt(process.env.PORT, 10) || 9999;
const TOKEN = process.env.WOL_RELAY_TOKEN || '';
const QUEUE = process.env.WOL_QUEUE || '';
const LISTEN_ADDR = process.env.LISTEN_ADDR || '';
const BIND_RETRY_MS = 2000;
const MAX_BODY = 1024;

function tokenOk(given) {
  if (!TOKEN || typeof given !== 'string') return false;
  const a = Buffer.from(given);
  const b = Buffer.from(TOKEN);
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}

function sendMagic(mac, broadcast = '255.255.255.255', port = 9) {
  return new Promise((resolve, reject) => {
    const hex = String(mac || '').replace(/[^0-9a-fA-F]/g, '');
    if (hex.length !== 12) return reject(new Error('bad_mac'));
    const macBuf = Buffer.from(hex, 'hex');
    const magic = Buffer.alloc(102, 0xff);
    for (let i = 0; i < 16; i++) macBuf.copy(magic, 6 + i * 6);
    const s = dgram.createSocket('udp4');
    s.once('error', (e) => { s.close(); reject(e); });
    s.bind(() => s.setBroadcast(true));
    s.send(magic, 0, magic.length, port, broadcast, (err) => { s.close(); err ? reject(err) : resolve(); });
  });
}

const server = http.createServer((req, res) => {
  if (req.method === 'GET' && req.url === '/health') { res.writeHead(200); return res.end('ok'); }
  if (req.method !== 'POST' || req.url !== '/wake') { res.writeHead(404); return res.end('not found'); }
  if (!tokenOk(req.headers['x-relay-token'])) { res.writeHead(401); return res.end('unauthorized'); }
  let body = '';
  let tooBig = false;
  req.on('data', (c) => {
    body += c;
    if (body.length > MAX_BODY) { tooBig = true; req.destroy(); }
  });
  req.on('end', async () => {
    if (tooBig) return;
    let p = {}; try { p = JSON.parse(body || '{}'); } catch {}
    try {
      await sendMagic(p.mac, p.broadcast, p.port);
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ sent: true, mac: p.mac, broadcast: p.broadcast || '255.255.255.255' }));
    } catch (e) {
      res.writeHead(500, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ error: e.message }));
    }
  });
});
let lastBindLog = 0;
function listen() { server.listen(PORT, LISTEN_ADDR || undefined); }
server.on('listening', () => console.log(`[wol-relay] listening on ${LISTEN_ADDR || '*'}:${PORT} (host network)`));
server.on('error', (e) => {
  if (e.code === 'EADDRNOTAVAIL' && LISTEN_ADDR) {
    const now = Date.now();
    if (now - lastBindLog >= 30000) {
      console.error(`[wol-relay] ${LISTEN_ADDR} is not on this host yet (${e.code}); retrying every ${BIND_RETRY_MS / 1000}s`);
      lastBindLog = now;
    }
    setTimeout(() => { server.close(); listen(); }, BIND_RETRY_MS);
    return;
  }
  console.error(`[wol-relay] cannot listen on ${LISTEN_ADDR || '*'}:${PORT}: ${e.code || e.message}`);
  process.exit(1);
});
listen();

async function pollQueue() {
  let files = [];
  try { files = fs.readdirSync(QUEUE).filter((f) => f.endsWith('.json')); } catch { return; }
  for (const f of files) {
    const fp = path.join(QUEUE, f);
    let req;
    try { req = JSON.parse(fs.readFileSync(fp, 'utf8')); } catch { try { fs.unlinkSync(fp); } catch {} continue; }
    try { await sendMagic(req.mac, req.broadcast, req.port); console.log(`[wol-relay] queued wake sent: ${req.mac} bcast ${req.broadcast || '255.255.255.255'}`); }
    catch (e) { console.error('[wol-relay] queue send failed:', e.message); }
    try { fs.unlinkSync(fp); } catch {}
  }
}

if (QUEUE) {
  try { fs.mkdirSync(QUEUE, { recursive: true }); } catch {}
  setInterval(pollQueue, 1000);
  console.log(`[wol-relay] polling queue ${QUEUE} every 1s`);
}
