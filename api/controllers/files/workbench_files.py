"""Read one signed workbench file without exposing account API credentials."""

import base64
import mimetypes
from typing import Literal
from urllib.parse import quote

from flask import Response, request
from flask_restx import Resource
from pydantic import BaseModel

from controllers.common.schema import query_params_from_model
from controllers.files import files_ns
from services.workbench.file_links import read_signed


def file_response(result: dict, *, preview: bool, fallback_name: str = "file") -> Response:
    name = result.get("name") or fallback_name
    mimetype = mimetypes.guess_type(name)[0] or "application/octet-stream"
    if result.get("kind") == "directory":
        mimetype = "application/zip"
    response = Response(base64.b64decode(result["data"], validate=True), mimetype=mimetype)
    disposition = "inline" if preview else "attachment"
    response.headers["Content-Disposition"] = f"{disposition}; filename*=UTF-8''{quote(name, safe='')}"
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = "sandbox allow-scripts allow-downloads"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    return response


class WorkbenchFileContentQuery(BaseModel):
    mode: Literal["preview", "download"] = "download"


@files_ns.route("/workbench/<string:token>/<string:filename>")
class WorkbenchFileContent(Resource):
    @files_ns.doc(params=query_params_from_model(WorkbenchFileContentQuery))
    @files_ns.response(200, "File bytes; inline preview or attachment download")
    def get(self, token: str, filename: str):
        mode = WorkbenchFileContentQuery.model_validate(request.args.to_dict()).mode
        result = read_signed(token)
        # Documents may contain scripts. An opaque origin keeps them away from
        # the file service's cookies and authenticated API; raster images work inline.
        return file_response(result, preview=mode == "preview", fallback_name=filename)


@files_ns.route("/s/<string:token>")
class WorkbenchSharedFile(Resource):
    @files_ns.response(200, "Public preview for a revocable file capability")
    def get(self, token: str):
        from services.workbench.file_shares import read_share

        return file_response(read_share(token), preview=True)
