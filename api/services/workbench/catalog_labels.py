"""Display metadata from Dify declarations, without changing the public allowlist."""

import logging

from sqlalchemy import select

from core.tools.builtin_tool.provider import BuiltinToolProviderController
from core.tools.custom_tool.provider import ApiToolProviderController
from core.tools.mcp_tool.provider import MCPToolProviderController
from core.tools.plugin_tool.provider import PluginToolProviderController
from core.tools.tool_manager import ToolManager
from core.tools.workflow_as_tool.provider import WorkflowToolProviderController
from extensions.ext_database import db
from models.tools import WorkflowToolProvider

logger = logging.getLogger(__name__)


def localized(value, fallback=""):
    if isinstance(value, str):
        return value or fallback
    if isinstance(value, dict):
        return value.get("zh_Hans") or value.get("en_US") or fallback
    return getattr(value, "zh_Hans", None) or getattr(value, "en_US", None) or fallback


def provider_metadata(tenant_id, kind, provider_id):
    controller: (
        PluginToolProviderController
        | BuiltinToolProviderController
        | MCPToolProviderController
        | ApiToolProviderController
        | WorkflowToolProviderController
    )
    if kind == "plugin":
        controller = ToolManager.get_plugin_provider(provider_id, tenant_id)
    elif kind == "builtin":
        controller = ToolManager.get_builtin_provider(provider_id, tenant_id)
    elif kind == "mcp":
        controller = ToolManager.get_mcp_provider_controller(tenant_id, provider_id)
    elif kind == "api":
        controller, _ = ToolManager.get_api_provider_controller(tenant_id, provider_id)
    elif kind == "workflow":
        provider = db.session.scalar(
            select(WorkflowToolProvider).where(
                WorkflowToolProvider.id == provider_id, WorkflowToolProvider.tenant_id == tenant_id
            )
        )
        if provider is None:
            raise ValueError("Provider unavailable")
        controller = WorkflowToolProviderController.from_db(provider)
    else:
        return None
    identity = controller.entity.identity
    tools = (
        controller.get_tools(tenant_id)
        if isinstance(controller, ApiToolProviderController | WorkflowToolProviderController)
        else controller.get_tools()
    )
    return localized(identity.label, identity.name), {
        tool.entity.identity.name: {
            "name": localized(tool.entity.identity.label, tool.entity.identity.name),
            "description": localized(tool.entity.description.human) if tool.entity.description else "",
        }
        for tool in tools or []
    }


def enrich_tool_labels(tenant_id, tools):
    providers = {}
    for tool in tools:
        key = (tool.get("group"), tool.get("provider"))
        if key not in providers:
            try:
                providers[key] = provider_metadata(tenant_id, *key)
            except Exception:
                # Display lookup failure must not hide the resource or expose credential errors.
                logger.warning("Workbench provider display metadata unavailable: %s", key)
                providers[key] = None
        metadata = providers[key]
        if metadata is None:
            continue
        provider_name, declarations = metadata
        tool["provider_name"] = provider_name
        declaration = declarations.get(tool.get("tool_name"))
        if declaration:
            tool.update(declaration)
        elif not tool.get("tool_name"):
            tool["name"] = provider_name
