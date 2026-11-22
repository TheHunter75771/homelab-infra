"""A small, safe ``.docx`` reader: stdlib only, paragraph text from ``word/document.xml``.

What it reads: only ``word/document.xml``, never another part, so comments (``comments.xml``),
headers and footers (``header*.xml``/``footer*.xml``), footnotes and embedded media are never
opened. Inside the document it keeps the text of runs and skips:

* tracked deletions and moves away (``w:del``, ``w:moveFrom``, ``w:delText``); tracked
  insertions are the current text and are kept;
* field codes (``w:instrText``; the field's displayed result is kept), drawings, pictures,
  embedded objects and text boxes (``w:drawing``, ``w:pict``, ``w:object``, ``w:txbxContent``),
  and the ``mc:Fallback`` copy of alternate content;
* hidden runs (``w:vanish``) and content controls that Word marks as a cover page, a table of
  contents or a bibliography (``w:docPartGallery``);
* tables (``w:tbl``): table cells hold forms, rubrics and other people's words, not prose.

It also reads ``word/styles.xml`` (formatting only, with the same safe parser and a size cap) to
learn each paragraph style's name and indentation, so a block quote whose indent or "Quote" name
comes only from its style is still recognised. ``[Content_Types].xml`` is scanned (bytes, never
parsed) for macro content types.

Bounded work: the zip's central directory is walked by hand from the end-of-central-directory
record (entry count and record sizes) before ``zipfile`` parses it, so a zip packed with tiny
entries is refused without building them; ``word/document.xml`` is streamed through expat in
chunks, never held whole; the text kept per sample is capped (``Caps.max_text_chars`` in total,
``max_paragraphs``, ``max_paragraph_chars`` each): an oversized sample is refused, not truncated.

What it refuses (``DocxRefused``, a one-line reason; nothing is ever executed or extracted to
disk): a symlink or non-regular file, a file over ``max_docx_bytes``, a bad zip, a ZIP64 or
multi-disk zip, a central directory that does not match its end record, more than
``max_zip_entries`` entries, an entry name that is absolute, has ``..``, a backslash, a drive
letter or a NUL, duplicate names, an encrypted entry, a VBA macro part or a macro content type
in ``[Content_Types].xml``, a declared total over
``max_uncompressed_bytes``, a compression ratio over ``max_ratio`` (per entry over 1 MiB and in
total), a compression method other than stored/deflated, a ``word/document.xml`` that inflates
past ``max_xml_bytes``, and XML that is not UTF-8 or carries a DOCTYPE or ENTITY declaration
(the defusedxml rules, done with expat directly: no DTD, no entities, no external references),
elements nested deeper than ``MAX_DEPTH``, and any single tag, comment, PI or CDATA section over
``MAX_MARKUP`` bytes (expat buffers each whole).
"""
from __future__ import annotations

import codecs
import io
import os
import re
import stat
import struct
import zipfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from xml.parsers import expat

DOCUMENT = "word/document.xml"
STYLES = "word/styles.xml"
CONTENT_TYPES = "[content_types].xml"     # compared casefolded
STYLES_MAX = 2 * 1024 * 1024
MAX_DEPTH = 128                  # element nesting (real documents stay under ~40)
MAX_MARKUP = 64 * 1024           # one tag (name + attributes), comment, PI or CDATA section
CONTENT_TYPES_MAX = 1024 * 1024
W_NAMESPACES = frozenset({
    "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "http://purl.oclc.org/ooxml/wordprocessingml/main",
})
MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
RATIO_FLOOR = 1024 * 1024        # per-entry ratio is checked for entries larger than this
READ_CHUNK = 64 * 1024

