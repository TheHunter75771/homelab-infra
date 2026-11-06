import io
import os
import tempfile
import unittest
import zipfile
from pathlib import Path

from voice_profile.docx import Caps, DocxRefused, parse_document, read_docx

from .fakes import MC, W, document, docx_bytes, para, run

CAPS = Caps(max_docx_bytes=2 * 1024 * 1024, max_zip_entries=50, max_uncompressed_bytes=8 * 1024 * 1024,
            max_xml_bytes=4 * 1024 * 1024, max_ratio=100)


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def write(self, data: bytes, name: str = "s.docx") -> Path:
        path = self.dir / name
        path.write_bytes(data)
        return path

    def read(self, data: bytes, caps: Caps = CAPS):
        return read_docx(self.write(data), caps)

    def refused(self, data: bytes, fragment: str, caps: Caps = CAPS) -> None:
        with self.assertRaises(DocxRefused) as ctx:
            self.read(data, caps)
        self.assertIn(fragment, str(ctx.exception))

    @staticmethod
    def raw_zip(entries: list[tuple[str | zipfile.ZipInfo, bytes]], compression=zipfile.ZIP_DEFLATED) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression) as zf:
            for name, data in entries:
                zf.writestr(name, data)
        return buf.getvalue()


class ExtractTests(Base):
    def text(self, body: str) -> str:
        return "\n".join(p.text for p in self.read(docx_bytes(document(body))))

    def test_plain_paragraphs_runs_tabs_and_breaks(self):
        body = (para("First line") + f"<w:p>{run('Split ')}{run('across runs')}<w:r><w:tab/></w:r>{run('tabbed')}</w:p>"
                + para("one\ntwo"))
        paras = self.read(docx_bytes(document(body)))
        self.assertEqual([p.text for p in paras], ["First line", "Split across runs tabbed", "one\ntwo"])

    def test_tracked_deletions_and_moves_are_skipped_insertions_kept(self):
        body = (f"<w:p>{run('Kept ')}<w:del w:id='1' w:author='x'><w:r><w:delText>DELETEDCANARY</w:delText></w:r></w:del>"
                f"<w:ins w:id='2' w:author='x'>{run('inserted')}</w:ins>"
                f"<w:moveFrom w:id='3' w:author='x'>{run('MOVEDCANARY')}</w:moveFrom>"
                f"<w:del w:id='4' w:author='x'>{run('DELETEDRUNCANARY')}</w:del></w:p>")
        self.assertEqual(self.text(body), "Kept inserted")

    def test_comments_headers_footers_are_never_read(self):
        body = (f"<w:p><w:commentRangeStart w:id='0'/>{run('Body text')}<w:commentRangeEnd w:id='0'/>"
                f"<w:r><w:commentReference w:id='0'/></w:r></w:p>")
        text = self.text(body)
        self.assertEqual(text, "Body text")
        for canary in ("COMMENTCANARY", "HEADERCANARY", "FOOTERCANARY"):
            self.assertNotIn(canary, text)

    def test_field_codes_textboxes_fallback_hidden_and_drawings_skipped(self):
        body = (f"<w:p>{run('Visible')}"
                f"<w:r><w:fldChar w:fldCharType='begin'/></w:r><w:r><w:instrText>HYPERLINK FIELDCANARY</w:instrText></w:r>"
                f"<w:r><w:fldChar w:fldCharType='separate'/></w:r>{run(' result')}<w:r><w:fldChar w:fldCharType='end'/></w:r>"
                f"{run('HIDDENCANARY', hidden=True)}"
                f"<w:r><mc:AlternateContent><mc:Choice Requires='wps'><w:drawing><wps:txbx><w:txbxContent>"
                f"{para('TEXTBOXCANARY')}</w:txbxContent></wps:txbx></w:drawing></mc:Choice>"
                f"<mc:Fallback><w:pict>{para('FALLBACKCANARY')}</w:pict></mc:Fallback></mc:AlternateContent></w:r></w:p>")
        self.assertEqual(self.text(body), "Visible result")

    def test_cover_page_and_bibliography_content_controls_skipped(self):
        def sdt(gallery: str, text: str) -> str:
            return (f"<w:sdt><w:sdtPr><w:docPartObj><w:docPartGallery w:val='{gallery}'/></w:docPartObj></w:sdtPr>"
                    f"<w:sdtContent>{para(text)}</w:sdtContent></w:sdt>")
        body = (sdt("Cover Pages", "SDTCOVERCANARY") + sdt("Bibliographies", "BIBCANARY")
                + sdt("Quick Parts", "kept part") + para("Body"))
        self.assertEqual(self.text(body), "kept part\nBody")

    def test_paragraph_properties(self):
        body = (para("Heading", style="Heading1") + para("Quoted", left=1440) + para("Ref", left=720, hanging=720)
                + para("Item", numbered=True, left=720, hanging=360) + para("Indented", first_line=720))
        paras = self.read(docx_bytes(document(body)))
        self.assertEqual(paras[0].style, "Heading1")
        self.assertEqual((paras[1].left, paras[1].hanging), (1440, 0))
        self.assertEqual((paras[2].left, paras[2].hanging), (720, 720))
        self.assertTrue(paras[3].is_list)
        self.assertEqual(paras[4].first_line, 720)
        self.assertFalse(paras[4].is_list)

    def test_strict_ooxml_namespace(self):
        xml = document(para("Strict")).replace(W, "http://purl.oclc.org/ooxml/wordprocessingml/main")
        self.assertEqual([p.text for p in parse_document(xml.encode())], ["Strict"])

    def test_declared_encoding_is_ignored_utf8_is_used(self):
        xml = document(para("Café"), prolog='<?xml version="1.0" encoding="ISO-8859-1"?>')
        self.assertEqual([p.text for p in parse_document(xml.encode("utf-8"))], ["Café"])


