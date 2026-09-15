"""Feed current-task supplements into the native Pydantic AI run boundaries."""

from __future__ import annotations

from dataclasses import dataclass

import httpx
from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability, AgentNode, NodeResult
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.tools import DeferredToolRequests
from pydantic_graph import End

from dify_agent.layers.workbench_followups import WorkbenchFollowupsLayer


@dataclass
class WorkbenchFollowupsCapability(AbstractCapability[None]):
    layer: WorkbenchFollowupsLayer
    http_client: httpx.AsyncClient
    run_id: str

    async def before_model_request(self, ctx, request_context):
        batch = await self.layer.poll(self.http_client, self.run_id)
        for item in batch.messages:
            if item.id in self.layer.runtime_state.seen_ids:
                continue
            message = ModelRequest(
                parts=[UserPromptPart(item.content)],
                run_id=ctx.run_id,
                conversation_id=ctx.conversation_id,
                metadata={"workbench_followup_id": item.id},
            )
            # The SDK gives model hooks a request copy. Update both lists, as its
            # built-in pending-message drain does, to retain the user's message.
            request_context.messages.append(message)
            ctx.messages.append(message)
            self.layer.runtime_state.seen_ids.add(item.id)
        return request_context

    async def after_node_run(
        self, ctx: RunContext[None], *, node: AgentNode[None], result: NodeResult[None]
    ) -> NodeResult[None]:
        if not isinstance(result, End) or isinstance(result.data.output, DeferredToolRequests):
            return result
        batch = await self.layer.poll(self.http_client, self.run_id, action="seal")
        for item in batch.messages:
            if item.id in self.layer.runtime_state.seen_ids:
                continue
            # Pydantic AI's outer pending-message capability redirects End back
            # into this same run. No new task or cancelled/replayed tool call.
            ctx.enqueue(
                ModelRequest(parts=[UserPromptPart(item.content)], metadata={"workbench_followup_id": item.id}),
                priority="asap",
            )
            self.layer.runtime_state.seen_ids.add(item.id)
        return result
