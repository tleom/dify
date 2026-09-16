"""Persist accepted follow-ups before a model call or tool effect can be interrupted."""

import asyncio
import json

import httpx
from pydantic_ai import Tool
from pydantic_ai.messages import UserPromptPart

from dify_agent.protocol import RunLayerSpec
from dify_agent.runtime.runner import AgentRunRunner

from .test_workbench_activity import _call, _setup


def test_real_runner_checkpoints_followups_before_model_and_tool_execution(monkeypatch):
    calls = 0
    executed = []

    def saved_adjustments():
        state = json.loads(sink.history_checkpoints["combined-native"])
        assert state["steering_delivered_ids"] == ["change"]
        return [
            item for item in state["messages"] if (item.get("metadata") or {}).get("workbench_followup_id") == "change"
        ]

    async def work():
        assert len(saved_adjustments()) == 1
        executed.append("saved")
        return "已保存"

    async def stream(messages, info):
        nonlocal calls
        calls += 1
        prompts = [part.content for message in messages for part in message.parts if isinstance(part, UserPromptPart)]
        assert prompts.count("改成横版") == 1
        assert len(saved_adjustments()) == 1
        if calls == 1:
            yield {0: _call("work", {}, "save")}
        else:
            yield "完成"

    request, sink, _ = _setup(monkeypatch, stream, [Tool(work)])
    context = next(layer for layer in request.composition.layers if layer.name == "execution_context")
    context.config = {**dict(context.config), "workbench_run_id": "combined", "user_id": "user", "app_id": "app"}
    request.composition.layers.append(
        RunLayerSpec(
            name="workbench_followups", type="dify.workbench_followups", deps={"execution_context": "execution_context"}
        )
    )

    def transport(request):
        data = json.loads(request.content)
        missing = "change" not in data["seen_ids"]
        return httpx.Response(
            200,
            json={
                "messages": [{"id": "change", "content": "改成横版"}] if missing else [],
                "sealed": data["action"] == "seal" and not missing,
            },
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            await AgentRunRunner(
                run_id="combined-native",
                request=request,
                sink=sink,
                plugin_daemon_http_client=client,
                dify_api_http_client=client,
            ).run()

    asyncio.run(run())
    assert executed == ["saved"]
    assert sink.events["combined-native"][-1].type == "run_succeeded"
