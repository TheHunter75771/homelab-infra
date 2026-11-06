"""Open WebUI effective-settings check for role ai_stack (docs/CONTRACTS.md §12.18b extraction, §12.22
search). Runs INSIDE the open-webui container, fed on stdin: `docker exec -i open-webui python3 - '<expected
JSON>'`. Only the keys in the expected JSON are read and compared.

Open WebUI 0.12.4 keeps its settings in the `config` table, one row per key (open_webui/models/config.py):
on every start it inserts the environment's value only for keys that have no row yet, and every request
reads the row (no cache). So the row is the effective value, and the environment only counts where there is
no row, or when ENABLE_PERSISTENT_CONFIG=false. This reads the database READ-ONLY (sqlite mode=ro, plus
PRAGMA query_only) and compares the way Open WebUI itself uses each value; it never writes.

Prints one JSON object: {"ok": bool, "source": ..., "settings": {key: {"effective", "expected", "ok", "from"}}}.
Exit 0: all match. 1: a mismatch (the admin fixes it in Admin Panel > Settings > Documents).
Exit 2 ("source": "unsupported"): can't tell (no sqlite database, locked, no config table, unreadable value).
"""

import json
import os
import sqlite3
import sys

# Seconds to wait for a lock held by Open WebUI before giving up (exit 2). Short on purpose: a busy
# database means "can't tell", not a hung playbook.
DB_TIMEOUT = 5

ENV_NAMES = {
    "rag.content_extraction_engine": "CONTENT_EXTRACTION_ENGINE",
    "rag.tika_server_url": "TIKA_SERVER_URL",
    "rag.tika_server_version": "TIKA_SERVER_VERSION",
    "rag.file.max_size": "RAG_FILE_MAX_SIZE",
    "rag.content_extraction.supported_media_mime_types": "CONTENT_EXTRACTION_SUPPORTED_MEDIA_MIME_TYPES",
}

# §12.22 search keys: where there is no row, Open WebUI 0.12.4's config.py value, i.e. the env variable
# converted the way config.py does, or config.py's default when it is unset.
SEARCH_ENV = {
    "rag.embedding_engine": ("RAG_EMBEDDING_ENGINE", "", str),
    "rag.embedding_model": ("RAG_EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2", str),
    "rag.ollama.base_url": ("RAG_OLLAMA_BASE_URL", None, str),
    "rag.top_k": ("RAG_TOP_K", "3", int),
    "rag.top_k_reranker": ("RAG_TOP_K_RERANKER", "3", int),
    "rag.enable_hybrid_search": ("ENABLE_RAG_HYBRID_SEARCH", "", lambda v: v.lower() == "true"),
    "rag.embedding_batch_size": ("RAG_EMBEDDING_BATCH_SIZE", "1", int),
    "rag.embedding_concurrent_requests": ("RAG_EMBEDDING_CONCURRENT_REQUESTS", "0", int),
    "task.model.default": ("TASK_MODEL", "", str),
    "rag.reranking_model": ("RAG_RERANKING_MODEL", "", str),
}
# Read with rag.top_k: with hybrid search on, Open WebUI keeps min(top_k, top_k_reranker) passages.
TOP_K_DEPS = ("rag.enable_hybrid_search", "rag.top_k_reranker")


def env_value(key):
    if key in SEARCH_ENV:
        name, default, convert = SEARCH_ENV[key]
        raw = os.environ.get(name)
        if raw is None or (raw == "" and convert is int):
            raw = default
        if key == "rag.ollama.base_url" and raw is None:
            raw = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
        try:
            return convert(raw)
        except (TypeError, ValueError):
            return raw
    return os.environ.get(ENV_NAMES.get(key, ""), None)


def as_int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def norm_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        value = value.split(",")
    return sorted({str(v).strip().lower() for v in value if str(v).strip()})


