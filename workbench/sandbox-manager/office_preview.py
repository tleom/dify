"""Convert one supplied Word document in a disposable, offline container."""
import base64
import json
import io
from pathlib import Path
import subprocess
import sys
import tempfile
from xml.dom import minidom
from xml.parsers.expat import ExpatError
import zipfile

MAX_BYTES = 20 * 1024 * 1024
SUFFIXES = {".doc", ".docx", ".docm", ".dotx", ".dotm", ".odt", ".rtf"}


def preserve_exact_spacing(data, suffix):
    """Word's exact line spacing overrides the grid; Writer otherwise snaps it.

    Only the disposable conversion input changes. See OOXML docGrid/linePitch:
    https://learn.microsoft.com/dotnet/api/documentformat.openxml.wordprocessing.docgrid.linepitch
    """
    if suffix not in {".docx", ".docm", ".dotx", ".dotm"}:
        return data
    namespace = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as source, zipfile.ZipFile(output, "w") as target:
        if len(source.infolist()) > 10000 or sum(item.file_size for item in source.infolist()) > 100 * 1024 * 1024:
            raise ValueError("文档解压后超过预览范围")
        for item in source.infolist():
            content = source.read(item)
            if item.filename.startswith("word/") and item.filename.endswith(".xml") and b"spacing" in content:
                if len(content) > MAX_BYTES or b"<!DOCTYPE" in content.upper():
                    raise ValueError("文档 XML 超过预览范围或包含不支持的声明")
                document = minidom.parseString(content)
                for spacing in document.getElementsByTagNameNS(namespace, "spacing"):
                    parent = spacing.parentNode
                    if spacing.getAttributeNS(namespace, "lineRule") != "exact" or parent.localName != "pPr":
                        continue
                    snaps = [node for node in parent.childNodes if node.nodeType == node.ELEMENT_NODE and node.namespaceURI == namespace and node.localName == "snapToGrid"]
                    snap = snaps[0] if snaps else document.createElementNS(namespace, (spacing.prefix + ":" if spacing.prefix else "") + "snapToGrid")
                    snap.setAttributeNS(namespace, (spacing.prefix + ":" if spacing.prefix else "") + "val", "0")
                    if not snaps:
                        parent.insertBefore(snap, spacing)
                content = document.toxml(encoding="utf-8")
                document.unlink()
            target.writestr(item, content)
    return output.getvalue()


def preview(payload):
    suffix = Path(payload["name"]).suffix.lower()
    if suffix not in SUFFIXES:
        raise ValueError("Unsupported document format")
    data = base64.b64decode(payload["data"], validate=True)
    if not data or len(data) > MAX_BYTES:
        raise ValueError("Only documents up to 20 MiB are supported")
    with tempfile.TemporaryDirectory(prefix="word-preview-") as directory:
        source = Path(directory) / ("source" + suffix)
        target = Path(directory) / "preview.pdf"
        source.write_bytes(preserve_exact_spacing(data, suffix))
        result = subprocess.run(
            ["/usr/bin/python3", "-I", "/opt/office/uno_worker.py", str(source), str(target), "convert"],
            capture_output=True, text=True, timeout=45,
        )
        if result.returncode or not target.is_file():
            raise ValueError("文档排版失败，请检查文件是否损坏或需要密码")
        if target.stat().st_size > MAX_BYTES:
            raise ValueError("分页预览超过 20 MiB，请下载原文件查看")
        content = target.read_bytes()
        if not content.startswith(b"%PDF-"):
            raise ValueError("文档未生成有效的分页预览")
        return {"data": base64.b64encode(content).decode(), "name": "preview.pdf"}


if __name__ == "__main__":
    try:
        print(json.dumps(preview(json.load(sys.stdin))))
    except (ValueError, OSError, zipfile.BadZipFile, ExpatError, subprocess.TimeoutExpired) as error:
        print(json.dumps({"error": str(error) if not isinstance(error, subprocess.TimeoutExpired) else "文档排版超时，请下载原文件查看"}))
        sys.exit(2)
