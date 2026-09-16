"""Resolve explicit resource mentions against the published Agent allowlist."""

import json
from typing import TypedDict

from pydantic import BaseModel, ConfigDict, Field

from services.workbench.policy import template_resources


class ResourceMentions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tools: list[str] = Field(default_factory=list, max_length=200)
    skills: list[str] = Field(default_factory=list, max_length=200)
    knowledge: list[str] = Field(default_factory=list, max_length=200)


class ResolvedMentions(TypedDict):
    resource_mentions: dict[str, list[str]]
    mentioned_resources: list[dict[str, str]]
    mention_prompt: str


def default_capabilities(soul, selection, *, new_chat=False):
    resources = template_resources(soul)
    return selection.model_copy(
        update={
            "tools": list(resources["tools"]),
            "skills": list(resources["skills"]),
            "knowledge": [] if new_chat else selection.knowledge,
        }
    )


def resolve_mentions(
    soul, value, *, provider_names=None, skill_names=None, personal_skills=None, personal_mcp=None
) -> ResolvedMentions:
    refs = ResourceMentions.model_validate(value or {}).model_dump()
    resources = template_resources(soul)
    personal = {item["id"]: item for item in personal_skills or []}
    mcp = {item["id"]: item for item in personal_mcp or []}
    badges, tokens, seen = [], [], set()
    for kind in ("skills", "tools", "knowledge"):
        refs[kind] = list(dict.fromkeys(refs[kind]))
        for key in refs[kind]:
            if kind == "tools" and key.startswith("personal:mcp:"):
                item = mcp.get(key)
                if item is None:
                    raise ValueError("点名的个人 MCP 已不可用，请重新选择")
                badge_id = "plugin:mcp:" + item["plugin_id"]
                if (kind, badge_id) not in seen:
                    badges.append({"kind": kind, "id": badge_id, "name": item["provider_name"]})
                    seen.add((kind, badge_id))
                tokens.append({"kind": kind, "scope": "personal", "resource": item["runtime_name"]})
                continue
            if kind == "skills" and key.startswith("personal:"):
                item = personal.get(key)
                if item is None:
                    raise ValueError("点名的个人技能已不可用，请重新选择")
                badges.append({"kind": kind, "id": key, "name": item["name"]})
                tokens.append(
                    {
                        "kind": kind,
                        "scope": "personal",
                        "name": item["name"],
                        "resource": "read_skill",
                        "instruction": "先调用 read_skill，参数 scope=personal、name 为此技能名称，再遵循技能内容",
                    }
                )
                continue
            if key not in resources[kind]:
                raise ValueError("点名的资源已不可用，请重新选择")
            item = resources[kind][key]
            if kind == "tools":
                provider = item.get("plugin_id") or item.get("provider_id") or item.get("provider") or key
                badge_id = f"plugin:{item.get('provider_type') or ''}:{provider}"
                name = (provider_names or {}).get(key) or item.get("provider") or item.get("provider_id") or provider
                token = f"[§tool:{item.get('tool_name')}§]"
            else:
                badge_id, name = key, item.get("name") or key
                if kind == "skills":
                    name = (skill_names or {}).get(key) or name
                token = f"[§{'skill' if kind == 'skills' else 'knowledge'}:{key}§]"
            if (kind, badge_id) not in seen:
                badges.append({"kind": kind, "id": badge_id, "name": name})
                seen.add((kind, badge_id))
            tokens.append({"kind": kind, "resource": token})
    prompt = ""
    if tokens:
        prompt = (
            "\n本轮明确指定使用以下资源；每个点名工具组至少调用一个适用工具，"
            "点名 Skills 须读取并遵循；缺少必要参数时先询问：\n"
        ) + json.dumps(tokens, ensure_ascii=False)
    return {"resource_mentions": refs, "mentioned_resources": badges, "mention_prompt": prompt}


def load_run_mentions(run_id: str, tenant_id: str, account_id: str | None) -> ResourceMentions:
    """Read this turn's frozen, owner-scoped mentions, including continuations."""
    from sqlalchemy import select
    from werkzeug.exceptions import Forbidden

    from core.db.session_factory import session_factory
    from models.workbench import WorkbenchRun

    with session_factory.create_session() as session:
        run = session.scalar(
            select(WorkbenchRun).where(
                WorkbenchRun.id == run_id,
                WorkbenchRun.tenant_id == tenant_id,
                WorkbenchRun.account_id == account_id,
                WorkbenchRun.status == "running",
            )
        )
        if run is None:
            raise Forbidden()
        return ResourceMentions.model_validate(json.loads(run.payload).get("resource_mentions") or {})


def required_tool_groups(soul, mentions: ResourceMentions, tool_layers):
    """Resolve each selected plugin/group to its actual exposed runtime tools."""
    from dify_agent.layers.workbench_mentions import RequiredToolGroup

    from core.workflow.nodes.agent_v2.dify_tools_builder import WorkflowAgentDifyToolsBuilder
    from models.agent_config_entities import AgentSoulDifyToolConfig

    resources = template_resources(soul)
    if not set(mentions.tools) <= resources["tools"].keys() or not set(mentions.skills) <= resources["skills"].keys():
        raise ValueError("本轮点名资源已失效")
    groups: dict[tuple[str, str], RequiredToolGroup] = {}
    exposed = set(tool_layers.exposed_tool_names())
    for key in mentions.tools:
        item = resources["tools"][key]
        tool = AgentSoulDifyToolConfig.model_validate(item)
        provider_key = WorkflowAgentDifyToolsBuilder._provider_key(tool)
        group_key = (tool.provider_type, tool.plugin_id or provider_key[1])
        names = [tool.tool_name] if tool.tool_name else tool_layers.provider_tool_names.get(provider_key, [])
        if not names or not set(names) <= exposed:
            raise ValueError("点名工具未成功载入本轮运行")
        group = groups.setdefault(group_key, RequiredToolGroup(name=tool.provider or provider_key[1], tool_names=[]))
        group.tool_names = list(dict.fromkeys([*group.tool_names, *names]))
    return list(groups.values())
