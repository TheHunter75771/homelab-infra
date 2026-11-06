"""Worst-shape synthetic document.xml generators (after Reviewer K's round-2 repro), sized near the
8 MiB XML cap. Random letters keep the compression ratio under the zip-bomb limit, so each shape
reaches the XML parser instead of being refused at the zip layer."""
import io
import random
import zipfile

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'

def xml_for(kind: str, target: int, seed: int = 7) -> str:
    rng = random.Random(seed)
    letters = "abcdefghijklmnop"
    def rnd(n): return "".join(rng.choice(letters) for _ in range(n))
    head, tail = f"<w:document {W}><w:body>", "</w:body></w:document>"
    if kind == "deep":
        names = [f"w:{rnd(3)}" for _ in range(64)]
        opens, n = [], 0
        while n < target:
            t = rng.choice(names); opens.append(t); n += len(t) * 2 + 5
        return head + "".join(f"<{t}>" for t in opens) + "".join(f"</{t}>" for t in reversed(opens)) + tail
    if kind == "deep127":   # many subtrees just under the depth cap
        unit = "".join(f"<w:x{rnd(2)}>" for _ in range(120))
        closes = "".join(f"</{t}>" for t in reversed([x[1:-1] for x in unit.replace("><", ">\n<").split("\n")]))
        reps = target // (len(unit) + len(closes))
        return head + (unit + closes) * reps + tail
    if kind == "attr":
        return head + f'<w:p x="{rnd(target)}"/>' + tail
    if kind == "gtattr":    # '>' inside the value must not end the tag
        return head + '<w:p x="' + "".join(rng.choice("ab>") for _ in range(target)) + '"/>' + tail
    if kind == "manyattrs":
        return head + "<w:p " + " ".join(f'a{i}="{rng.choice("ab")}"' for i in range(target // 12)) + "/>" + tail
    if kind == "gtcomment":  # quotes and '>' inside a comment
        return head + "<!--" + "".join(rng.choice("ab>'\"") for _ in range(target)) + "-->" + tail
    if kind == "longname":
        return head + f"<w:{rnd(target)}/>" + tail
    if kind == "comment":
        return head + f"<!--{rnd(target)}-->" + tail
    if kind == "pi":
        return head + f"<?x {rnd(target)}?>" + tail
    if kind == "cdata":
        return head + f"<w:p><w:r><w:t><![CDATA[{rnd(target)}]]></w:t></w:r></w:p>" + tail
    if kind == "emptyparas":
        return head + "".join(f'<w:p w:rsidR="{rnd(8)}"/>' for _ in range(target // 24)) + tail
    if kind == "emptyruns":
        return head + "<w:p>" + "".join(f'<w:r w:rsidR="{rnd(6)}"><w:t>{rng.choice(["", " "])}</w:t></w:r>'
                                        for _ in range(target // 40)) + "</w:p>" + tail
    if kind == "skippedtext":  # a huge table cell: text that is skipped, streamed by expat
        return head + f"<w:tbl><w:tr><w:tc><w:p><w:r><w:t>{rnd(target)}</w:t></w:r></w:p></w:tc></w:tr></w:tbl>" + tail
    raise ValueError(kind)

KINDS = ("deep", "deep127", "attr", "gtattr", "manyattrs", "longname", "comment", "gtcomment", "pi", "cdata",
         "emptyparas", "emptyruns", "skippedtext")

def docx(xml: str) -> bytes:
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", xml)
    return b.getvalue()
