"""Exercise ask-human validation, deferral and resumption with pydantic-ai."""

import asyncio
import json

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, RetryPromptPart, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults

from dify_agent.layers.ask_human.configs import DifyAskHumanLayerConfig
from dify_agent.layers.ask_human.layer import DifyAskHumanLayer
from dify_agent.layers.ask_human.schema import AskHumanToolArgs


@pytest.mark.parametrize(
    "config",
    [
        DifyAskHumanLayerConfig(),
        DifyAskHumanLayerConfig(max_fields=0),
        DifyAskHumanLayerConfig(allowed_field_types=[]),
        DifyAskHumanLayerConfig(allowed_field_types=["paragraph"], max_question_chars=1, max_field_label_chars=1),
    ],
)
def test_prompt_example_is_valid_for_config(config):
    layer = DifyAskHumanLayer.from_config(config)
    args = json.loads(layer.build_prompt_hint().split("Example arguments: ", 1)[1])
    payload = layer.build_deferred_tool_call_payload(
        DeferredToolRequests(calls=[ToolCallPart("ask_human", args, tool_call_id="question")])
    )
    assert payload.args["question"]


def test_schema_exposes_identifier_and_nested_option_contract():
    schema = AskHumanToolArgs.model_json_schema()
    assert "question" in schema["required"]
    defs = schema["$defs"]
    assert defs["AskHumanAction"]["properties"]["id"]["pattern"]
    assert defs["AskHumanSelectField"]["properties"]["name"]["pattern"]
    options = defs["AskHumanSelectField"]["properties"]["options"]
    assert options["items"]["$ref"].startswith("#/$defs/")


def test_default_prompt_example_pairs_three_choices_with_an_inline_other_answer():
    layer = DifyAskHumanLayer.from_config(DifyAskHumanLayerConfig())
    args = AskHumanToolArgs.model_validate(json.loads(layer.build_prompt_hint().split("Example arguments: ", 1)[1]))
    choice, answer = args.fields
    assert choice.type == "select"
    assert len(choice.options) == 4
    assert choice.options[-1].value == "other"
    assert answer.type == "paragraph"
    assert answer.name == f"{choice.name}_other"
    assert not answer.required


@pytest.mark.parametrize("invalid_first", [False, True])
def test_model_call_defers_and_resumes_with_human_answer(invalid_first):
    layer = DifyAskHumanLayer.from_config(DifyAskHumanLayerConfig())
    args = {
        "question": "请选择处理方式",
        "fields": [
            {"type": "select", "name": "choice", "label": "处理方式", "options": [{"value": "yes", "label": "继续"}]}
        ],
    }
    requests = []

    def response(messages, info):
        requests.append(messages)
        assert info.function_tools[0].parameters_json_schema == AskHumanToolArgs.model_json_schema()
        if any(isinstance(part, ToolReturnPart) for message in messages for part in message.parts):
            return ModelResponse(parts=[TextPart("已收到选择，继续处理。")])
        if invalid_first and len(requests) == 1:
            return ModelResponse(parts=[ToolCallPart("ask_human", {"fields": []}, tool_call_id="bad")])
        return ModelResponse(parts=[ToolCallPart("ask_human", args, tool_call_id="question")])

    agent = Agent(FunctionModel(response), tools=layer.tools, output_type=[str, DeferredToolRequests], retries=1)

    async def run():
        initial = await agent.run("需要我选择时请询问。")
        assert isinstance(initial.output, DeferredToolRequests)
        payload = layer.build_deferred_tool_call_payload(initial.output)
        assert payload.args["fields"][0]["options"] == [{"value": "yes", "label": "继续", "description": None}]
        assert payload.args["actions"][0]["id"] == "submit"
        if invalid_first:
            assert any(
                isinstance(part, RetryPromptPart) for message in initial.all_messages() for part in message.parts
            )
        resumed = await agent.run(
            message_history=initial.all_messages(),
            deferred_tool_results=DeferredToolResults(calls={"question": {"choice": "yes"}}),
        )
        assert resumed.output == "已收到选择，继续处理。"

    asyncio.run(run())
