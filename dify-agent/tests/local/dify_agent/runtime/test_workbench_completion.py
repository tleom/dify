import asyncio
import json

import httpx
import pytest
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import RetryPromptPart

from dify_agent.protocol import RunSucceededEvent
from dify_agent.runtime.runner import AgentRunRunner
from dify_agent.runtime.workbench_completion import unfinished_clarification

from .test_workbench_activity import _progress, _setup
from .test_workbench_files import add_files


INTRO = "这是一个很有价值的研究课题。在开始之前，我需要了解几个关键信息来确保研究方向和成果形式符合您的预期："


@pytest.mark.parametrize("text", [INTRO, "请您补充以下信息：", "我需要确认几个问题：\n\n"])
def test_rejects_empty_clarification_introduction(text):
    assert unfinished_clarification(text)


@pytest.mark.parametrize(
    "text",
    [
        "请确认您的研究对象是出租人还是经销商？",
        INTRO + "\n1. 研究站在出租人还是经销商立场？",
        "结果为：\n合同约定的回购条件已经满足。",
        "```text\n请您补充以下信息：\n```",
        "> 请您补充以下信息：",
        "分析结果如下：",
        "已完成。",
    ],
)
def test_leaves_complete_answers_and_literal_examples_alone(text):
    assert not unfinished_clarification(text)


def test_real_runner_continues_same_turn_before_publishing_incomplete_text(monkeypatch):
    calls = 0

    async def stream(messages, info):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield INTRO[:20]
            yield INTRO[20:]
        else:
            assert any(isinstance(part, RetryPromptPart) for message in messages for part in message.parts)
            yield "请确认研究主要站在出租人、经销商还是承租人的立场？"

    request, sink, _ = _setup(monkeypatch, stream)
    add_files(request)

    async def scenario():
        async with httpx.AsyncClient() as client:
            await AgentRunRunner(
                run_id="clarification",
                request=request,
                sink=sink,
                plugin_daemon_http_client=client,
                dify_api_http_client=client,
            ).run()

    asyncio.run(scenario())
    events = sink.events["clarification"]
    assert calls == 2
    assert isinstance(events[-1], RunSucceededEvent)
    assert (
        "".join(item.text for item in _progress(events, "text")) == "请确认研究主要站在出租人、经销商还是承租人的立场？"
    )
    public = [event for event in events if event.type == "pydantic_ai_event"]
    assert all(INTRO not in json.dumps(event.model_dump(mode="json"), ensure_ascii=False) for event in public)


def test_repeated_incomplete_output_exhausts_bounded_retry_without_success(monkeypatch):
    calls = 0

    async def stream(messages, info):
        nonlocal calls
        calls += 1
        yield INTRO

    request, sink, _ = _setup(monkeypatch, stream)
    add_files(request)

    async def scenario():
        async with httpx.AsyncClient() as client:
            with pytest.raises(UnexpectedModelBehavior):
                await AgentRunRunner(
                    run_id="incomplete",
                    request=request,
                    sink=sink,
                    plugin_daemon_http_client=client,
                    dify_api_http_client=client,
                ).run()

    asyncio.run(scenario())
    assert 1 < calls <= 5
    assert not any(isinstance(event, RunSucceededEvent) for event in sink.events["incomplete"])
    assert _progress(sink.events["incomplete"], "text") == []
