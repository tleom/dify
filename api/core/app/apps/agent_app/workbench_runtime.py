"""Service-provided Workbench hooks for an Agent App execution.

The application service and task entry points supply the implementation so
the Agent App runtime does not import Workbench orchestration services.
"""

from typing import Any, Protocol

from agenton.compositor import CompositorSessionSnapshot
from dify_agent.layers.workbench_mentions import RequiredToolGroup
from dify_agent.protocol import (
    ContextStatusRunEvent,
    CreateRunRequest,
    DeferredToolResultsPayload,
    WorkbenchActivityRunEvent,
)

from clients.agent_backend import AgentBackendDeferredToolCallInternalEvent
from core.workflow.nodes.agent_v2.dify_tools_builder import WorkflowAgentToolLayers
from models.agent_config_entities import AgentSoulConfig


class AgentAppWorkbenchRuntime(Protocol):
    def resolve_run_requirements(
        self,
        run_id: str,
        tenant_id: str,
        account_id: str | None,
        soul: AgentSoulConfig,
        tool_layers: WorkflowAgentToolLayers,
    ) -> tuple[list[str], list[RequiredToolGroup]]: ...

    def activity_protocol(self, tenant_id: str, conversation_id: str, account_id: str) -> int: ...

    def accept_activity(
        self, tenant_id: str, conversation_id: str, account_id: str, public_event: WorkbenchActivityRunEvent
    ) -> bool: ...

    def sync_native_title(self, tenant_id: str, conversation_id: str) -> None: ...

    def record_context_status(
        self, tenant_id: str, conversation_id: str, account_id: str, public_event: ContextStatusRunEvent
    ) -> dict[str, Any] | None: ...

    def resolve_run_config(self, run_id: str, tenant_id: str, account_id: str) -> AgentSoulConfig: ...

    def resolve_run_generation(self, run_id: str, tenant_id: str, account_id: str) -> str: ...

    def attach_conversation(
        self, run_id: str, tenant_id: str, account_id: str, conversation_id: str, task_id: str
    ) -> None: ...

    def conversation_owner(self, tenant_id: str, conversation_id: str, account_id: str) -> str | None: ...

    def execution_run_id(self, tenant_id: str, conversation_id: str, account_id: str) -> str | None: ...

    def continuation(
        self, tenant_id: str, conversation_id: str, account_id: str
    ) -> DeferredToolResultsPayload | None: ...

    def prepare_execution(
        self, tenant_id: str, conversation_id: str, account_id: str, request: CreateRunRequest
    ) -> None: ...

    def pause(
        self,
        tenant_id: str,
        conversation_id: str,
        account_id: str,
        terminal: AgentBackendDeferredToolCallInternalEvent,
        binding_id: str,
    ) -> bool: ...

    def capture_run_history(
        self,
        tenant_id: str,
        account_id: str,
        conversation_id: str,
        run_id: str,
        snapshot: CompositorSessionSnapshot,
    ) -> None: ...
