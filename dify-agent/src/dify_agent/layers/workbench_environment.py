"""Deferred shared Python/Node dependency management for account workbenches."""

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import RunContext
from pydantic_ai.tools import DeferredToolRequests, Tool, ToolDefinition

from agenton.layers import EmptyLayerConfig, EmptyRuntimeState, NoLayerDeps, PydanticAILayer
from dify_agent.protocol.schemas import DeferredToolCallPayload

TOOL_NAME = "update_shared_environment"


class EnvironmentArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    python: list[str] = Field(default_factory=list, max_length=50)
    node: list[str] = Field(default_factory=list, max_length=50)
    reason: str = Field(min_length=1, max_length=1000)


class WorkbenchEnvironmentLayer(PydanticAILayer[NoLayerDeps, object, EmptyLayerConfig, EmptyRuntimeState]):
    type_id: ClassVar[str | None] = "dify.workbench_environment"

    @property
    def tools(self):
        return [Tool(self._deferred, takes_ctx=True, name=TOOL_NAME, prepare=self._prepare, sequential=True)]

    @property
    def prefix_prompts(self):
        return [self._environment_prompt]

    @staticmethod
    def _environment_prompt() -> str:
        return (
            "Common Office/PDF/OCR/image/data libraries, LibreOffice, Chinese fonts and Google Chrome are preinstalled. "
            "Read /opt/office/README.md and use python/node directly before requesting extra packages. "
            "Use /opt/office/office.py for isolated Office conversion, previews and formula recalculation. "
            "Use update_shared_environment only for missing or explicitly requested Python/Node registry packages. "
            "The environment is shared by this user's conversations and mounted read-only. "
            "The task will pause while other runs finish, then resume with the result. "
            "Do not use pip/npm to create private replacement environments. "
            "All uploaded, generated, and intermediate files belong in your current conversation directory. "
            "Use relative paths from the working directory. Other conversations and the old shared folder are inaccessible."
        )

    def _prepare(self, _ctx: RunContext[object], _definition: ToolDefinition):
        return ToolDefinition(
            name=TOOL_NAME,
            description="Install or upgrade shared Python/Node dependencies atomically.",
            parameters_json_schema=EnvironmentArgs.model_json_schema(),
            sequential=True,
            kind="external",
        )

    async def _deferred(
        self, _ctx: RunContext[object], reason: str, python: list[str] | None = None, node: list[str] | None = None
    ) -> str:
        raise RuntimeError("Environment updates must be deferred to the workbench controller")

    def build_deferred_tool_call_payload(self, requests: DeferredToolRequests):
        if requests.approvals or len(requests.calls) != 1 or requests.calls[0].tool_name != TOOL_NAME:
            raise ValueError("Request exactly one environment update at a time")
        call = requests.calls[0]
        args = (
            EnvironmentArgs.model_validate_json(call.args)
            if isinstance(call.args, str)
            else EnvironmentArgs.model_validate(call.args)
        )
        return DeferredToolCallPayload(
            tool_call_id=call.tool_call_id, tool_name=call.tool_name, args=args.model_dump(mode="json")
        )