# Elements whose whole subtree is ignored (local names in the w: namespace).
_SKIP_W = frozenset({
    "del", "moveFrom", "delText", "instrText", "txbxContent", "drawing", "pict", "object",
    "rPrChange", "pPrChange", "sectPrChange", "tblPrChange", "trPrChange", "tcPrChange",
    "numberingChange", "commentReference", "footnoteReference", "endnoteReference", "tbl",
})
_SKIP_GALLERIES = frozenset({"cover pages", "table of contents", "bibliographies"})
_DECL_RE = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY|ELEMENT|ATTLIST|NOTATION)", re.IGNORECASE)
_DECL_OVERLAP = 64          # bytes kept from the previous chunk, so a split declaration is still seen
_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_MACRO_NAMES = ("vbaproject.bin", "vbadata.xml")
_MACRO_TYPES = (b"macroenabled", b"vbaproject", b"vbadata", b"ms-office.activex")
_EOCD = b"PK\x05\x06"
_CDIR = b"PK\x01\x02"
_ZIP64_LOCATOR = b"PK\x06\x07"


class DocxRefused(ValueError):
    """The file is not read; the message is a safe one-line reason."""


class TooMuchText(DocxRefused):
    """The sample holds more text than the per-sample caps; it is excluded, not truncated."""


@dataclass
class Paragraph:
    text: str
    style: str = ""           # w:pStyle value, e.g. "Heading1", "Quote"
    left: int = 0             # left/start indent, twips
    hanging: int = 0          # hanging indent, twips
    first_line: int = 0       # first-line indent, twips
    is_list: bool = False     # has w:numPr (a bulleted or numbered list item)
    has_left: bool = False    # the left indent was set on the paragraph itself (not inherited)
    has_hanging: bool = False  # likewise the hanging indent (Word merges w:ind attribute by attribute)
    style_name: str = ""      # the style's display name from styles.xml, e.g. "Block Quote"


@dataclass
class Style:
    name: str = ""
    based_on: str = ""
    left: int | None = None
    hanging: int | None = None
    is_default: bool = False


@dataclass(frozen=True)
class Caps:
    max_docx_bytes: int = 5 * 1024 * 1024
    max_zip_entries: int = 2000
    max_uncompressed_bytes: int = 200 * 1024 * 1024
    max_xml_bytes: int = 8 * 1024 * 1024
    max_ratio: float = 200
    max_text_chars: int = 1_000_000     # text kept per sample
    max_paragraphs: int = 5000
    max_paragraph_chars: int = 20_000

    @classmethod
    def from_config(cls, cfg: dict) -> Caps:
        return cls(cfg["max_docx_bytes"], cfg["max_zip_entries"], cfg["max_uncompressed_bytes"],
                   cfg["max_xml_bytes"], cfg["max_ratio"])


# ---------------------------------------------------------------------- the zip

def read_file(path: str | os.PathLike[str], caps: Caps) -> bytes:
    """The bytes of a regular file (never through a symlink), at most ``max_docx_bytes``."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError as exc:
        raise DocxRefused(f"cannot open ({exc.strerror or type(exc).__name__})") from None
    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise DocxRefused("not a regular file")
        if st.st_size > caps.max_docx_bytes:
            raise DocxRefused(f"larger than {caps.max_docx_bytes} bytes")
        data = fh.read(caps.max_docx_bytes + 1)
    if len(data) > caps.max_docx_bytes:
        raise DocxRefused(f"larger than {caps.max_docx_bytes} bytes")
    return data


def _check_name(name: str) -> None:
    parts = name.replace("\\", "/").split("/")
    if (not name or name.startswith("/") or "\\" in name or "\0" in name or _DRIVE_RE.match(name)
            or any(p in ("..", ".") for p in parts)):
        raise DocxRefused(f"unsafe entry name {name[:60]!r}")


def check_directory(data: bytes, caps: Caps) -> None:
    """Walk the central directory by hand before ``zipfile`` builds an object per entry: the end
    record must be plain (no ZIP64, one disk), and the records must chain exactly to it, at most
    ``max_zip_entries`` of them. Constant memory; time linear in the directory's size."""
    if len(data) >= 22 and data[-22:-18] == _EOCD and data[-2:] == b"\0\0":
        pos = len(data) - 22                       # no comment: where zipfile looks first
    else:
        pos = data.rfind(_EOCD, max(0, len(data) - 22 - 65535))
    if pos < 0 or len(data) - pos < 22:
        raise DocxRefused("not a valid .docx (zip) file")
    disk, cd_disk, n_disk, n_total, cd_size, cd_offset, _ = struct.unpack("<HHHHIIH", data[pos + 4:pos + 22])
    if (n_total == 0xFFFF or cd_size == 0xFFFFFFFF or cd_offset == 0xFFFFFFFF
            or (pos >= 20 and data[pos - 20:pos - 16] == _ZIP64_LOCATOR)):
        raise DocxRefused("a ZIP64 archive (not a normal .docx)")
    if disk or cd_disk or n_disk != n_total:
        raise DocxRefused("a multi-disk zip (not a normal .docx)")
    if n_total > caps.max_zip_entries:
        raise DocxRefused(f"more than {caps.max_zip_entries} zip entries")
    p = pos - cd_size
    if p < 0:
        raise DocxRefused("not a valid .docx (zip) file")
    count = 0
    while p < pos:
        if data[p:p + 4] != _CDIR or p + 46 > pos:
            raise DocxRefused("corrupt zip central directory")
        n, m, k = struct.unpack("<HHH", data[p + 28:p + 34])
        p += 46 + n + m + k
        count += 1
        if count > caps.max_zip_entries:
            raise DocxRefused(f"more than {caps.max_zip_entries} zip entries")
    if p != pos or count != n_total:
        raise DocxRefused("corrupt zip central directory")


