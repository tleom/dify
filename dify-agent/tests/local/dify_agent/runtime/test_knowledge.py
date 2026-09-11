import asyncio
from types import SimpleNamespace

import pytest
from pydantic_ai import Agent, Tool
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from dify_agent.runtime.knowledge import prepare_knowledge_tools, require_knowledge_before_answer


@pytest.mark.parametrize("search_again", [False, True])
def test_agent_decides_search_count_and_rejects_unsearched_answer(search_again):
    queries = []
    knowledge = SimpleNamespace(config=SimpleNamespace(workbench_run_id="run"), missing_searches=["资料库"])

    def search(query: str) -> str:
        queries.append(query)
        knowledge.missing_searches = []
        return "63人" if query == "使用人数" else "4572次"

    def other() -> str:
        raise AssertionError("Unrelated tool should not run")

    requests = []

    def model(messages, info):
        requests.append([tool.name for tool in info.function_tools])
        if len(requests) == 1:
            return ModelResponse(parts=[TextPart("没有查询就直接回答")])
        if len(requests) == 2:
            return ModelResponse(parts=[ToolCallPart("knowledge_base_search", {"query": "使用人数"})])
        if len(requests) == 3 and search_again:
            return ModelResponse(parts=[ToolCallPart("knowledge_base_search", {"query": "累计使用次数"})])
        return ModelResponse(parts=[TextPart("63人，共4572次。")])

    tools = [Tool(search, name="knowledge_base_search"), Tool(other)]
    agent = Agent(FunctionModel(model), tools=prepare_knowledge_tools(tools, knowledge))
    require_knowledge_before_answer(agent, knowledge)
    result = asyncio.run(agent.run("请按资料说明人数和使用次数，并注明来源"))
    assert result.output == "63人，共4572次。"
    assert requests[:2] == [["knowledge_base_search"], ["knowledge_base_search"]]
    assert "other" in requests[2]
    assert queries == (["使用人数", "累计使用次数"] if search_again else ["使用人数"])
    assert tools[1].prepare is None
