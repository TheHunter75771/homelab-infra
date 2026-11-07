"""Minimal Ollama client: list loaded models and unload them. Never raises."""

import json
import logging
import urllib.error
import urllib.request

log = logging.getLogger("gpu_gate.ollama")


class OllamaClient:
    def __init__(self, url, timeout=5.0):
        self.url = url.rstrip("/")
        self.timeout = timeout

    def _call(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.url + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            e.close()  # the error carries an open response
            raise
        return json.loads(raw) if raw.strip() else {}

    def loaded_models(self):
        ps = self._call("GET", "/api/ps")
        names = []
        for m in ps.get("models") or []:
            name = m.get("model") or m.get("name")
            if name:
                names.append(name)
        return names

    def unload_all(self):
        """Unload every loaded model. Returns the names unloaded; errors are logged only."""
        try:
            models = self.loaded_models()
        except Exception as e:  # noqa: BLE001 - never fatal (contract 12.4)
            log.warning("unload: GET /api/ps failed: %s", e)
            return []
        done = []
        for m in models:
            try:
                self._call("POST", "/api/generate", {"model": m, "keep_alive": 0})
                done.append(m)
                log.info("unload: %s unloaded", m)
            except Exception as e:  # noqa: BLE001
                log.warning("unload: %s failed: %s", m, e)
        if not models:
            log.info("unload: no models loaded")
        return done
