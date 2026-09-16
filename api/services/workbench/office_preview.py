"""Owned Word pagination; conversion receives only document bytes, in an offline sandbox."""

import base64
import hashlib
from pathlib import PurePosixPath

from werkzeug.exceptions import BadRequest

from extensions.ext_redis import redis_client
from services.workbench.files import ensure_workspace, manager, operate

SUFFIXES = {".doc", ".docx", ".docm", ".dotx", ".dotm", ".odt", ".rtf"}
MAX_BYTES = 20 * 1024 * 1024


def preview(tenant_id: str, account_id: str, *, path: str | None = None, name: str = "", data: str = "") -> bytes:
    if path is not None:
        result = operate(tenant_id, account_id, "get", path)
        if result.get("kind") == "directory":
            raise BadRequest("请选择单个 Word 文件")
        name, data = result["name"], result["data"]
    if PurePosixPath(name).suffix.lower() not in SUFFIXES:
        raise BadRequest("该格式不支持 Word 分页预览")
    try:
        content = base64.b64decode(data, validate=True)
    except ValueError as error:
        raise BadRequest("文档内容无效") from error
    if not content or len(content) > MAX_BYTES:
        raise BadRequest("单个文档最大 20 MiB")
    digest = hashlib.sha256(content).hexdigest()
    key = f"workbench:word-preview:v1:{tenant_id}:{account_id}:{digest}"
    cached = redis_client.get(key)
    if cached:
        return cached
    with redis_client.lock(key + ":lock", timeout=65, blocking_timeout=55):
        cached = redis_client.get(key)
        if cached:
            return cached
        identifier = ensure_workspace(tenant_id, account_id)
        output = manager(identifier, "office-preview", {"name": name, "data": data}, timeout=60)
        pdf = base64.b64decode(output["data"], validate=True)
        if len(pdf) > MAX_BYTES or not pdf.startswith(b"%PDF-"):
            raise BadRequest("文档排版服务返回了无效内容")
        # Large results remain usable without occupying the shared cache.
        if len(pdf) <= 2 * 1024 * 1024:
            redis_client.setex(key, 600, pdf)
        return pdf