def _read_entry(zf: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int) -> Iterator[bytes]:
    """The entry's bytes in chunks, refusing it once it inflates past ``limit``."""
    if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
        raise DocxRefused("unsupported compression method")
    if info.file_size > limit:
        raise DocxRefused(f"{info.filename[:60]} is larger than {limit} bytes")
    size = 0
    try:
        with zf.open(info) as fh:
            while True:
                chunk = fh.read(READ_CHUNK)
                if not chunk:
                    return
                size += len(chunk)
                if size > limit:
                    raise DocxRefused(f"{info.filename[:60]} inflates past {limit} bytes")
                yield chunk
    except (zipfile.BadZipFile, zipfile.LargeZipFile, ValueError, EOFError, OSError, RuntimeError,
            NotImplementedError):
        raise DocxRefused(f"{info.filename[:60]} cannot be read (corrupt or unsupported)") from None


def open_docx(data: bytes, caps: Caps) -> tuple[zipfile.ZipFile, zipfile.ZipInfo, zipfile.ZipInfo | None]:
    """(the zip, its word/document.xml, its word/styles.xml or None), after every zip check."""
    check_directory(data, caps)
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, zipfile.LargeZipFile, ValueError, EOFError, OSError):
        raise DocxRefused("not a valid .docx (zip) file") from None
    try:
        infos = zf.infolist()
        if len(infos) > caps.max_zip_entries:
            raise DocxRefused(f"more than {caps.max_zip_entries} zip entries")
        seen: set[str] = set()
        total = compressed = 0
        doc = styles = types = None
        for info in infos:
            _check_name(info.filename)
            key = info.filename.casefold()
            if key in seen:
                raise DocxRefused(f"duplicate entry name {info.filename[:60]!r}")
            seen.add(key)
            if key.rsplit("/", 1)[-1] in _MACRO_NAMES:
                raise DocxRefused("contains VBA macros (a macro-enabled document); refused")
            if info.flag_bits & 0x1:
                raise DocxRefused("contains an encrypted entry")
            total += info.file_size
            compressed += info.compress_size
            if total > caps.max_uncompressed_bytes:
                raise DocxRefused(f"declared uncompressed size over {caps.max_uncompressed_bytes} bytes")
            if info.file_size > RATIO_FLOOR and info.file_size > caps.max_ratio * max(info.compress_size, 1):
                raise DocxRefused(f"compression ratio over {caps.max_ratio} ({info.filename[:60]!r}): zip bomb")
            if info.filename == DOCUMENT:
                doc = info
            elif info.filename == STYLES:
                styles = info
            elif key == CONTENT_TYPES:
                types = info
        if total > RATIO_FLOOR and total > caps.max_ratio * max(compressed, 1):
            raise DocxRefused(f"total compression ratio over {caps.max_ratio}: zip bomb")
        if doc is None:
            raise DocxRefused(f"no {DOCUMENT} (not a Word document)")
        if types is not None:
            declared = b"".join(_read_entry(zf, types, CONTENT_TYPES_MAX)).lower()
            if any(t in declared for t in _MACRO_TYPES):
                raise DocxRefused("declares macro content (a macro-enabled document); refused")
        if doc.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise DocxRefused("unsupported compression method")
        if doc.file_size > caps.max_xml_bytes:
            raise DocxRefused(f"{DOCUMENT} is larger than {caps.max_xml_bytes} bytes")
    except BaseException:
        zf.close()
        raise
    return zf, doc, styles


