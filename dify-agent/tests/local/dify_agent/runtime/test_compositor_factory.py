from typing import cast

from dify_agent.layers.config import DIFY_CONFIG_LAYER_TYPE_ID, DifyConfigLayerConfig
from dify_agent.layers.config.layer import DifyConfigLayer
from dify_agent.layers.dify_core_tools import DIFY_CORE_TOOLS_LAYER_TYPE_ID, DifyCoreToolsLayerConfig
from dify_agent.layers.dify_core_tools.layer import DifyCoreToolsLayer
from dify_agent.layers.runtime import DIFY_RUNTIME_LAYER_TYPE_ID, DifyRuntimeLayerConfig
from dify_agent.layers.runtime.layer import DifyRuntimeLayer
from dify_agent.layers.shell import DIFY_SHELL_LAYER_TYPE_ID, DifyShellLayerConfig
from dify_agent.layers.execution_context import DifyExecutionContextLayerConfig
from dify_agent.layers.shell.layer import DifyShellLayer
from dify_agent.runtime.compositor_factory import create_default_layer_providers
from dify_agent.runtime_backend import ExecutionBindingBackend, HomeSnapshotBackend, RuntimeBackendProfile


class FakeProvider:
    """No-op provider for tests that never actually open a shell resource."""

    async def create(self) -> object:
        raise AssertionError("create should not be called by these tests")


def _runtime_backend_profile() -> RuntimeBackendProfile:
    return RuntimeBackendProfile(
        home_snapshots=cast(HomeSnapshotBackend, cast(object, FakeProvider())),
        execution_bindings=cast(ExecutionBindingBackend, cast(object, FakeProvider())),
    )


def test_default_layer_providers_register_config_layer() -> None:
    providers = create_default_layer_providers()

    config_provider = next(provider for provider in providers if provider.type_id == DIFY_CONFIG_LAYER_TYPE_ID)
    config = DifyConfigLayerConfig(agent_id="agent-1")
    layer = config_provider.create_layer(config)

    assert isinstance(layer, DifyConfigLayer)
    assert layer.type_id == DIFY_CONFIG_LAYER_TYPE_ID
    assert layer.config == config


def test_default_layer_providers_register_runtime_layer() -> None:
    profile = _runtime_backend_profile()

    providers = create_default_layer_providers(
        runtime_backend_profile=profile,
    )
    shell_provider = next(provider for provider in providers if provider.type_id == DIFY_SHELL_LAYER_TYPE_ID)
    shell_layer = shell_provider.create_layer(DifyShellLayerConfig())
    runtime_provider = next(provider for provider in providers if provider.type_id == DIFY_RUNTIME_LAYER_TYPE_ID)
    runtime_layer = runtime_provider.create_layer(DifyRuntimeLayerConfig(backend_binding_ref="binding-1"))

    assert isinstance(shell_layer, DifyShellLayer)
    assert isinstance(runtime_layer, DifyRuntimeLayer)
    assert runtime_layer.backend is profile.execution_bindings
    assert {provider.type_id for provider in providers} >= {"dify.runtime", "dify.shell"}


def test_default_layer_providers_forward_agent_stub_token_factory() -> None:
    captured_calls: list[tuple[DifyExecutionContextLayerConfig, str | None]] = []

    def build_agent_stub_token(
        execution_context: DifyExecutionContextLayerConfig,
        *,
        session_id: str | None,
    ) -> str:
        captured_calls.append((execution_context, session_id))
        return f"token-for:{execution_context.tenant_id}:{session_id}"

    providers = create_default_layer_providers(
        runtime_backend_profile=_runtime_backend_profile(),
        agent_stub_api_base_url="https://agent.example.com/agent-stub",
        agent_stub_token_factory=build_agent_stub_token,
    )
    shell_provider = next(provider for provider in providers if provider.type_id == DIFY_SHELL_LAYER_TYPE_ID)
    shell_layer = shell_provider.create_layer(DifyShellLayerConfig())

    token = shell_layer.agent_stub_token_factory(
        DifyExecutionContextLayerConfig(
            tenant_id="tenant-1",
            user_id="user-1",
            user_from="account",
            agent_mode="workflow_run",
            invoke_from="service-api",
        ),
        session_id="abc12ff",
    )

    assert token == "token-for:tenant-1:abc12ff"
    assert captured_calls == [
        (
            DifyExecutionContextLayerConfig(
                tenant_id="tenant-1",
                user_id="user-1",
                user_from="account",
                agent_mode="workflow_run",
                invoke_from="service-api",
            ),
            "abc12ff",
        )
    ]


def test_default_layer_providers_register_core_tools_layer() -> None:
    providers = create_default_layer_providers(inner_api_url="http://dify-api", inner_api_key="inner-secret")

    core_provider = next(provider for provider in providers if provider.type_id == DIFY_CORE_TOOLS_LAYER_TYPE_ID)
    layer = core_provider.create_layer(DifyCoreToolsLayerConfig())

    assert isinstance(layer, DifyCoreToolsLayer)
    assert layer.type_id == DIFY_CORE_TOOLS_LAYER_TYPE_ID
    assert layer.inner_api_url == "http://dify-api"
    assert layer.inner_api_key == "inner-secret"
    assert layer.config == DifyCoreToolsLayerConfig()