class HostileTests(Base):
    def test_zip_bomb_ratio_refused(self):
        bomb = document(para("x")) + " " * (3 * 1024 * 1024)
        self.refused(docx_bytes(bomb), "zip bomb")

    def test_one_bombed_entry_beside_incompressible_data_refused(self):
        # The total ratio stays low (random bytes); the per-entry ratio still catches the bomb.
        data = docx_bytes(document(para("x")), extra={"word/media/noise.bin": os.urandom(1024 * 1024),
                                                       "word/bomb.xml": " " * (3 * 1024 * 1024)})
        self.refused(data, "'word/bomb.xml'): zip bomb")

    def test_declaration_scan_runs_before_any_parser_exists(self):
        from unittest import mock

        from voice_profile import docx as docxmod
        xml = document(para("x"), prolog='<?xml version="1.0"?><!DOCTYPE w:document>')
        with (mock.patch.object(docxmod.expat, "ParserCreate", side_effect=AssertionError("parser created")),
              self.assertRaises(DocxRefused)):
            parse_document(xml.encode())

    def test_declared_size_cap(self):
        caps = Caps(2 * 1024 * 1024, 50, 64 * 1024, 4 * 1024 * 1024, 10_000)
        data = docx_bytes(document(para("x")), extra={"word/media/blob.bin": os.urandom(80 * 1024)})
        self.refused(data, "declared uncompressed size", caps)

    def test_xml_inflating_past_its_cap_refused(self):
        caps = Caps(2 * 1024 * 1024, 50, 64 * 1024 * 1024, 64 * 1024, 10_000)
        self.refused(docx_bytes(document(para("x" * 100_000))), "larger than 65536", caps)

    def test_lying_size_header_is_caught_while_reading(self):
        data = bytearray(docx_bytes(document(para("y" * 200_000)), compression=zipfile.ZIP_STORED))
        # Shrink the declared size of word/document.xml in its central directory record.
        zf = zipfile.ZipFile(io.BytesIO(bytes(data)))
        info = zf.getinfo("word/document.xml")
        caps = Caps(2 * 1024 * 1024, 50, 64 * 1024 * 1024, 64 * 1024, 10_000)
        pos = 0
        while True:
            pos = data.find(b"PK\x01\x02", pos)
            if pos < 0:
                break
            name_len = int.from_bytes(data[pos + 28:pos + 30], "little")
            if data[pos + 46:pos + 46 + name_len] == b"word/document.xml":
                data[pos + 24:pos + 28] = (1000).to_bytes(4, "little")  # uncompressed size
            pos += 4
        self.assertGreater(info.file_size, caps.max_xml_bytes)
        with self.assertRaises(DocxRefused):
            self.read(bytes(data), caps)

    def test_doctype_refused(self):
        xml = document(para("x"), prolog='<?xml version="1.0"?><!DOCTYPE w:document [<!ELEMENT a ANY>]>')
        self.refused(docx_bytes(xml), "DOCTYPE or ENTITY")

    def test_entity_refused_billion_laughs(self):
        xml = ('<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;">]>'
               + document(para("&lol2;"), prolog=""))
        self.refused(docx_bytes(xml), "DOCTYPE or ENTITY")

    def test_entity_declaration_is_refused_by_expat_handler_too(self):
        # Defence in depth: even past the text scan, the parser refuses any DTD.
        from voice_profile import docx as docxmod
        saved = docxmod._DECL_RE
        docxmod._DECL_RE = __import__("re").compile(b"(?!)")
        self.addCleanup(setattr, docxmod, "_DECL_RE", saved)
        xml = '<?xml version="1.0"?><!DOCTYPE d [<!ENTITY e SYSTEM "file:///etc/passwd">]><d>&e;</d>'
        with self.assertRaises(DocxRefused):
            parse_document(xml.encode())

    def test_external_entity_refused(self):
        xml = ('<?xml version="1.0"?><!DOCTYPE d SYSTEM "http://example.invalid/x.dtd">'
               + document(para("x"), prolog=""))
        self.refused(docx_bytes(xml), "DOCTYPE or ENTITY")

    def test_path_traversal_names_refused(self):
        for name in ("../evil.xml", "/abs/evil.xml", "word\\..\\evil.xml", "C:/evil.xml", "word/./x.xml",
                     "a/../../b.xml"):
            with self.subTest(name=name):
                self.refused(docx_bytes(document(para("x")), extra={name: "x"}), "unsafe entry name")

    def test_duplicate_names_refused(self):
        data = self.raw_zip([("word/document.xml", document(para("one")).encode()),
                             ("WORD/document.xml", document(para("two")).encode())])
        self.refused(data, "duplicate entry")

    def test_macro_part_refused(self):
        self.refused(docx_bytes(document(para("x")), extra={"word/vbaProject.bin": b"\x00macro"}), "macros")

    def test_encrypted_entry_refused(self):
        data = bytearray(self.raw_zip([("word/document.xml", document(para("x")).encode()),
                                       ("word/secret.xml", b"x")]))
        pos = data.find(b"PK\x01\x02")
        while pos >= 0:  # set the "encrypted" flag on word/secret.xml in the central directory
            name_len = int.from_bytes(data[pos + 28:pos + 30], "little")
            if data[pos + 46:pos + 46 + name_len] == b"word/secret.xml":
                data[pos + 8] |= 0x1
            pos = data.find(b"PK\x01\x02", pos + 4)
        self.refused(bytes(data), "encrypted")

    def test_too_many_entries(self):
        extra = {f"word/media/{i}.xml": "x" for i in range(60)}
        self.refused(docx_bytes(document(para("x")), extra=extra), "more than 50 zip entries")

    def test_unsupported_compression_refused(self):
        data = self.raw_zip([("word/document.xml", document(para("x")).encode())], zipfile.ZIP_BZIP2)
        self.refused(data, "unsupported compression")

    def test_not_a_zip_and_no_document(self):
        self.refused(b"PK\x03\x04 not really a zip", "not a valid .docx")
        self.refused(self.raw_zip([("word/other.xml", b"<x/>")]), "no word/document.xml")

    def test_oversized_file(self):
        caps = Caps(1024, 50, 64 * 1024 * 1024, 64 * 1024, 10_000)
        self.refused(docx_bytes(document(para("x" * 5000))), "larger than 1024 bytes", caps)

    def test_symlink_and_non_regular_refused(self):
        target = self.write(docx_bytes(document(para("x"))), "real.docx")
        link = self.dir / "link.docx"
        link.symlink_to(target)
        with self.assertRaises(DocxRefused):
            read_docx(link, CAPS)
        os.mkfifo(self.dir / "fifo.docx")
        with self.assertRaises(DocxRefused) as ctx:
            read_docx(self.dir / "fifo.docx", CAPS)
        self.assertIn("not a regular file", str(ctx.exception))

    def test_not_utf8_refused(self):
        xml = document(para("x"), prolog='<?xml version="1.0" encoding="UTF-16"?>').encode("utf-16")
        self.refused(docx_bytes(xml), "not UTF-8")
        self.refused(docx_bytes(document(para("x")).encode("utf-8") + b"\xff"), "not UTF-8")

    def test_malformed_xml_refused(self):
        self.refused(docx_bytes("<w:document xmlns:w='" + W + "'><w:body><w:p>"), "not well-formed")
        self.refused(docx_bytes("<x:document><x:p/></x:document>"), "not well-formed")  # unbound prefix

    def test_mc_namespace_only_fallback_is_skipped(self):
        body = (f"<w:p><mc:AlternateContent><mc:Choice Requires='w14'>{run('choice')}</mc:Choice>"
                f"<mc:Fallback>{run('FALLBACKCANARY')}</mc:Fallback></mc:AlternateContent></w:p>")
        self.assertEqual([p.text for p in self.read(docx_bytes(document(body)))], ["choice"])
        self.assertIn("markup-compatibility", MC)


if __name__ == "__main__":
    unittest.main()
