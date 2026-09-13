from pydantic_ai import Agent
from pydantic_ai.messages import SystemPromptPart
from pydantic_ai.models.test import TestModel

from agenton.layers import EmptyLayerConfig
from dify_agent.layers.workbench_environment import WorkbenchEnvironmentLayer


def test_environment_instructions_are_accepted_by_pydantic_ai_prompt_registration() -> None:
    layer = WorkbenchEnvironmentLayer.from_config(EmptyLayerConfig())
    agent = Agent(TestModel())
    for prompt in layer.prefix_prompts:
        agent.system_prompt(prompt)

    result = agent.run_sync("Explain the shared environment")

    prompts = [
        part.content
        for message in result.all_messages()
        for part in message.parts
        if isinstance(part, SystemPromptPart)
    ]
    assert any(
        "update_shared_environment" in prompt and "current conversation directory" in prompt for prompt in prompts
    )