def document_xml(data: bytes, caps: Caps) -> bytes:
    """``word/document.xml`` from the zip in ``data``, after every zip check (whole; for tests)."""
    zf, doc, _ = open_docx(data, caps)
    with zf:
        return b"".join(_read_entry(zf, doc, caps.max_xml_bytes))


# ---------------------------------------------------------------------- the XML

class _Walker:
    """Expat event handler that collects paragraphs (see the module doc for what is skipped)."""

    def __init__(self, caps: Caps | None = None) -> None:
        self.caps = caps or Caps()
        self.chars = 0                             # text kept so far (all paragraphs)
        self.paragraphs: list[Paragraph] = []
        self.stack: list[tuple[str, str]] = []     # (namespace, local name)
        self.skip_at: int | None = None            # depth of the element that started a skip
        self.paras: list[Paragraph] = []           # open paragraphs (a table cell's p is not nested)
        self.parts: list[list[str]] = []
        self.run_hidden = False
        self.in_text = False
        self.sdt: list[bool] = []                  # per open w:sdt: skip its content?

    @staticmethod
    def _split(name: str) -> tuple[str, str]:
        ns, _, local = name.rpartition(" ")
        return ns, local

    @staticmethod
    def _attr(attrs: dict[str, str], local: str) -> str | None:
        for key, value in attrs.items():
            ns, _, name = key.rpartition(" ")
            if name == local and (ns in W_NAMESPACES or not ns):
                return value
        return None

    @staticmethod
    def _twips(value: str | None) -> int:
        try:
            return int(value) if value is not None else 0
        except ValueError:
            return 0

    def _parent(self, back: int = 1) -> str:
        if len(self.stack) < back:
            return ""
        ns, local = self.stack[-back]
        return local if ns in W_NAMESPACES else ""

    def start(self, name: str, attrs: dict[str, str]) -> None:
        if len(self.stack) >= MAX_DEPTH:
            raise DocxRefused(f"{DOCUMENT} nests elements deeper than {MAX_DEPTH}")
        ns, local = self._split(name)
        self.stack.append((ns, local))
        if self.skip_at is not None:
            return
        if ns == MC_NS and local == "Fallback":
            self.skip_at = len(self.stack)
            return
        if ns not in W_NAMESPACES:
            return
        if local in _SKIP_W:
            self.skip_at = len(self.stack)
            return
        if local == "sdt":
            self.sdt.append(False)
        elif local == "docPartGallery" and self.sdt and "sdtPr" in [n for _, n in self.stack[-4:]]:
            if (self._attr(attrs, "val") or "").strip().casefold() in _SKIP_GALLERIES:
                self.sdt[-1] = True
        elif local == "sdtContent" and self.sdt and self.sdt[-1]:
            self.skip_at = len(self.stack)
        elif local == "p":
            self.paras.append(Paragraph(""))
            self.parts.append([])
        elif not self.paras:
            return
        elif local == "pStyle" and self._parent(2) == "pPr":
            self.paras[-1].style = self._attr(attrs, "val") or ""
        elif local == "ind" and self._parent(2) == "pPr":
            p = self.paras[-1]
            left = self._attr(attrs, "left") or self._attr(attrs, "start")
            hanging = self._attr(attrs, "hanging")
            p.left, p.has_left = self._twips(left), left is not None
            p.hanging, p.has_hanging = self._twips(hanging), hanging is not None
            p.first_line = self._twips(self._attr(attrs, "firstLine"))
        elif local == "numPr" and self._parent(2) == "pPr":
            self.paras[-1].is_list = True
        elif local == "r":
            self.run_hidden = False
        elif local == "vanish" and self._parent(2) == "rPr" and self._parent(3) == "r":
            self.run_hidden = (self._attr(attrs, "val") or "true").casefold() not in ("0", "false", "off")
        elif local == "t":
            self.in_text = True
        elif local == "tab" and self._parent(2) == "r":
            self._emit(" ")
        elif local in ("br", "cr"):
            self._emit("\n")
        elif local == "noBreakHyphen":
            self._emit("-")

    def end(self, name: str) -> None:
        depth = len(self.stack)
        ns, local = self.stack.pop()
        if self.skip_at is not None:
            if depth == self.skip_at:
                self.skip_at = None
            return
        if ns not in W_NAMESPACES:
            return
        if local == "t":
            self.in_text = False
        elif local == "r":
            self.run_hidden = False
        elif local == "sdt" and self.sdt:
            self.sdt.pop()
        elif local == "p" and self.paras:
            para = self.paras.pop()
            para.text = "".join(self.parts.pop())
            self.para_chars = 0
            if para.text.strip():
                if len(self.paragraphs) >= self.caps.max_paragraphs:
                    raise TooMuchText(f"too much text: more than {self.caps.max_paragraphs} paragraphs")
                self.paragraphs.append(para)

    para_chars = 0

    def _emit(self, text: str) -> None:
        if self.paras and not self.run_hidden and self.skip_at is None:
            self.chars += len(text)
            self.para_chars += len(text)
            if self.para_chars > self.caps.max_paragraph_chars:
                raise TooMuchText(f"too much text: a paragraph over {self.caps.max_paragraph_chars} characters")
            if self.chars > self.caps.max_text_chars:
                raise TooMuchText(f"too much text: over {self.caps.max_text_chars} characters")
            self.parts[-1].append(text)

    def data(self, text: str) -> None:
        if self.in_text:
            self._emit(text)


