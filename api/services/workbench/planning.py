"""Authorize read-only investigations against the current owned execution."""

from pydantic import BaseModel, ConfigDict, Field
from werkzeug.exceptions import Conflict, Forbidden

from configs import dify_config
from core.db.session_factory import session_factory
from services.workbench import control
from services.workbench.directories import chat_directory
from services.workbench.files import ensure_workspace, manager
from services.workbench.recovery import locked_run


class AgentPlanInspectPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: str
    account_id: str
    app_id: str
    workbench_run_id: str
    backend_run_id: str
    request_key: str = Field(min_length=1, max_length=128)
    script: str = Field(min_length=1, max_length=100000)
    timeout: int = Field(default=30, ge=1, le=60)
    preview_paths: list[str] = Field(default_factory=list, max_length=4)


def _context(payload: AgentPlanInspectPayload) -> str:
    if not dify_config.WORKBENCH_ENABLED:
        raise Forbidden()
    with session_factory.get_session_maker().begin() as session:
        chat, run = locked_run(session, payload.tenant_id, payload.account_id, payload.workbench_run_id)
        if chat.app_id != payload.app_id or run.status != "running" or run.backend_run_id != payload.backend_run_id:
            raise Forbidden("当前执行已结束或身份不匹配")
        if not control.load(session, chat).plan.active:
            raise Conflict("计划阶段已结束，请使用当前模式的工具")
        return chat_directory(session, chat)


def inspect(payload: AgentPlanInspectPayload):
    directory = _context(payload)
    identifier = ensure_workspace(payload.tenant_id, payload.account_id)
    binding = directory.removeprefix("conversations/")
    ticket = manager(identifier, "plan-admission", {"binding_id": binding})["ticket"]
    # Workspace startup may wait behind another conversation. Recheck the
    # execution before launching; never hold a DB transaction during Docker IO.
    if _context(payload) != directory:
        raise Conflict("会话目录已改变，请重新调查")
    return manager(
        identifier,
        "plan-inspect",
        {
            "binding_id": binding,
            "admission_ticket": ticket,
            "execution_id": payload.backend_run_id,
            "request_key": payload.request_key,
            "script": payload.script,
            "timeout": payload.timeout,
            "preview_paths": payload.preview_paths,
        },
        timeout=95,
    )
