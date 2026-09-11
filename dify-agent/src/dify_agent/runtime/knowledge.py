"""Require selected knowledge searches before releasing a workbench answer."""

from copy import copy
from inspect import isawaitable

from pydantic_ai import ModelRetry


def prepare_knowledge_tools(tools, knowledge):
    if knowledge is None or not knowledge.config.workbench_run_id:
        return tools
    prepared = []
    for tool in tools:
        if tool.name == "knowledge_base_search":
            prepared.append(tool)
            continue
        current = copy(tool)
        original = current.prepare

        async def prepare(ctx, definition, previous=original):
            if knowledge.missing_searches:
                return None
            result = previous(ctx, definition) if previous else definition
            return await result if isawaitable(result) else result

        current.prepare = prepare
        prepared.append(current)
    return prepared


def require_knowledge_before_answer(agent, knowledge):
    if knowledge is None or not knowledge.config.workbench_run_id:
        return

    @agent.output_validator
    def validate(output):
        if knowledge.missing_searches:
            raise ModelRetry(
                "请先调用 knowledge_base_search，按问题提炼检索词，检索这些知识库后再回答："
                + "、".join(knowledge.missing_searches)
            )
        return output
