"""Real SDK tool calls recover CAS conflicts and lost HTTP responses without data loss."""

import asyncio
import json
from hashlib import sha256

import httpx
from pydantic_ai.messages import ToolReturnPart

from dify_agent.protocol import RunSucceededEvent
from dify_agent.protocol.workbench_control import WorkbenchControlState
from dify_agent.runtime.runner import AgentRunRunner
from .test_workbench_activity import _call, _setup
from .test_workbench_control_execution import add_control


def test_memory_conflict_merges_latest_then_retries_identical_payload_after_lost_response(monkeypatch):
    memory = {"content": "使用中文", "version": "initial"}
    calls, writes = [], []
    attempts = 0

    async def stream(messages, info):
        calls.append(messages)
        assert "Maintain it proactively" in info.instructions
        assert "Update it only when the user asks" not in info.instructions
        assert (
            'Current memory version (copy this exact JSON value; never add a prefix or invent a hash): "'
            + memory["version"]
            + '"'
            in info.instructions
        )
        if len(calls) == 1:
            yield {0: _call("read_memory", {}, "read")}
        elif len(calls) == 2:
            yield {0: _call("update_memory", {"content": "使用中文\n金额保留两位小数", "version": "initial"}, "save")}
        elif len(calls) == 3:
            retries = [
                p
                for message in messages
                for p in message.parts
                if isinstance(p, ToolReturnPart) and p.outcome == "failed"
            ]
            assert retries and "报告先写结论" in str(retries[-1].content)
            assert "concurrent" in str(retries[-1].content)
            yield {
                0: _call(
                    "update_memory",
                    {"content": "使用中文\n报告先写结论\n金额保留两位小数", "version": "concurrent"},
                    "save",
                )
            }
        else:
            assert memory["content"] in info.instructions
            yield "处理完成。"

    request, sink, _ = _setup(monkeypatch, stream)
    add_control(request)

    def transport(request):
        nonlocal attempts
        body = json.loads(request.content)
        if request.url.path.endswith("/resources"):
            return httpx.Response(200, json={"memory": memory.copy(), "skills": []})
        if request.url.path.endswith("/control"):
            return httpx.Response(200, json={"state": WorkbenchControlState().model_dump(mode="json")})
        assert request.url.path.endswith("/memory")
        assert body["account_id"] == "owner-1" and body["backend_run_id"] == "memory-run"
        attempts += 1
        writes.append(body)
        if attempts == 1:
            memory.update(content="使用中文\n报告先写结论", version="concurrent")
            return httpx.Response(409, json={"message": "其他会话已更新记忆"})
        if attempts == 2:
            memory.update(content=body["content"], version=sha256(body["content"].encode()).hexdigest())
            raise httpx.ReadTimeout("response lost after save", request=request)
        assert writes[-1] == writes[-2]
        return httpx.Response(200, json=memory.copy())

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            await AgentRunRunner(
                run_id="memory-run",
                request=request,
                sink=sink,
                plugin_daemon_http_client=client,
                dify_api_http_client=client,
            ).run()

    asyncio.run(scenario())
    assert isinstance(sink.events["memory-run"][-1], RunSucceededEvent), sink.events["memory-run"][-1]
    assert len(calls) == 4 and attempts == 3
    assert memory["content"] == "使用中文\n报告先写结论\n金额保留两位小数"
