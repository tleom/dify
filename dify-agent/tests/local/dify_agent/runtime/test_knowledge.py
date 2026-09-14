import asyncio
from types import SimpleNamespace

import pytest
from pydantic_ai import Agent, Tool
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.tools import DeferredToolRequests

from dify_agent.runtime.knowledge import require_knowledge_before_answer


@pytest.mark.parametrize("search_again", [False, True])
def test_agent_can_prepare_before_search_and_still_requires_a_knowledge_attempt(search_again):
    queries = []
    knowledge = SimpleNamespace(config=SimpleNamespace(workbench_run_id="run"), missing_searches=["资料库"])

    def search(query: str) -> str:
        queries.append(query)
        knowledge.missing_searches = []
        return "63人" if query == "使用人数" else "4572次"

    def read_attachment() -> str:
        return "需要核对的指标：使用人数、累计使用次数"

    requests = []

    def model(messages, info):
        requests.append([tool.name for tool in info.function_tools])
        if len(requests) == 1:
            return ModelResponse(parts=[TextPart("没有查询就直接回答")])
        if len(requests) == 2:
            return ModelResponse(parts=[ToolCallPart("read_attachment", {})])
        if len(requests) == 3:
            return ModelResponse(parts=[ToolCallPart("knowledge_base_search", {"query": "使用人数"})])
        if len(requests) == 4 and search_again:
            return ModelResponse(parts=[ToolCallPart("knowledge_base_search", {"query": "累计使用次数"})])
        return ModelResponse(parts=[TextPart("按已查询的资料回答。")])

    tools = [Tool(search, name="knowledge_base_search"), Tool(read_attachment)]
    agent = Agent(FunctionModel(model), tools=tools)
    require_knowledge_before_answer(agent, knowledge)
    result = asyncio.run(agent.run("请按资料说明人数和使用次数，并注明来源"))
    assert result.output == "按已查询的资料回答。"
    assert all("read_attachment" in names for names in requests)
    assert queries == (["使用人数", "累计使用次数"] if search_again else ["使用人数"])
    assert tools[1].prepare is None


def test_human_input_can_suspend_before_knowledge_is_searched():
    knowledge = SimpleNamespace(config=SimpleNamespace(workbench_run_id="run"), missing_searches=["资料库"])

    def ask_human(question: str) -> str:
        raise AssertionError("External human input must not execute in this run")

    def prepare(ctx, definition):
        definition.kind = "external"
        return definition

    def model(messages, info):
        assert {tool.name for tool in info.function_tools} == {"ask_human"}
        return ModelResponse(parts=[ToolCallPart("ask_human", {"question": "需要查询哪一年？"})])

    agent = Agent(
        FunctionModel(model), tools=[Tool(ask_human, prepare=prepare)], output_type=[str, DeferredToolRequests]
    )
    require_knowledge_before_answer(agent, knowledge)
    result = asyncio.run(agent.run("查询年度资料"))
    assert isinstance(result.output, DeferredToolRequests)
    assert result.output.calls[0].tool_name == "ask_human"
    assert knowledge.missing_searches == ["资料库"]
