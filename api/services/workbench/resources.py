"""Per-account memory/skills and immutable administrator resource materialization."""

import base64
import io
import json
import re
import zipfile

import yaml
from pydantic import BaseModel, ConfigDict, Field
from werkzeug.exceptions import BadRequest, Conflict, Forbidden

from core.db.session_factory import session_factory
from extensions.ext_redis import redis_client
from services.workbench.files import ensure_workspace, manager

MAX_PACKAGE = 20 * 1024 * 1024


class AgentMemoryPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: str
    account_id: str
    app_id: str
    workbench_run_id: str
    backend_run_id: str
    content: str = Field(max_length=65536)
    version: str | None = Field(max_length=128)


class ResourceFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=512)
    data: str = Field(max_length=28_000_000)


class ResourceMutation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: str
    name: str | None = Field(default=None, max_length=64)
    content: str | None = Field(default=None, max_length=65536)
    enabled: bool | None = None
    pinned: bool | None = None
    version: str | None = Field(default=None, max_length=128)
    archive: str | None = Field(default=None, max_length=28_000_000)
    files: list[ResourceFile] = Field(default_factory=list, max_length=200)


def skill_metadata(content, *, personal=False):
    if not isinstance(content, str) or len(content.encode("utf-8")) > 65536:
        raise ValueError("SKILL.md 不能超过 64 KiB")
    lines = content.lstrip("\ufeff").splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("SKILL.md 需要包含 name 和 description 的 YAML 文件头")
    end = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
    if end is None:
        raise ValueError("SKILL.md 的 YAML 文件头不完整")
    data = yaml.safe_load("\n".join(lines[1:end]))
    if not isinstance(data, dict):
        raise ValueError("技能文件头必须是键值对象")
    name, description = data.get("name"), data.get("description")
    if not isinstance(name, str) or not name.strip() or len(name) > 64:
        raise ValueError("技能 name 需为 1–64 个字符")
    if personal and not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?", name):
        raise ValueError("个人技能 name 需为小写字母、数字或连字符")
    if not isinstance(description, str) or not description.strip() or len(description) > 2000:
        raise ValueError("技能 description 需为 1–2000 个字符")
    return {"name": name, "description": description.strip()}


def _import_name(payload):
    try:
        if payload.get("archive"):
            raw = base64.b64decode(payload["archive"], validate=True)
            if len(raw) > MAX_PACKAGE:
                raise ValueError("技能包超过 20 MiB")
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                infos = [info for info in archive.infolist() if not info.is_dir()]
                if len(infos) > 200 or sum(info.file_size for info in infos) > MAX_PACKAGE:
                    raise ValueError("技能包最多 200 个文件、20 MiB")
                candidates = [
                    info
                    for info in infos
                    if info.filename == "SKILL.md"
                    or (info.filename.count("/") == 1 and info.filename.endswith("/SKILL.md"))
                ]
                if len(candidates) != 1 or candidates[0].file_size > 65536:
                    raise ValueError("技能包需要包含一个不超过 64 KiB 的 SKILL.md")
                content = archive.read(candidates[0]).decode("utf-8-sig")
        else:
            entries = payload.get("files", [])
            if sum(len(item["data"]) for item in entries) > 28_000_000:
                raise ValueError("技能包超过 20 MiB")
            file_candidates = [
                item
                for item in entries
                if item["path"] == "SKILL.md" or (item["path"].count("/") == 1 and item["path"].endswith("/SKILL.md"))
            ]
            if len(file_candidates) != 1:
                raise ValueError("技能包需要包含一个 SKILL.md")
            content = base64.b64decode(file_candidates[0]["data"], validate=True).decode("utf-8-sig")
        return skill_metadata(content, personal=True)["name"]
    except (ValueError, UnicodeError, zipfile.BadZipFile, yaml.YAMLError) as error:
        raise BadRequest(str(error)) from error


def personal_snapshot(identifier):
    result = manager(identifier, "personal-resources", {"operation": "list"})
    for skill in result["skills"]:
        skill["scope"], skill["readonly"] = "personal", False
        try:
            skill.update(skill_metadata(skill["content"], personal=True))
            if skill["name"] != skill["id"]:
                raise ValueError("技能目录名与 SKILL.md 的 name 不一致")
        except (ValueError, yaml.YAMLError) as error:
            skill.update(name=skill["id"], description="", enabled=False, error=str(error))
    return result


def personal_skill_catalog(tenant_id: str, account_id: str) -> list[dict[str, str]]:
    """List usable skills from this account's workspace with distinct public IDs."""
    snapshot = personal_snapshot(ensure_workspace(tenant_id, account_id))
    return [
        {"id": f"personal:{item['id']}", "name": item["name"], "description": item["description"], "scope": "personal"}
        for item in snapshot["skills"]
        if item.get("enabled") and not item.get("error")
    ]


