"""Require a knowledge attempt without preventing preparation or human input."""

from pydantic_ai import ModelRetry
from pydantic_ai.tools import DeferredToolRequests


def require_knowledge_before_answer(agent, knowledge):
    if knowledge is None or not knowledge.config.workbench_run_id:
        return

    @agent.output_validator
    def validate(output):
        # Clarification and environment preparation suspend the run; they are
        # not a final, evidence-backed answer and must remain available first.
        if isinstance(output, DeferredToolRequests):
            return output
        if knowledge.missing_searches:
            raise ModelRetry(
                "请先使用所选知识库再给出最终结论；可以先读取附件、调用其他工具或询问用户。"
                "尚未尝试的知识库：" + "、".join(knowledge.missing_searches)
            )
        return output
