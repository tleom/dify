"""Publish an owned file to the current task's sidebar after verifying its metadata."""

from hashlib import sha256

from pydantic import Field
from werkzeug.exceptions import BadRequest, Conflict, Forbidden

from core.db.session_factory import session_factory
from services.workbench.event_log import append_locked, notify
from services.workbench.file_links import AgentFileLinksPayload, agent_lookup
from services.workbench.recovery import locked_run


class AgentFilePreviewPayload(AgentFileLinksPayload):
    backend_run_id: str = Field(min_length=1, max_length=64)
    request_key: str = Field(min_length=1, max_length=128)


def open_preview(payload: AgentFilePreviewPayload) -> dict:
    # File I/O stays outside the transaction. The execution ticket is rechecked
    # under the run lock so a stopped/replaced Agent cannot open a sidebar later.
    result = agent_lookup(
        AgentFileLinksPayload.model_validate(payload.model_dump(exclude={"backend_run_id", "request_key"}))
    )
    entries = result["entries"]
    if len(entries) != 1 or entries[0].get("kind") != "file":
        raise BadRequest("请指定一个可预览的文件")
    entry = entries[0]
    if not entry.get("downloadable", True):
        raise BadRequest("文件超出预览范围，请拆分后重试")
    # These URLs remain available through the ordinary file toolbar, not in the
    # model observation or UI event used for automatic delivery.
    file = {key: value for key, value in entry.items() if key not in {"download_url", "preview_url"}}
    with session_factory.get_session_maker().begin() as session:
        chat, run = locked_run(session, payload.tenant_id, payload.account_id, payload.workbench_run_id)
        if chat.app_id != payload.app_id or run.status != "running" or run.backend_run_id != payload.backend_run_id:
            raise Forbidden("当前执行已结束或身份不匹配")
        stored = append_locked(
            session,
            run,
            {
                "event": "workbench_preview",
                "file": file,
                "backend_run_id": payload.backend_run_id,
                "source_event_id": "preview:" + sha256(payload.request_key.encode()).hexdigest(),
            },
        )
        if not stored:
            raise Conflict("当前任务已结束")
        stored_file = stored.get("file")
        if not isinstance(stored_file, dict) or stored_file.get("path") != file["path"]:
            raise Conflict("预览请求编号已用于另一个文件")
    notify(payload.workbench_run_id, stored)
    return {"accepted": True, "file": stored_file}