def mutate(tenant_id, account_id, payload):
    identifier = ensure_workspace(tenant_id, account_id)
    if payload["operation"] == "skill_inspect":
        name = _import_name(payload)
        previous = next((item for item in personal_snapshot(identifier)["skills"] if item["id"] == name), None)
        return {"name": name, "version": previous["version"] if previous else None}
    if payload["operation"] == "skill_import":
        payload["name"] = _import_name(payload)
    elif payload["operation"] == "memory_update":
        if payload.get("content") is None:
            raise BadRequest("缺少记忆内容")
    elif payload["operation"] == "skill_toggle":
        if not payload.get("name") or not isinstance(payload.get("enabled"), bool):
            raise BadRequest("缺少技能名称或开关状态")
    elif payload["operation"] == "skill_pin":
        if not payload.get("name") or not isinstance(payload.get("pinned"), bool):
            raise BadRequest("缺少技能名称或置顶状态")
    elif payload["operation"] in {"skill_update", "skill_uninstall"}:
        if not payload.get("name") or not payload.get("version"):
            raise BadRequest("缺少技能名称或版本，请刷新后重试")
        if payload["operation"] == "skill_update":
            try:
                metadata = skill_metadata(payload.get("content"), personal=True)
                if metadata["name"] != payload["name"]:
                    raise ValueError("编辑时不能修改技能 name，请保留原名称")
            except (ValueError, yaml.YAMLError) as error:
                raise BadRequest(str(error)) from error
    else:
        raise BadRequest("不支持的个人资源操作")
    with redis_client.lock(f"workbench:resources:{identifier}", timeout=90, blocking_timeout=30):
        result = manager(identifier, "personal-resources", payload)
    if result.get("conflict"):
        raise Conflict("内容已改变，请刷新后重新保存")
    return result


def _global_resources(tenant_id, identifier, soul):
    from services.agent_config_service import AgentConfigService

    loader = AgentConfigService()
    packages = []
    for kind, key in (("skill", "config_skills"), ("file", "config_files")):
        for item in soul.get(key, []):
            if item.get("is_missing") or not item.get("file_id"):
                continue
            raw, _ = loader._load_tool_file_bytes(tenant_id=tenant_id, file_id=item["file_id"])
            if len(raw) > MAX_PACKAGE:
                raise BadRequest("管理员资源超过单包 20 MiB 限制")
            package = {"name": item["name"], "kind": kind, "description": item.get("description", "")}
            if kind == "skill":
                package["archive"] = base64.b64encode(raw).decode()
            else:
                filename = item["name"].replace("\\", "/").split("/")[-1]
                package["files"] = [{"path": filename, "data": base64.b64encode(raw).decode()}]
            packages.append(package)
    materialized = manager(
        identifier, "global-resources", {"operation": "global_install", "packages": packages}, timeout=180
    )
    skills: list[dict[str, object]] = []
    files: list[dict[str, object]] = []
    for item, source in zip(materialized["skills"], packages, strict=True):
        item.update(id=source["name"], scope="global", readonly=True, enabled=True, description=source["description"])
        (skills if source["kind"] == "skill" else files).append(item)
    return {"skills": skills, "files": files}


def listing(tenant_id, account_id):
    from services.workbench.service import catalog, template

    identifier = ensure_workspace(tenant_id, account_id)
    personal = personal_snapshot(identifier)
    global_items = _global_resources(tenant_id, identifier, template(tenant_id, account_id)["soul"])
    visible = catalog(tenant_id, account_id, include_personal=False)
    return {**personal, "global": {**global_items, "tools": visible["tools"], "knowledge": visible["knowledge"]}}


def _agent_soul(payload):
    """Fence resource access to this owner's current execution before external IO."""
    from services.workbench.recovery import locked_run

    with session_factory.get_session_maker().begin() as session:
        chat, run = locked_run(session, payload.tenant_id, payload.account_id, payload.workbench_run_id)
        if chat.app_id != payload.app_id or run.status != "running" or run.backend_run_id != payload.backend_run_id:
            raise Forbidden()
        return json.loads(run.payload)["effective_soul"]


def agent_snapshot(payload, *, initialize=False):
    """Resolve only the currently leased run; never accept a client workspace ID."""
    soul = _agent_soul(payload)
    identifier = ensure_workspace(payload.tenant_id, payload.account_id)
    result = personal_snapshot(identifier)
    if initialize:
        result["global"] = _global_resources(payload.tenant_id, identifier, soul)
    return result


def agent_memory_update(payload: AgentMemoryPayload):
    """Use the same account lock and file CAS as editor writes, outside DB locks."""
    _agent_soul(payload)
    if len(payload.content.encode("utf-8")) > 65536:
        raise BadRequest("记忆内容不能超过 64 KiB，请先合并重复内容、压缩过时记录")
    identifier = ensure_workspace(payload.tenant_id, payload.account_id)
    with redis_client.lock(f"workbench:resources:{identifier}", timeout=90, blocking_timeout=30):
        # Waiting for another conversation must not authorize a superseded run.
        _agent_soul(payload)
        current = personal_snapshot(identifier)
        if any("memory.md" in warning for warning in current.get("warnings", [])):
            raise Conflict("现有记忆无法完整读取，请先修复 memory.md，不能用不完整内容覆盖")
        if current["memory"]["content"] == payload.content:
            # A response may be lost after a successful write. An exact no-op is
            # safe even with the old revision; never adopt a newer revision to write.
            return current["memory"]
        result = manager(
            identifier,
            "personal-resources",
            {"operation": "memory_update", "content": payload.content, "version": payload.version},
        )
    if result.get("conflict"):
        raise Conflict("其他会话已更新记忆，请重新读取并合并后使用新版本保存")
    return result
