"""Refresh collaboration guidance at accepted model boundaries, outside history."""

from dataclasses import dataclass

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.models import InstructionPart

from dify_agent.layers.workbench_control import WorkbenchControlLayer


@dataclass
class WorkbenchControlCapability(AbstractCapability[None]):
    layer: WorkbenchControlLayer

    async def before_model_request(self, ctx, request_context):
        await self.layer.request()
        await self.layer.resource_request()
        state = self.layer.runtime_state.state
        if state.plan.pending:
            await self.layer.request("apply_plan", {"revision": state.revision})
        # Use the SDK's dynamic instruction slot, not a synthetic history turn.
        params = request_context.model_request_parameters
        params.instruction_parts = [
            *(params.instruction_parts or []),
            InstructionPart(content=self.layer.guidance(), dynamic=True),
        ]
        return request_context
