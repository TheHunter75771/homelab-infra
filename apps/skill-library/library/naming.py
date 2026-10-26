"""Names: kebab-case slugs, topic/skill names, item file names, and cap sizes."""
from __future__ import annotations

import datetime as dt
import re
import unicodedata

MAX_DIR_NAME = 48     # topic and skill names (CONTRACTS §12.11)
MAX_SLUG = 80         # keeps full paths well inside Windows' 260-character habit
MAX_EXT = 16

_KEBAB_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
# YYYY-MM-DD_kebab-title.ext ; the extension is one lowercase [a-z0-9] run.
_ITEM_RE = re.compile(
    r"(?P<date>\d{4}-\d{2}-\d{2})_(?P<slug>[a-z0-9]+(?:-[a-z0-9]+)*)\.(?P<ext>[a-z0-9]{1,16})"
)
# Names Windows refuses for any file or folder (the share is read from Windows too). The
# superscript variants (COM¹ ...) cannot occur: names are [a-z0-9-] only.
_WINDOWS_RESERVED = frozenset(
    {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(10)} | {f"lpt{i}" for i in range(10)}
)

ITEM_PATTERN = "YYYY-MM-DD_kebab-title.ext"


def is_kebab_name(name: str) -> bool:
    """A valid topic or skill folder name: kebab-case, 1-48 chars, not a Windows device name.

    Stricter than the contract's ``[a-z0-9-]{1,48}``: no leading, trailing or doubled hyphens.
    """
    return (
        len(name) <= MAX_DIR_NAME
        and _KEBAB_RE.fullmatch(name) is not None
        and name not in _WINDOWS_RESERVED
    )


def slugify(title: str, max_len: int = MAX_SLUG) -> str:
    """Turn any title into a kebab-case slug ("Ünïcode: A/B test!" -> "unicode-a-b-test").

    Accents are folded to ASCII; anything else that is not a letter or digit becomes a hyphen.
    A title with nothing usable (e.g. only CJK) becomes "untitled".
    """
    ascii_text = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    if len(slug) > max_len:
        cut = slug[:max_len]
        # Prefer cutting at a word boundary.
        slug = (cut.rsplit("-", 1)[0] if "-" in cut else cut).strip("-")
    return slug or "untitled"


def normalize_ext(filename: str) -> str:
    """The item extension for an incoming file name: last suffix, lowercased, [a-z0-9] only.

    A name with no usable extension gets "bin" (the sidecar's `source` keeps the original name).
    """
    head, dot, ext = filename.rpartition(".")
    if not dot or not head:
        return "bin"
    ext = re.sub(r"[^a-z0-9]", "", ext.lower())[:MAX_EXT]
    return ext or "bin"


def is_valid_utf8(name: str) -> bool:
    """False for a name Python decoded with surrogate escapes (bytes that are not UTF-8)."""
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def display(text: str) -> str:
    """Text that is safe to print on one line: undecodable bytes and control characters escaped.

    A name holding the raw byte 0xff prints as ``b-\\xff.txt``; a newline prints as ``\\n``. Used
    for messages, never for paths we act on.
    """
    out = []
    for ch in text:
        o = ord(ch)
        if 0xDC80 <= o <= 0xDCFF:  # surrogateescape'd byte
            out.append(f"\\x{o - 0xDC00:02x}")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        elif o < 0x20 or 0x7F <= o <= 0x9F or ch in "\u2028\u2029" or 0xD800 <= o <= 0xDFFF:
            out.append(f"\\u{o:04x}")
        else:
            out.append(ch)
    return "".join(out)


def title_from_filename(filename: str) -> str:
    """A readable default title: the file name without its extension, separators as spaces.

    Bytes that are not UTF-8 become U+FFFD so the title can always be written to a header.
    """
    filename = filename.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
    stem = filename.rsplit(".", 1)[0] if "." in filename.lstrip(".") else filename
    return re.sub(r"[_\s]+", " ", stem).strip() or filename


def parse_item_name(name: str) -> tuple[str, str, str] | None:
    """(date, slug, ext) if ``name`` follows YYYY-MM-DD_kebab-title.ext with a real date."""
    m = _ITEM_RE.fullmatch(name)
    if not m:
        return None
    try:
        dt.date.fromisoformat(m.group("date"))
    except ValueError:
        return None
    return m.group("date"), m.group("slug"), m.group("ext")


def item_name(date: str, slug: str, ext: str, n: int = 1) -> str:
    """The n-th candidate name: n=1 is plain, n>=2 adds "-n" before the extension."""
    suffix = "" if n == 1 else f"-{n}"
    return f"{date}_{slug}{suffix}.{ext}"


def numbered(name: str, n: int, *, is_dir: bool) -> str:
    """Generic collision name for archive moves: "x.pdf" -> "x-2.pdf", folder "x" -> "x-2"."""
    if n == 1:
        return name
    if is_dir or "." not in name.lstrip("."):
        return f"{name}-{n}"
    stem, ext = name.rsplit(".", 1)
    return f"{stem}-{n}.{ext}"


# --------------------------------------------------------------------------- sizes

_SIZE_RE = re.compile(r"\s*(\d+(?:\.\d+)?)\s*([kmgt]i?b?|b)?\s*", re.IGNORECASE)
_UNITS = {"": 1, "b": 1}
for _i, _p in enumerate("kmgt", start=1):
    _UNITS[_p] = _UNITS[_p + "b"] = 1000 ** _i
    _UNITS[_p + "i"] = _UNITS[_p + "ib"] = 1024 ** _i


def parse_size(text: str) -> int:
    """Bytes from "500GB", "500 G", "750GiB", "10MB", "123" (decimal units unless "i" is used)."""
    m = _SIZE_RE.fullmatch(text)
    if not m:
        raise ValueError(f"not a size: {text!r} (examples: 500GB, 750GiB, 10MB, 4096)")
    number, unit = m.group(1), (m.group(2) or "").lower()
    value = int(float(number) * _UNITS[unit])
    if value <= 0:
        raise ValueError(f"the cap must be more than 0 bytes: {text!r}")
    return value


def format_size(n: int) -> str:
    """Decimal, one decimal place: 1234567 -> "1.2 MB"."""
    for unit, scale in (("TB", 10**12), ("GB", 10**9), ("MB", 10**6), ("KB", 10**3)):
        if n >= scale:
            return f"{n / scale:.1f} {unit}"
    return f"{n} B"
