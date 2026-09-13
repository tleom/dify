"""Resolve explicit resource mentions against the published Agent allowlist."""
import json

from pydantic import BaseModel, ConfigDict, Field

from services.workbench.policy import template_resources


class ResourceMentions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tools: list[str] = Field(default_factory=list, max_length=200)
    skills: list[str] = Field(default_factory=list, max_length=200)
    knowledge: list[str] = Field(default_factory=list, max_length=200)


def default_capabilities(soul, selection, *, new_chat=False):
    resources = template_resources(soul)
    return selection.model_copy(update={
        "tools": list(resources["tools"]), "skills": list(resources["skills"]),
        "knowledge": [] if new_chat else selection.knowledge,
    })


def resolve_mentions(soul, value, *, provider_names=None, skill_names=None):
    refs = ResourceMentions.model_validate(value or {}).model_dump()
    resources = template_resources(soul)
    badges, tokens, seen = [], [], set()
    for kind in ("skills", "tools", "knowledge"):
        refs[kind] = list(dict.fromkeys(refs[kind]))
        for key in refs[kind]:
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
            "\n本轮明确指定使用以下资源，请调用这些资源处理请求；缺少必要参数时先询问：\n"
            + json.dumps(tokens, ensure_ascii=False)
        )
    return {"resource_mentions": refs, "mentioned_resources": badges, "mention_prompt": prompt}
