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