def _refuse_declaration(*_args) -> None:
    raise DocxRefused("the XML declares a DOCTYPE or ENTITY; refused")


class _MarkupGuard:
    """A byte-level scan, fed the same chunks as expat and ahead of it, that refuses any single
    piece of markup longer than ``MAX_MARKUP``: a start or end tag (its name and every attribute,
    quote-aware, so a ``>`` inside an attribute value does not end it), a comment, a processing
    instruction or a CDATA section. Expat buffers each of those whole before it reports them (a
    30 MB attribute cost 122 MB), so this bounds its memory; text between tags is streamed by
    expat and is not limited here. Linear: each byte is looked at a constant number of times."""

    _TAG_STOP = re.compile(rb"[\"'>]")
    _OPENERS = ((b"<!--", b"-->"), (b"<![CDATA[", b"]]>"), (b"<?", b"?>"))

    def __init__(self, part: str) -> None:
        self.part = part
        self.state = "text"      # text | tag | quote | until
        self.quote = b""
        self.until = b""
        self.length = 0
        self.carry = b""

    def _grow(self, n: int) -> None:
        self.length += n
        if self.length > MAX_MARKUP:
            raise DocxRefused(f"{self.part} has a tag, comment or other markup over {MAX_MARKUP} bytes")

    def feed(self, chunk: bytes, final: bool = False) -> None:
        buf = self.carry + chunk
        self.carry = b""
        i, n = 0, len(buf)
        while i < n:
            if self.state == "text":
                j = buf.find(b"<", i)
                if j < 0:
                    return
                head = buf[j:j + 9]
                if not final and len(head) < 9 and any(o.startswith(head) for o, _ in self._OPENERS):
                    self.carry = buf[j:]          # too few bytes to tell a comment from a tag yet
                    return
                self.length = 0
                for opener, closer in self._OPENERS:
                    if buf.startswith(opener, j):
                        self.state, self.until = "until", closer
                        self._grow(len(opener))
                        i = j + len(opener)
                        break
                else:
                    self.state = "tag"
                    self._grow(1)
                    i = j + 1
            elif self.state == "tag":
                m = self._TAG_STOP.search(buf, i)
                if m is None:
                    self._grow(n - i)
                    return
                self._grow(m.end() - i)
                i = m.end()
                if m.group() == b">":
                    self.state = "text"
                else:
                    self.state, self.quote = "quote", m.group()
            elif self.state == "quote":
                j = buf.find(self.quote, i)
                if j < 0:
                    self._grow(n - i)
                    return
                self._grow(j + 1 - i)
                i = j + 1
                self.state = "tag"
            else:  # until the closer of a comment, PI or CDATA section
                j = buf.find(self.until, i)
                if j < 0:
                    keep = min(len(self.until) - 1, n - i)   # a closer may straddle chunks
                    self._grow(n - i - keep)
                    if not final:
                        self.carry = buf[n - keep:]
                    return
                self._grow(j + len(self.until) - i)
                i = j + len(self.until)
                self.state = "text"


