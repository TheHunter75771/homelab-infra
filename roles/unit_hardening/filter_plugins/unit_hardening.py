"""Filters for role unit_hardening (parsing `systemctl cat` and `systemctl show` output).

- unit_hardening_blocks(cat_text): [{'path': ..., 'lines': [...]}], one per file `systemctl cat` printed (it heads
  every file with "# /path/to/file").
- unit_hardening_lines(cat_text, skip_paths): the directive lines systemd reads, from every file except skip_paths
  (this role's own drop-in): no comments, no blanks, no section headers, and no InaccessiblePaths= or
  TemporaryFileSystem= lines (those hide paths; they never use them). Continuation lines are joined.
- unit_hardening_uses(lines, regex): the lines that match the "uses tailscaled's local API" regex (ignore case).
- unit_hardening_matches(lines, regex): the distinct matches themselves, lower case, sorted (the PID-namespace check).
- unit_hardening_exec_paths(lines): the absolute paths named on Exec*= and EnvironmentFile= lines (prefix
  characters stripped, --opt=/path split), in order, without duplicates.
- unit_hardening_props(show_text): `systemctl show` output as a dict (first '=' splits).
- unit_hardening_paths_by_unit(pre): {unit: paths} from the preflight's per-unit facts (the scan's request).
- unit_hardening_bind_sources(value): the source paths of a BindPaths=/BindReadOnlyPaths= `systemctl show` value.
- unit_hardening_denied_ancestors(paths, denied): the entries that are a denied path or a directory above one.
"""
import re
import shlex

HEADER_RE = re.compile(r"^# (/\S+)$")
EXEC_KEYS = ("ExecCondition", "ExecStartPre", "ExecStart", "ExecStartPost", "ExecReload", "ExecStop",
             "ExecStopPost", "EnvironmentFile")
HIDING_KEYS = ("InaccessiblePaths", "TemporaryFileSystem")


def unit_hardening_blocks(cat_text):
    # A "# /path" line is a file header only at the start or after a blank line (systemctl cat separates files
    # with one), so a comment inside a unit that happens to look like a path can't start a new block.
    blocks = []
    prev = ""
    for raw in str(cat_text).splitlines():
        m = HEADER_RE.match(raw)
        is_header = m is not None and (not blocks or prev.strip() == "")
        prev = raw
        if is_header:
            blocks.append({"path": m.group(1), "lines": []})
        elif blocks:
            blocks[-1]["lines"].append(raw)
        else:
            # Text before any header (shouldn't happen): keep it, scanned like the rest.
            blocks.append({"path": "", "lines": [raw]})
    return blocks


def unit_hardening_lines(cat_text, skip_paths=()):
    skip = set(skip_paths or ())
    out = []
    for block in unit_hardening_blocks(cat_text):
        if block["path"] in skip:
            continue
        pending = ""
        for raw in block["lines"]:
            line = raw.strip()
            if not pending and (not line or line[0] in "#;" or (line.startswith("[") and line.endswith("]"))):
                continue
            if line.endswith("\\"):
                pending += line[:-1].rstrip() + " "
                continue
            line = pending + line
            pending = ""
            key = line.split("=", 1)[0].strip()
            if key in HIDING_KEYS:
                continue
            out.append(line)
        if pending:
            out.append(pending.strip())
    return out


def unit_hardening_uses(lines, regex):
    rx = re.compile(regex, re.IGNORECASE)
    return [ln for ln in lines if rx.search(ln)]


def unit_hardening_matches(lines, regex):
    """The distinct matches (lower case), sorted."""
    rx = re.compile(regex, re.IGNORECASE)
    return sorted({m.group(0).lower() for ln in lines for m in rx.finditer(ln)})


def _tokens(value):
    try:
        return shlex.split(value, posix=True)
    except ValueError:
        return value.split()


def unit_hardening_exec_paths(lines):
    paths = []
    for line in lines:
        key, sep, value = line.partition("=")
        if not sep or key.strip() not in EXEC_KEYS:
            continue
        toks = _tokens(value.strip())
        if toks:
            toks[0] = toks[0].lstrip("-@:+!|")
        for tok in toks:
            for part in tok.split("="):
                if part.startswith("/") and part not in paths:
                    paths.append(part)
    return paths


def unit_hardening_paths_by_unit(pre):
    return {unit: list(v.get("paths", [])) for unit, v in dict(pre).items()}


def unit_hardening_bind_sources(value):
    """The source paths of a BindPaths=/BindReadOnlyPaths= property as `systemctl show` prints it
    ("[-]src[:dst[:opts]] ..."), "-" stripped."""
    return [tok.lstrip("-").split(":", 1)[0] for tok in str(value or "").split() if tok.strip("-")]


def unit_hardening_denied_ancestors(paths, denied):
    """The entries of paths that are a denied path, or a directory above one (a bind of it would bring the denied
    path along). A denied entry ending in "." (/run/systemd/io.systemd.) is a prefix: only its directory counts."""
    out = []
    for p in paths:
        p = str(p).rstrip("/")
        for d in denied:
            d = str(d)
            if p == d or d.startswith(p + "/"):
                out.append(p)
                break
    return out


def unit_hardening_props(show_text):
    props = {}
    for line in str(show_text).splitlines():
        key, sep, value = line.partition("=")
        if sep and key not in props:
            props[key] = value
    return props


class FilterModule:
    def filters(self):
        return {
            "unit_hardening_blocks": unit_hardening_blocks,
            "unit_hardening_lines": unit_hardening_lines,
            "unit_hardening_uses": unit_hardening_uses,
            "unit_hardening_matches": unit_hardening_matches,
            "unit_hardening_exec_paths": unit_hardening_exec_paths,
            "unit_hardening_props": unit_hardening_props,
            "unit_hardening_paths_by_unit": unit_hardening_paths_by_unit,
            "unit_hardening_bind_sources": unit_hardening_bind_sources,
            "unit_hardening_denied_ancestors": unit_hardening_denied_ancestors,
        }