def matches(key, effective, expected):
    """True when Open WebUI 0.12.4 would behave as with `expected` (retrieval/loaders/main.py, routers/files.py)."""
    if key == "rag.content_extraction_engine":
        # Loader: `self.engine == 'tika'`, an exact comparison ("Tika" would not select Tika).
        return effective == expected
    if key == "rag.tika_server_url":
        # TikaLoader strips a trailing "/".
        return isinstance(effective, str) and effective.strip().rstrip("/") == str(expected).rstrip("/")
    if key == "rag.tika_server_version":
        # TikaLoader: str(version or '3'); only '4' selects the 4.x API.
        return (str(effective or "3") == "4") == (str(expected or "3") == "4")
    if key == "rag.reranking_model":
        # Empty and unset both mean "no reranker" to Open WebUI (get_rf loads nothing).
        return (effective or "") == (expected or "")
    if key in ("rag.embedding_engine", "rag.embedding_model", "task.model.default"):
        # Used as given: the embedding engine selects the loader by exact name; models are exact names.
        return effective == expected
    if key == "rag.ollama.base_url":
        return isinstance(effective, str) and effective.strip().rstrip("/") == str(expected).rstrip("/")
    if key == "rag.top_k":
        # A minimum; main() passes the number of passages Open WebUI really keeps.
        return as_int(effective) is not None and as_int(effective) >= int(expected)
    if key in ("rag.embedding_batch_size", "rag.embedding_concurrent_requests"):
        return as_int(effective) == int(expected)
    if key == "rag.enable_hybrid_search":
        return effective is expected or (isinstance(effective, str) and effective.lower() == str(expected).lower())
    if key == "rag.file.max_size":
        try:
            return int(str(effective).strip()) == int(expected)
        except (TypeError, ValueError):
            return False
    if key == "rag.content_extraction.supported_media_mime_types":
        have = norm_list(effective)
        # Every expected type must be accepted: listed, or covered by type/* or */* (Open WebUI matches with
        # mimeparse). Extra types the admin added are fine.
        return all(t in have or t.split("/")[0] + "/*" in have or "*/*" in have for t in norm_list(expected))
    return effective == expected


def unsupported(error):
    print(json.dumps({"ok": False, "source": "unsupported", "error": str(error)[:300]}))
    return 2


def read_rows(db_path, keys):
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=DB_TIMEOUT)
    try:
        con.execute("PRAGMA query_only = ON")
        marks = ",".join("?" for _ in keys)
        rows = {}
        for key, value in con.execute(f"SELECT key, value FROM config WHERE key IN ({marks})", list(keys)):
            rows[key] = json.loads(value) if isinstance(value, (str, bytes)) else value
        return rows
    finally:
        con.close()


def main(argv=None):
    argv = sys.argv if argv is None else argv
    expected = json.loads(argv[1])
    persistent = os.environ.get("ENABLE_PERSISTENT_CONFIG", "True").lower() == "true"
    db_url = os.environ.get("DATABASE_URL", "")
    db_path = os.path.join(os.environ.get("DATA_DIR", "/app/backend/data"), "webui.db")

    rows, source = {}, "environment (ENABLE_PERSISTENT_CONFIG=false)"
    if persistent:
        if db_url and not db_url.startswith("sqlite:///"):
            return unsupported("DATABASE_URL is not sqlite")
        if db_url:
            db_path = db_url[len("sqlite:///") :]
        if not os.path.isfile(db_path):
            return unsupported(f"no database at {db_path}")
        source = "database"
        wanted = list(expected) + [k for k in TOP_K_DEPS if "rag.top_k" in expected and k not in expected]
        try:
            rows = read_rows(db_path, wanted)
        except (sqlite3.Error, ValueError) as exc:  # locked, no table, not a database, bad JSON in a row
            return unsupported(f"{type(exc).__name__}: {exc}")

    # Where there is no row, Open WebUI uses the value it derived from the environment at start, which the
    # role set from the same variables as `expected`.
    def effective(key):
        if key in rows:
            return rows[key], "database"
        return env_value(key), "environment"

    result = {}
    for key, want in expected.items():
        have, origin = effective(key)
        entry = {"effective": have, "expected": want, "from": origin}
        compared = have
        if key == "rag.top_k":
            hybrid, _ = effective("rag.enable_hybrid_search")
            reranker, _ = effective("rag.top_k_reranker")
            hybrid = hybrid is True or (isinstance(hybrid, str) and hybrid.lower() == "true")
            if hybrid and as_int(have) is not None and as_int(reranker) is not None:
                compared = min(as_int(have), as_int(reranker))
            entry.update(hybrid=hybrid, top_k_reranker=reranker, passages=as_int(compared))
        entry["ok"] = matches(key, compared, want)
        result[key] = entry

    ok = all(r["ok"] for r in result.values())
    print(json.dumps({"ok": ok, "source": source, "settings": result}))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
