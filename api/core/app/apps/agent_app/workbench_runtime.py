"""Service-provided Workbench hooks for an Agent App execution.

The application service and task entry points supply the implementation so
the Agent App runtime does not import Workbench orchestration services.
"""

from typing import Protocol

from agenton.compositor import CompositorSessionSnapshot
from dify_agent.protocol import ContextStatusRunEvent, CreateRunRequest, DeferredToolResultsPayload

from clients.agent_backend import AgentBackendDeferredToolCallInternalEvent
from models.agent_config_entities import AgentSoulConfig


class AgentAppWorkbenchRuntime(Protocol):
    def sync_native_title(self, tenant_id: str, conversation_id: str) -> None: ...

    def record_context_status(
        self, tenant_id: str, conversation_id: str, account_id: str, public_event: ContextStatusRunEvent
    ) -> None: ...

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
