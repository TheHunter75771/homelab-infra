// A minimal browser for the smoke tests: linkedom's DOM + the globals the app touches, with fetch pointed at
// a real HTTP server (the fake API serving public/). Collects console.error and uncaught errors.

import { parseHTML } from 'linkedom';
import { createCacheStorage, waitFor } from './helpers.mjs';

const realFetch = globalThis.fetch;
const KEYS = ['window', 'document', 'location', 'matchMedia', 'localStorage', 'requestAnimationFrame', 'cancelAnimationFrame', 'caches', 'fetch', 'navigator', 'HTMLElement'];

function mediaMatches(query, width) {
  return query
    .split(/\s+and\s+/)
    .every((part) => {
      const m = /\((min|max)-width:\s*(\d+)px\)/.exec(part);
      if (m) return m[1] === 'min' ? width >= Number(m[2]) : width <= Number(m[2]);
      if (/display-mode:\s*standalone/.test(part)) return false;
      if (/pointer:\s*coarse/.test(part)) return false;
      if (/prefers-reduced-motion/.test(part)) return false;
      return true;
    });
}

// Opens the dashboard: fetches index.html from `base`, builds the DOM, boots the real app modules.
export async function openApp({ base, hash = '#/', width = 390, caches = createCacheStorage() } = {}) {
  const html = await (await realFetch(`${base}/`)).text();
  const { window, document } = parseHTML(html);
  const saved = Object.fromEntries(KEYS.map((k) => [k, Object.getOwnPropertyDescriptor(globalThis, k)]));
  const errors = [];
  const origError = console.error;
  console.error = (...args) => errors.push(args.map(String).join(' '));
  const onErr = (err) => errors.push(`uncaught: ${err?.stack || err}`);
  process.on('uncaughtException', onErr);
  process.on('unhandledRejection', onErr);

  let currentHash = hash;
  const location = {
    protocol: 'http:',
    hostname: '127.0.0.1',
    get hash() {
      return currentHash;
    },
    set hash(v) {
      currentHash = v.startsWith('#') ? v : `#${v}`;
      window.dispatchEvent(new window.Event('hashchange'));
    },
    get href() {
      return `${base}/${currentHash}`;
    },
  };
  const store = new Map();
  const define = (k, value) => Object.defineProperty(globalThis, k, { value, configurable: true, writable: true, enumerable: true });
  define('window', window);
  define('document', document);
  define('location', location);
  define('matchMedia', (q) => ({ matches: mediaMatches(q, width), addEventListener() {}, removeEventListener() {} }));
  define('localStorage', { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, String(v)), removeItem: (k) => store.delete(k) });
  define('requestAnimationFrame', (cb) => setTimeout(() => cb(Date.now()), 0));
  define('cancelAnimationFrame', (t) => clearTimeout(t));
  define('caches', caches);
  define('fetch', (url, opts) => realFetch(new URL(url, base), opts));
  define('navigator', { userAgent: 'Mozilla/5.0 (X11; Linux x86_64) Chrome/130', platform: 'Linux x86_64', maxTouchPoints: 0 });
  window.matchMedia = globalThis.matchMedia;
  window.scrollTo = () => {};

  const { boot } = await import('../public/js/boot.js');
  const app = boot({ root: document.getElementById('app'), registerWorker: false });

  const text = (sel = 'body') => document.querySelector(sel)?.textContent?.replace(/\s+/g, ' ').trim() ?? '';
  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => [...document.querySelectorAll(sel)];
  const click = (el) => {
    if (!el) throw new Error('click: no element');
    el.dispatchEvent(new window.Event('click', { bubbles: true, cancelable: true }));
  };
  const button = (label, root = document) => [...root.querySelectorAll('button, a')].find((b) => b.textContent.replace(/\s+/g, ' ').trim() === label);
  const type = (el, value) => {
    el.value = value;
    el.dispatchEvent(new window.Event('input', { bubbles: true }));
  };
  const change = (el, value) => {
    el.value = value;
    el.dispatchEvent(new window.Event('change', { bubbles: true }));
  };
  const until = (fn, what) => waitFor(fn, { what, timeout: 5000 });
  const go = async (h) => {
    location.hash = h;
    await new Promise((r) => setTimeout(r, 0));
  };

  async function close() {
    app.stop();
    await new Promise((r) => setTimeout(r, 5));
    console.error = origError;
    process.off('uncaughtException', onErr);
    process.off('unhandledRejection', onErr);
    for (const [k, d] of Object.entries(saved)) {
      if (d) Object.defineProperty(globalThis, k, d);
      else delete globalThis[k];
    }
  }

  return { app, window, document, errors, text, $, $$, click, button, type, change, until, go, close, caches, location };
}
