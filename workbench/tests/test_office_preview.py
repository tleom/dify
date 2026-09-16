"""Word fixed spacing survives a document grid without changing the uploaded file."""

import importlib.util
import io
from pathlib import Path
import zipfile
from xml.etree import ElementTree

import pytest

spec = importlib.util.spec_from_file_location("office_preview", Path(__file__).parents[1] / "sandbox-manager/office_preview.py")
preview = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preview)
NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def document(xml):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("word/document.xml", xml)
        archive.writestr("word/media/image.png", b"original-image")
    return stream.getvalue()


def test_exact_spacing_overrides_grid_only_in_conversion_copy():
    xml = f'''<w:document xmlns:w="{NS}"><w:body>
      <w:p><w:pPr><w:spacing w:lineRule="exact" w:line="440"/></w:pPr><w:r><w:rPr><w:b/><w:rFonts w:eastAsia="宋体"/></w:rPr><w:t>正文</w:t><w:br w:type="page"/></w:r></w:p>
      <w:p><w:pPr><w:spacing w:lineRule="auto" w:line="240"/></w:pPr></w:p>
      <w:sectPr><w:docGrid w:type="lines" w:linePitch="312"/></w:sectPr>
    </w:body></w:document>'''
    original = document(xml)
    output = preview.preserve_exact_spacing(original, ".docx")
    with zipfile.ZipFile(io.BytesIO(original)) as uploaded, zipfile.ZipFile(io.BytesIO(output)) as converted:
        assert uploaded.read("word/document.xml").decode() == xml
        tree = ElementTree.fromstring(converted.read("word/document.xml"))
        paragraphs = tree.findall(f".//{{{NS}}}pPr")
        assert paragraphs[0].find(f"{{{NS}}}snapToGrid").get(f"{{{NS}}}val") == "0"
        assert paragraphs[1].find(f"{{{NS}}}snapToGrid") is None
        assert tree.find(f".//{{{NS}}}spacing").get(f"{{{NS}}}line") == "440"
        assert tree.find(f".//{{{NS}}}br").get(f"{{{NS}}}type") == "page"
        assert tree.find(f".//{{{NS}}}rFonts").get(f"{{{NS}}}eastAsia") == "宋体"
        assert converted.read("word/media/image.png") == b"original-image"


def test_unsafe_xml_is_rejected_before_conversion():
    with pytest.raises(ValueError, match="声明"):
        preview.preserve_exact_spacing(document('<!DOCTYPE spacing><spacing/>'), ".docx")
