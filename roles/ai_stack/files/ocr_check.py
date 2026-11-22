"""OCR check for role ai_stack (docs/CONTRACTS.md §12.18b). Runs INSIDE the open-webui container, fed on
stdin (`docker exec -i open-webui python3 - <tika url>`), so it travels the same network path as Open WebUI.

It draws a PNG with known words (Pillow ships in the Open WebUI image), saves the same picture as an
image-only PDF (no text layer), and sends both to Tika:
  png  PUT <url>/tika       Content-Type image/png, Accept text/plain  (Tesseract on an image)
  png  PUT <url>/tika/text  no Content-Type, JSON reply  (exactly what Open WebUI 0.12.4's TikaLoader does)
  pdf  PUT <url>/tika/text  no Content-Type, JSON reply  (PDFParser's ocrStrategy on a scanned page)
Prints one JSON object; exit 0 only if every reply is a 200 that contains every word. Nothing is stored.
"""

import io
import json
import random
import sys
import urllib.parse
from http.client import HTTPConnection, HTTPException

# The first OCR call starts Tesseract, and Tika's worker may still be warming up.
TIMEOUT = 180


def render(words):
    """The words as (PNG bytes, image-only PDF bytes). Pillow is imported here, inside the container."""
    from PIL import Image, ImageDraw, ImageFont

    font = ImageFont.load_default(size=56)
    img = Image.new("RGB", (1400, 220), "white")
    ImageDraw.Draw(img).text((40, 70), " ".join(words), fill="black", font=font)
    png, pdf = io.BytesIO(), io.BytesIO()
    img.save(png, format="PNG")
    img.save(pdf, format="PDF", resolution=150.0)
    return png.getvalue(), pdf.getvalue()


def put(url, body, headers):
    # http.client, not urllib: urllib adds "Content-Type: application/x-www-form-urlencoded" to any request
    # with a body, and Tika trusts it (EmptyParser, no text). Open WebUI's requests.put(data=bytes) sends no
    # Content-Type, so Tika detects the type itself; this does the same.
    u = urllib.parse.urlsplit(url)
    conn = HTTPConnection(u.hostname, u.port or 80, timeout=TIMEOUT)
    try:
        conn.request("PUT", u.path, body=body, headers=headers)
        resp = conn.getresponse()
        return resp.status, resp.read().decode("utf-8", "replace")
    finally:
        conn.close()


def main(argv=None):
    argv = sys.argv if argv is None else argv
    base = (argv[1] if len(argv) > 1 else "http://tika:9998").rstrip("/")
    # Plain dictionary words plus a fresh number (no 0/1, which OCR confuses with O/l), so a reply can't
    # come from a cache or a fixture.
    number = "".join(random.choice("23456789") for _ in range(5))
    words = ["Harbor", "Lantern", "Meadow", number]
    png, pdf = render(words)

    checks = []
    for name, path, body, headers, as_json in (
        ("png /tika", "/tika", png, {"Content-Type": "image/png", "Accept": "text/plain"}, False),
        ("png /tika/text", "/tika/text", png, {}, True),
        ("pdf /tika/text", "/tika/text", pdf, {}, True),
    ):
        result = {"check": name, "ok": False}
        try:
            status, text = put(base + path, body, headers)
            if status == 200 and as_json:
                meta = json.loads(text)
                text = meta.get("X-TIKA:content", "")
                result["parsed_by"] = [p.rsplit(".", 1)[-1] for p in meta.get("X-TIKA:Parsed-By", [])]
            missing = [w for w in words if w.lower() not in text.lower()]
            result.update(
                status=status,
                missing=missing,
                ok=(status == 200 and not missing),
                text=" ".join(text.split())[:200],
            )
        except (OSError, ValueError, HTTPException, AttributeError) as exc:  # network, timeout, HTTP, JSON
            result["error"] = f"{type(exc).__name__}: {exc}"[:300]
        checks.append(result)

    ok = all(c["ok"] for c in checks)
    print(json.dumps({"ok": ok, "words": words, "checks": checks}))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