def _safe_parse(chunks: Iterable[bytes], handler: object, part: str) -> None:
    """Feed ``chunks`` of one XML part to expat with the defusedxml rules: UTF-8 only (checked
    incrementally), and no DOCTYPE/ENTITY/ELEMENT/ATTLIST/NOTATION declaration (a byte scan of
    every chunk before expat sees it, and expat handlers that refuse any DTD as well)."""
    decoder = codecs.getincrementaldecoder("utf-8")()
    guard = _MarkupGuard(part)
    parser = None
    tail = b""
    try:
        for chunk in chunks:
            if parser is None and chunk.startswith((b"\xff\xfe", b"\xfe\xff", b"\x00\x00\xfe\xff")):
                raise DocxRefused(f"{part} is not UTF-8")  # a UTF-16/32 byte-order mark
            try:
                decoder.decode(chunk)
            except UnicodeDecodeError:
                raise DocxRefused(f"{part} is not UTF-8") from None
            if _DECL_RE.search(tail + chunk):
                raise DocxRefused("the XML declares a DOCTYPE or ENTITY; refused")
            tail = chunk[-_DECL_OVERLAP:]
            guard.feed(chunk)
            if parser is None:
                parser = _parser(handler)
            parser.Parse(chunk, False)
        try:
            decoder.decode(b"", True)
        except UnicodeDecodeError:
            raise DocxRefused(f"{part} is not UTF-8") from None
        guard.feed(b"", True)
        if parser is None:
            parser = _parser(handler)
        parser.Parse(b"", True)
    except expat.ExpatError as exc:
        raise DocxRefused(f"{part} is not well-formed XML ({expat.ErrorString(exc.code)})") from None


def _parser(handler) -> expat.XMLParserType:
    parser = expat.ParserCreate("UTF-8", namespace_separator=" ")
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    parser.StartDoctypeDeclHandler = _refuse_declaration
    parser.EntityDeclHandler = _refuse_declaration
    parser.UnparsedEntityDeclHandler = _refuse_declaration
    parser.ExternalEntityRefHandler = _refuse_declaration
    parser.StartElementHandler = handler.start
    parser.EndElementHandler = handler.end
    parser.CharacterDataHandler = handler.data
    parser.buffer_text = True
    return parser


def _chunked(xml: bytes) -> Iterator[bytes]:
    for i in range(0, len(xml), READ_CHUNK):
        yield xml[i:i + READ_CHUNK]
    if not xml:
        yield b""


def parse_document(xml: bytes | Iterable[bytes], caps: Caps | None = None,
                   styles: dict[str, Style] | None = None) -> list[Paragraph]:
    """Paragraphs of ``word/document.xml`` (bytes, or an iterable of chunks). Raises DocxRefused."""
    walker = _Walker(caps)
    _safe_parse(_chunked(xml) if isinstance(xml, (bytes, bytearray)) else xml, walker, DOCUMENT)
    apply_styles(walker.paragraphs, styles or {})
    return walker.paragraphs


# ---------------------------------------------------------------------- styles

