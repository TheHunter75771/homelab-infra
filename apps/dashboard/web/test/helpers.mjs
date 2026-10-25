// Small test shims: an in-memory CacheStorage (the Cache API) and a polling wait.

// In-memory CacheStorage. `fetcher` backs cache.addAll, which (as in browsers) is all-or-nothing: any
// failed or non-2xx response rejects and stores nothing.
export function createCacheStorage({ fetcher } = {}) {
  const stores = new Map();
  const keyOf = (req) => {
    const url = typeof req === 'string' ? req : req.url;
    try {
      return new URL(url, 'http://local').pathname + new URL(url, 'http://local').search;
    } catch {
      return String(url);
    }
  };
  function cache(name) {
    if (!stores.has(name)) stores.set(name, new Map());
    const map = stores.get(name);
    return {
      async put(req, res) {
        map.set(keyOf(req), { body: await res.clone().arrayBuffer(), status: res.status, headers: [...res.headers] });
      },
      async match(req) {
        const hit = map.get(keyOf(req));
        return hit ? new Response(hit.body, { status: hit.status, headers: hit.headers }) : undefined;
      },
      async addAll(requests) {
        const got = [];
        for (const r of requests) {
          const res = await fetcher(r);
          if (!res.ok) throw new TypeError(`addAll: ${typeof r === 'string' ? r : r.url} answered ${res.status}`);
          got.push([r, res]);
        }
        for (const [r, res] of got) await this.put(r, res);
      },
      async keys() {
        return [...map.keys()];
      },
      async delete(req) {
        return map.delete(keyOf(req));
      },
    };
  }
  return {
    stores,
    async open(name) {
      return cache(name);
    },
    async keys() {
      return [...stores.keys()];
    },
    async delete(name) {
      return stores.delete(name);
    },
    async has(name) {
      return stores.has(name);
    },
  };
}

export async function waitFor(fn, { timeout = 4000, interval = 10, what = 'condition' } = {}) {
  const end = Date.now() + timeout;
  let last;
  for (;;) {
    try {
      last = await fn();
      if (last) return last;
    } catch (err) {
      last = err;
    }
    if (Date.now() > end) throw new Error(`timed out waiting for ${what}${last instanceof Error ? `: ${last.message}` : ''}`);
    await new Promise((r) => setTimeout(r, interval));
  }
}

// CSP guard for shipped HTML (the server's CSP forbids inline code). Returns a list of problems, empty when
// the markup only loads external scripts. Deliberately tag-by-tag rather than matching <script>…</script>
// pairs, which misses `</script >`, `</script foo>` and scripts that are never closed:
// - every opening `<script` tag (up to its `>`, or the end of the text) must carry a src attribute;
// - an external script must have no inline content before its closing tag (`</script` + anything + `>`),
//   and must be closed at all (an unfinished opening tag counts as never closed);
// - no inline event-handler attributes (`on…=`) in any tag, no `javascript:` URLs, no <style> or style=.
export function inlineCodeProblems(html) {
  const problems = [];
  const text = String(html);
  const opening = /<script\b/gi;
  for (let m = opening.exec(text); m; m = opening.exec(text)) {
    const end = text.indexOf('>', m.index);
    const tag = end === -1 ? text.slice(m.index) : text.slice(m.index, end + 1);
    if (!/\ssrc\s*=/i.test(tag)) {
      problems.push(`inline script: ${tag.slice(0, 60)}`);
      continue;
    }
    const after = end === -1 ? text.length : end + 1; // an unfinished opening tag is never closed either
    const closing = /<\/script\b[^>]*>/gi;
    closing.lastIndex = after;
    const close = closing.exec(text);
    if (!close) problems.push(`script never closed: ${tag.slice(0, 60)}`);
    else if (text.slice(after, close.index).trim()) problems.push(`external script with inline content: ${tag.slice(0, 60)}`);
  }
  const tags = /<[a-z][^>]*(?:>|$)/gi;
  for (let m = tags.exec(text); m; m = tags.exec(text)) {
    if (/\son[a-z0-9_-]+\s*=/i.test(m[0])) problems.push(`inline event handler: ${m[0].slice(0, 60)}`);
    if (/\sstyle\s*=/i.test(m[0])) problems.push(`style attribute: ${m[0].slice(0, 60)}`);
  }
  if (/<style\b/i.test(text)) problems.push('<style> element');
  if (/javascript\s*:/i.test(text)) problems.push('javascript: URL');
  return problems;
}