class _StyleWalker:
    """Paragraph styles from ``word/styles.xml``: id, name, basedOn, indentation, default."""

    def __init__(self) -> None:
        self.styles: dict[str, Style] = {}
        self.stack: list[str] = []
        self.current: Style | None = None
        self.count = 0

    def start(self, name: str, attrs: dict[str, str]) -> None:
        if len(self.stack) >= MAX_DEPTH:
            raise DocxRefused(f"{STYLES} nests elements deeper than {MAX_DEPTH}")
        ns, _, local = name.rpartition(" ")
        local = local if ns in W_NAMESPACES else ""
        parent = self.stack[-1] if self.stack else ""
        self.stack.append(local)
        if local == "style" and parent == "styles":
            self.count += 1
            if self.count > 5000:
                raise DocxRefused(f"{STYLES} has more than 5000 styles")
            kind = _Walker._attr(attrs, "type") or ""
            sid = _Walker._attr(attrs, "styleId") or ""
            self.current = None
            if kind == "paragraph" and sid:
                default = (_Walker._attr(attrs, "default") or "").casefold() in ("1", "true", "on")
                self.current = self.styles[sid] = Style(is_default=default)
        elif self.current is None:
            return
        elif local == "name" and parent == "style":
            self.current.name = (_Walker._attr(attrs, "val") or "")[:200]
        elif local == "basedOn" and parent == "style":
            self.current.based_on = _Walker._attr(attrs, "val") or ""
        elif local == "ind" and parent == "pPr" and self.stack[-3:-2] == ["style"]:
            left = _Walker._attr(attrs, "left") or _Walker._attr(attrs, "start")
            hanging = _Walker._attr(attrs, "hanging")
            self.current.left = _Walker._twips(left) if left is not None else None
            self.current.hanging = _Walker._twips(hanging) if hanging is not None else None

    def end(self, name: str) -> None:
        local = self.stack.pop()
        if local == "style" and self.stack[-1:] == ["styles"]:
            self.current = None

    def data(self, text: str) -> None:
        pass


def parse_styles(xml: bytes | Iterable[bytes]) -> dict[str, Style]:
    walker = _StyleWalker()
    _safe_parse(_chunked(xml) if isinstance(xml, (bytes, bytearray)) else xml, walker, STYLES)
    return walker.styles


def _resolve(styles: dict[str, Style], sid: str) -> tuple[list[str], int | None, int | None]:
    """(names along the basedOn chain, inherited left indent, inherited hanging indent)."""
    names: list[str] = []
    left = hanging = None
    seen: set[str] = set()
    while sid and sid in styles and sid not in seen and len(seen) < 20:
        seen.add(sid)
        st = styles[sid]
        names.append(st.name)
        if left is None:
            left = st.left
        if hanging is None:
            hanging = st.hanging
        sid = st.based_on
    return names, left, hanging


def apply_styles(paragraphs: list[Paragraph], styles: dict[str, Style]) -> None:
    """Give each paragraph its style's display name (the first in its basedOn chain that names a
    quote, else its own) and, for each of the left and hanging indents the paragraph does not set
    itself, the style's (the left one relative to the default paragraph style's)."""
    default = next((sid for sid, st in styles.items() if st.is_default), "")
    _, base_left, _ = _resolve(styles, default)
    base = base_left or 0
    cache: dict[str, tuple[list[str], int | None, int | None]] = {}
    for para in paragraphs:
        sid = para.style or default
        if not sid:
            continue
        if sid not in cache:
            cache[sid] = _resolve(styles, sid)
        names, left, hanging = cache[sid]
        quote = [n for n in names if re.search(r"(?i)quot|block\s*text|epigraph", n)]
        para.style_name = quote[0] if quote else (names[0] if names else "")
        if not para.has_left:
            para.left = max(0, (left or 0) - base)
        if not para.has_hanging:
            para.hanging = hanging or 0


def read_docx(path: str | os.PathLike[str], caps: Caps) -> list[Paragraph]:
    """Paragraphs of a ``.docx`` file. Raises DocxRefused (TooMuchText for an oversized sample)."""
    zf, doc, styles_info = open_docx(read_file(path, caps), caps)
    with zf:
        styles = parse_styles(_read_entry(zf, styles_info, STYLES_MAX)) if styles_info else {}
        return parse_document(_read_entry(zf, doc, caps.max_xml_bytes), caps, styles)
