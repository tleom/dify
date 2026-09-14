import asyncio
import json

import httpx
import pytest
from pydantic_ai import Tool

from agenton.compositor import Compositor, LayerNode, LayerProvider
from dify_agent.layers.execution_context import DifyExecutionContextLayerConfig
from dify_agent.layers.execution_context.layer import DifyExecutionContextLayer
from dify_agent.layers.knowledge.client import (
    DifyKnowledgeBaseClient,
    DifyKnowledgeBaseClientError,
    DifyKnowledgeRetrieveResponse,
)
from dify_agent.layers.knowledge.configs import DifyKnowledgeBaseLayerConfig
from dify_agent.layers.knowledge.layer import (
    BLANK_QUERY_OBSERVATION,
    DifyKnowledgeBaseLayer,
    NO_RESULTS_OBSERVATION,
    TEMPORARY_UNAVAILABLE_OBSERVATION,
)


def _execution_context_config(**overrides: object) -> DifyExecutionContextLayerConfig:
    payload: dict[str, object] = {
        "tenant_id": "tenant-1",
        "user_id": "user-1",
        "user_from": "account",
        "app_id": "app-1",
        "agent_mode": "agent_app",
        "invoke_from": "web-app",
    }
    payload.update(overrides)
    return DifyExecutionContextLayerConfig.model_validate(payload)


def _knowledge_config(**overrides: object) -> DifyKnowledgeBaseLayerConfig:
    set_payload: dict[str, object] = {
        "id": "support",
        "name": "Support KB",
        "datasets": [{"id": "dataset-1"}],
        "query": {"mode": "generated_query"},
        "retrieval": {"mode": "multiple", "top_k": 4},
    }
    for key in ("id", "name", "description", "datasets", "query", "retrieval", "metadata_filtering"):
        if key in overrides:
            set_payload[key] = overrides.pop(key)
    if "dataset_ids" in overrides:
        dataset_ids = overrides.pop("dataset_ids")
        assert isinstance(dataset_ids, list)
        set_payload["datasets"] = [{"id": dataset_id} for dataset_id in dataset_ids]
    payload: dict[str, object] = {
        "sets": [set_payload],
    }
    payload.update(overrides)
    return DifyKnowledgeBaseLayerConfig.model_validate(payload)


def _execution_context_provider() -> LayerProvider[DifyExecutionContextLayer]:
    return LayerProvider.from_factory(
        layer_type=DifyExecutionContextLayer,
        create=lambda config: DifyExecutionContextLayer.from_config_with_settings(
            DifyExecutionContextLayerConfig.model_validate(config),
            daemon_url="http://plugin-daemon",
            daemon_api_key="daemon-secret",
        ),
    )


def _knowledge_provider() -> LayerProvider[DifyKnowledgeBaseLayer]:
    return LayerProvider.from_factory(
        layer_type=DifyKnowledgeBaseLayer,
        create=lambda config: DifyKnowledgeBaseLayer.from_config_with_settings(
            DifyKnowledgeBaseLayerConfig.model_validate(config),
            inner_api_url="http://dify-api",
            inner_api_key="inner-secret",
        ),
    )


def test_knowledge_layer_exposes_one_set_scoped_tool_definition() -> None:
    async def scenario() -> None:
        compositor = Compositor(
            [
                LayerNode("execution_context", _execution_context_provider()),
                LayerNode("knowledge", _knowledge_provider(), deps={"execution_context": "execution_context"}),
            ]
        )
        async with httpx.AsyncClient() as http_client:
            async with compositor.enter(
                configs={
                    "execution_context": _execution_context_config(),
                    "knowledge": _knowledge_config(),
                }
            ) as run:
                knowledge_layer = run.get_layer("knowledge", DifyKnowledgeBaseLayer)
                tool = (await knowledge_layer.get_tools(http_client=http_client))[0]
                tool_def = await tool.prepare_tool_def(None)  # pyright: ignore[reportArgumentType]
                assert isinstance(tool, Tool)
                assert tool.name == "knowledge_base_search"
                assert tool.description is not None
                assert "Pick one configured set_name" in tool.description
                assert tool_def is not None
                assert tool_def.description is not None
                assert "Pick one configured set_name" in tool_def.description
                assert tool_def.parameters_json_schema == {
                    "type": "object",
                    "properties": {
                        "set_name": {
                            "type": "string",
                            "enum": ["Support KB"],
                            "description": "Knowledge set to search.",
                        },
                        "query": {
                            "type": "string",
                            "description": "Search query for the selected knowledge set.",
                        },
                    },
                    "required": ["set_name", "query"],
                    "additionalProperties": False,
                }

    asyncio.run(scenario())


def test_knowledge_layer_rejects_blank_query_locally() -> None:
    async def scenario() -> None:
        compositor = Compositor(
            [
                LayerNode("execution_context", _execution_context_provider()),
                LayerNode("knowledge", _knowledge_provider(), deps={"execution_context": "execution_context"}),
            ]
        )
        async with httpx.AsyncClient() as http_client:
            async with compositor.enter(
                configs={
                    "execution_context": _execution_context_config(),
                    "knowledge": _knowledge_config(),
                }
            ) as run:
                knowledge_layer = run.get_layer("knowledge", DifyKnowledgeBaseLayer)
                tool = (await knowledge_layer.get_tools(http_client=http_client))[0]
                result = await tool.function_schema.call(  # pyright: ignore[reportArgumentType]
                    {"set_name": "Support KB", "query": "   "},
                    None,  # pyright: ignore[reportArgumentType]
                )
                assert result == BLANK_QUERY_OBSERVATION

    asyncio.run(scenario())


def test_knowledge_layer_exposes_no_tool_when_all_sets_are_user_query(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_retrieve(self: DifyKnowledgeBaseClient, **_kwargs: object) -> DifyKnowledgeRetrieveResponse:
        del self
        return DifyKnowledgeRetrieveResponse.model_validate({"results": [], "usage": {}})

    monkeypatch.setattr(DifyKnowledgeBaseClient, "retrieve", fake_retrieve)

    async def scenario() -> None:
        compositor = Compositor(
            [
                LayerNode("execution_context", _execution_context_provider()),
                LayerNode("knowledge", _knowledge_provider(), deps={"execution_context": "execution_context"}),
            ]
        )
        async with httpx.AsyncClient() as http_client:
            async with compositor.enter(
                configs={
                    "execution_context": _execution_context_config(),
                    "knowledge": _knowledge_config(query={"mode": "user_query", "value": "release notes"}),
                }
            ) as run:
                knowledge_layer = run.get_layer("knowledge", DifyKnowledgeBaseLayer)
                assert await knowledge_layer.get_tools(http_client=http_client) == []

    asyncio.run(scenario())


def test_workbench_generated_searches_can_repeat_with_independent_records(monkeypatch: pytest.MonkeyPatch):
    requests = []

    async def retrieve(self, **kwargs):
        requests.append(kwargs)
        return DifyKnowledgeRetrieveResponse.model_validate({"results": [], "usage": {}})

    monkeypatch.setattr(DifyKnowledgeBaseClient, "retrieve", retrieve)

    async def scenario():
        compositor = Compositor(
            [
                LayerNode("execution_context", _execution_context_provider()),
                LayerNode("knowledge", _knowledge_provider(), deps={"execution_context": "execution_context"}),
            ]
        )
        async with (
            httpx.AsyncClient() as client,
            compositor.enter(
                configs={
                    "execution_context": _execution_context_config(),
                    "knowledge": _knowledge_config(workbench_run_id="run-1"),
                }
            ) as run,
        ):
            layer = run.get_layer("knowledge", DifyKnowledgeBaseLayer)
            assert not requests
            assert layer.missing_searches == ["Support KB"]
            tool = (await layer.get_tools(http_client=client))[0]
            for query in ["使用人数", "累计使用次数"]:
                await tool.function_schema.call({"set_name": "Support KB", "query": query}, None)
            assert layer.missing_searches == []
            await layer.on_context_resume()
            assert layer.missing_searches == []
            layer.config.workbench_run_id = "run-2"
            await layer.on_context_resume()
            assert layer.missing_searches == ["Support KB"]
        assert [r["query"] for r in requests] == ["使用人数", "累计使用次数"]
        assert all(r["workbench_run_id"] == "run-1" for r in requests)
        assert len({r["workbench_search_id"] for r in requests}) == 2

    asyncio.run(scenario())


def test_workbench_search_pages_all_returned_content_without_retrieving_again(monkeypatch):
    content = "长片段" * 6000 + "最后一条证据"
    calls = []

    async def retrieve(self, **kwargs):
        calls.append(kwargs)
        return DifyKnowledgeRetrieveResponse.model_validate(
            {
                "results": [
                    {"metadata": {"dataset_id": "dataset-1", "document_id": "doc-1"}, "content": content},
                    {"metadata": {"document_id": "doc-2"}, "content": "第二篇文档尾部"},
                ],
            }
        )

    monkeypatch.setattr(DifyKnowledgeBaseClient, "retrieve", retrieve)

    async def scenario():
        compositor = Compositor(
            [
                LayerNode("execution_context", _execution_context_provider()),
                LayerNode("knowledge", _knowledge_provider(), deps={"execution_context": "execution_context"}),
            ]
        )
        async with (
            httpx.AsyncClient() as client,
            compositor.enter(
                configs={
                    "execution_context": _execution_context_config(),
                    "knowledge": _knowledge_config(workbench_run_id="run-1"),
                }
            ) as run,
        ):
            layer = run.get_layer("knowledge", DifyKnowledgeBaseLayer)
            tools = {tool.name: tool for tool in await layer.get_tools(http_client=client)}
            first = json.loads(
                await tools["knowledge_base_search"].function_schema.call(
                    {"set_name": "Support KB", "query": "证据"},
                    None,
                )
            )
            assert not first["complete"]
            joined = first["content"]
            page = first
            while page["next_offset"] is not None:
                page = json.loads(
                    await tools["knowledge_base_read_results"].function_schema.call(
                        {"search_id": first["search_id"], "offset": page["next_offset"]},
                        None,
                    )
                )
                joined += page["content"]
            assert content in joined and "第二篇文档尾部" in joined and "doc-1" in joined
            assert len(joined) == page["total_chars"] and len(calls) == 1
            assert "Invalid offset" in layer._read_search_results(first["search_id"], -1)
            await layer.on_context_resume()
            assert json.loads(layer._read_search_results(first["search_id"])) == first
            layer.config.workbench_run_id = "run-2"
            await layer.on_context_resume()
            assert "expired" in layer._read_search_results(first["search_id"])

    asyncio.run(scenario())


@pytest.mark.parametrize("status,retryable", [(400, False), (403, False), (500, False), (502, True)])
def test_workbench_errors_release_required_search_without_claiming_empty(monkeypatch, status, retryable):
    search_ids = []

    async def retrieve(self, **kwargs):
        search_ids.append(kwargs["workbench_search_id"])
        raise DifyKnowledgeBaseClientError(
            "private provider credential", status_code=status, error_code="provider_failed", retryable=retryable
        )

    monkeypatch.setattr(DifyKnowledgeBaseClient, "retrieve", retrieve)

    async def scenario():
        compositor = Compositor(
            [
                LayerNode("execution_context", _execution_context_provider()),
                LayerNode("knowledge", _knowledge_provider(), deps={"execution_context": "execution_context"}),
            ]
        )
        async with (
            httpx.AsyncClient() as client,
            compositor.enter(
                configs={
                    "execution_context": _execution_context_config(),
                    "knowledge": _knowledge_config(workbench_run_id="run-1"),
                }
            ) as run,
        ):
            layer = run.get_layer("knowledge", DifyKnowledgeBaseLayer)
            tool = (await layer.get_tools(http_client=client))[0]
            result = await tool.function_schema.call({"set_name": "Support KB", "query": "资料"}, None)
            assert "failure" in result or "failed" in result
            failed_search = json.loads(result)
            assert failed_search["status"] == "error" and failed_search["search_id"] == search_ids[0]
            assert NO_RESULTS_OBSERVATION not in result and "private provider credential" not in result
            assert layer.missing_searches == [] and layer.runtime_state.searched_set_ids == []

    asyncio.run(scenario())


def test_workbench_document_tools_bind_dataset_and_identity_to_selected_set():
    requests = []

    def handler(request):
        assert request.url.path == "/inner/api/knowledge/documents"
        assert request.headers["X-Inner-Api-Key"] == "inner-secret"
        payload = json.loads(request.content)
        requests.append(payload)
        return httpx.Response(
            200,
            json={
                "operation": payload["operation"],
                "dataset_id": payload["dataset_id"],
                "total": 0,
                "complete": True,
                "scope": "indexed content",
            },
        )

    async def scenario():
        compositor = Compositor(
            [
                LayerNode("execution_context", _execution_context_provider()),
                LayerNode("knowledge", _knowledge_provider(), deps={"execution_context": "execution_context"}),
            ]
        )
        async with (
            httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client,
            compositor.enter(
                configs={
                    "execution_context": _execution_context_config(),
                    "knowledge": _knowledge_config(workbench_run_id="run-1"),
                }
            ) as run,
        ):
            layer = run.get_layer("knowledge", DifyKnowledgeBaseLayer)
            tools = {tool.name: tool for tool in await layer.get_tools(http_client=client)}
            await tools["knowledge_base_list_documents"].function_schema.call({"set_name": "Support KB"}, None)
            await tools["knowledge_base_read_document"].function_schema.call(
                {"set_name": "Support KB", "document_id": "doc-1", "cursor": "next-page"},
                None,
            )
            result = await tools["knowledge_base_list_documents"].function_schema.call({"set_name": "unknown"}, None)
            assert "unknown" in result and len(requests) == 2
            assert layer.missing_searches == []
        assert requests[1]["cursor"] == "next-page" and requests[1]["document_id"] == "doc-1"
        assert all(item["dataset_id"] == "dataset-1" and item["workbench_run_id"] == "run-1" for item in requests)
        assert requests[0]["caller"]["user_id"] == "user-1"
        assert requests[0]["caller"]["tenant_id"] == "tenant-1"

    asyncio.run(scenario())


def test_knowledge_layer_fetches_user_query_sets_on_context_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    seen_requests: list[dict[str, object]] = []

    async def fake_retrieve(self: DifyKnowledgeBaseClient, **kwargs: object) -> DifyKnowledgeRetrieveResponse:
        del self
        seen_requests.append(kwargs)
        return DifyKnowledgeRetrieveResponse.model_validate(
            {
                "results": [
                    {
                        "metadata": {
                            "_source": "knowledge",
                            "dataset_name": "Docs",
                            "document_name": "Release.md",
                            "score": 0.8,
                        },
                        "title": "Release",
                        "files": [],
                        "content": "Version notes",
                        "summary": None,
                    }
                ],
                "usage": {},
            }
        )

    monkeypatch.setattr(DifyKnowledgeBaseClient, "retrieve", fake_retrieve)

    async def scenario() -> None:
        compositor = Compositor(
            [
                LayerNode("execution_context", _execution_context_provider()),
                LayerNode("knowledge", _knowledge_provider(), deps={"execution_context": "execution_context"}),
            ]
        )
        async with compositor.enter(
            configs={
                "execution_context": _execution_context_config(),
                "knowledge": _knowledge_config(query={"mode": "user_query", "value": "release notes"}),
            }
        ) as run:
            knowledge_layer = run.get_layer("knowledge", DifyKnowledgeBaseLayer)
            assert len(seen_requests) == 1
            assert seen_requests[0]["query"] == "release notes"
            assert seen_requests[0]["dataset_ids"] == ["dataset-1"]
            assert knowledge_layer.runtime_state.eager_config_fingerprint
            assert knowledge_layer.runtime_state.eager_results[0].status == "success"
            assert knowledge_layer.user_prompts == [
                "Knowledge retrieval results:\n\n"
                "Set: Support KB\n"
                "Query: release notes\n"
                "Results:\n"
                "1. Title: Release\n"
                "   Dataset: Docs\n"
                "   Document: Release.md\n"
                "   Score: 0.8\n"
                "   Content: Version notes"
            ]
            await knowledge_layer.on_context_resume()
            assert len(seen_requests) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("field_name", "field_value"),
    [
        ("user_id", None),
        ("user_from", None),
        ("app_id", None),
    ],
)
def test_knowledge_layer_fails_fast_when_execution_context_is_missing_required_fields(
    field_name: str,
    field_value: object,
) -> None:
    async def scenario() -> None:
        compositor = Compositor(
            [
                LayerNode("execution_context", _execution_context_provider()),
                LayerNode("knowledge", _knowledge_provider(), deps={"execution_context": "execution_context"}),
            ]
        )
        async with httpx.AsyncClient() as http_client:
            async with compositor.enter(
                configs={
                    "execution_context": _execution_context_config(),
                    "knowledge": _knowledge_config(),
                }
            ) as run:
                execution_context_layer = run.get_layer("execution_context", DifyExecutionContextLayer)
                setattr(execution_context_layer.config, field_name, field_value)
                knowledge_layer = run.get_layer("knowledge", DifyKnowledgeBaseLayer)
                with pytest.raises(ValueError, match=field_name):
                    _ = await knowledge_layer.get_tools(http_client=http_client)

    asyncio.run(scenario())


def test_knowledge_layer_formats_results_and_truncates_observation() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "metadata": {
                            "_source": "knowledge",
                            "dataset_name": "Docs",
                            "document_name": "Guide.md",
                            "score": 0.9,
                        },
                        "title": "Guide",
                        "files": [],
                        "content": "ABCDEFGHIJKL",
                        "summary": None,
                    }
                ],
                "usage": {},
            },
        )

    async def scenario() -> None:
        compositor = Compositor(
            [
                LayerNode("execution_context", _execution_context_provider()),
                LayerNode("knowledge", _knowledge_provider(), deps={"execution_context": "execution_context"}),
            ]
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            async with compositor.enter(
                configs={
                    "execution_context": _execution_context_config(),
                    "knowledge": _knowledge_config(max_result_content_chars=8, max_observation_chars=160),
                }
            ) as run:
                knowledge_layer = run.get_layer("knowledge", DifyKnowledgeBaseLayer)
                tool = (await knowledge_layer.get_tools(http_client=http_client))[0]
                result = await tool.function_schema.call(  # pyright: ignore[reportArgumentType]
                    {"set_name": "Support KB", "query": "reset"},
                    None,  # pyright: ignore[reportArgumentType]
                )
                assert result.startswith("Knowledge base search results:\n1. Title: Guide")
                assert "Dataset: Docs" in result
                assert "Document: Guide.md" in result
                assert "Score: 0.9" in result
                assert "Content: ABCDE..." in result
                assert len(result) <= 160

    asyncio.run(scenario())


def test_knowledge_layer_returns_no_results_observation() -> None:
    async def scenario() -> None:
        compositor = Compositor(
            [
                LayerNode("execution_context", _execution_context_provider()),
                LayerNode("knowledge", _knowledge_provider(), deps={"execution_context": "execution_context"}),
            ]
        )
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={"results": [], "usage": {}}))
        ) as http_client:
            async with compositor.enter(
                configs={
                    "execution_context": _execution_context_config(),
                    "knowledge": _knowledge_config(),
                }
            ) as run:
                knowledge_layer = run.get_layer("knowledge", DifyKnowledgeBaseLayer)
                tool = (await knowledge_layer.get_tools(http_client=http_client))[0]
                result = await tool.function_schema.call(  # pyright: ignore[reportArgumentType]
                    {"set_name": "Support KB", "query": "reset"},
                    None,  # pyright: ignore[reportArgumentType]
                )
                assert result == NO_RESULTS_OBSERVATION

    asyncio.run(scenario())


def test_knowledge_layer_converts_retryable_failures_into_observation() -> None:
    async def scenario() -> None:
        compositor = Compositor(
            [
                LayerNode("execution_context", _execution_context_provider()),
                LayerNode("knowledge", _knowledge_provider(), deps={"execution_context": "execution_context"}),
            ]
        )
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(429, json={"code": "knowledge_rate_limited", "message": "slow down"})
            )
        ) as http_client:
            async with compositor.enter(
                configs={
                    "execution_context": _execution_context_config(),
                    "knowledge": _knowledge_config(),
                }
            ) as run:
                knowledge_layer = run.get_layer("knowledge", DifyKnowledgeBaseLayer)
                tool = (await knowledge_layer.get_tools(http_client=http_client))[0]
                result = await tool.function_schema.call(  # pyright: ignore[reportArgumentType]
                    {"set_name": "Support KB", "query": "reset"},
                    None,  # pyright: ignore[reportArgumentType]
                )
                assert result == TEMPORARY_UNAVAILABLE_OBSERVATION

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "transport_error",
    [
        lambda request: httpx.ReadTimeout("timed out", request=request),
        lambda request: httpx.ConnectError("connection failed", request=request),
    ],
)
def test_knowledge_layer_converts_retryable_transport_failures_into_observation(transport_error) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise transport_error(request)

    async def scenario() -> None:
        compositor = Compositor(
            [
                LayerNode("execution_context", _execution_context_provider()),
                LayerNode("knowledge", _knowledge_provider(), deps={"execution_context": "execution_context"}),
            ]
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            async with compositor.enter(
                configs={
                    "execution_context": _execution_context_config(),
                    "knowledge": _knowledge_config(),
                }
            ) as run:
                knowledge_layer = run.get_layer("knowledge", DifyKnowledgeBaseLayer)
                tool = (await knowledge_layer.get_tools(http_client=http_client))[0]
                result = await tool.function_schema.call(  # pyright: ignore[reportArgumentType]
                    {"set_name": "Support KB", "query": "reset"},
                    None,  # pyright: ignore[reportArgumentType]
                )
                assert result == TEMPORARY_UNAVAILABLE_OBSERVATION

    asyncio.run(scenario())


def test_knowledge_layer_raises_non_retryable_client_errors() -> None:
    async def scenario() -> None:
        compositor = Compositor(
            [
                LayerNode("execution_context", _execution_context_provider()),
                LayerNode("knowledge", _knowledge_provider(), deps={"execution_context": "execution_context"}),
            ]
        )
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(403, json={"code": "dataset_tenant_mismatch", "message": "forbidden"})
            )
        ) as http_client:
            async with compositor.enter(
                configs={
                    "execution_context": _execution_context_config(),
                    "knowledge": _knowledge_config(),
                }
            ) as run:
                knowledge_layer = run.get_layer("knowledge", DifyKnowledgeBaseLayer)
                tool = (await knowledge_layer.get_tools(http_client=http_client))[0]
                with pytest.raises(DifyKnowledgeBaseClientError) as exc_info:
                    await tool.function_schema.call(  # pyright: ignore[reportArgumentType]
                        {"set_name": "Support KB", "query": "reset"},
                        None,  # pyright: ignore[reportArgumentType]
                    )
                assert exc_info.value.status_code == 403

    asyncio.run(scenario())


def test_knowledge_layer_raises_for_malformed_success_responses() -> None:
    async def scenario() -> None:
        compositor = Compositor(
            [
                LayerNode("execution_context", _execution_context_provider()),
                LayerNode("knowledge", _knowledge_provider(), deps={"execution_context": "execution_context"}),
            ]
        )
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={"bad": []}))
        ) as http_client:
            async with compositor.enter(
                configs={
                    "execution_context": _execution_context_config(),
                    "knowledge": _knowledge_config(),
                }
            ) as run:
                knowledge_layer = run.get_layer("knowledge", DifyKnowledgeBaseLayer)
                tool = (await knowledge_layer.get_tools(http_client=http_client))[0]
                with pytest.raises(DifyKnowledgeBaseClientError) as exc_info:
                    await tool.function_schema.call(  # pyright: ignore[reportArgumentType]
                        {"set_name": "Support KB", "query": "reset"},
                        None,  # pyright: ignore[reportArgumentType]
                    )
                assert exc_info.value.error_code == "invalid_response"
                assert exc_info.value.retryable is False

    asyncio.run(scenario())


def test_knowledge_layer_sends_execution_context_and_static_config_to_inner_api() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        assert request.headers["X-Inner-Api-Key"] == "inner-secret"
        assert payload["caller"] == {
            "tenant_id": "tenant-1",
            "user_id": "user-1",
            "app_id": "app-1",
            "user_from": "account",
            "invoke_from": "web-app",
        }
        assert payload["dataset_ids"] == ["dataset-1", "dataset-2"]
        assert payload["query"] == "reset"
        assert payload["retrieval"]["top_k"] == 2
        assert payload["metadata_filtering"] == {
            "mode": "manual",
            "conditions": {
                "logical_operator": "and",
                "conditions": [
                    {
                        "name": "category",
                        "comparison_operator": "contains",
                        "value": "auth",
                    }
                ],
            },
        }
        return httpx.Response(200, json={"results": [], "usage": {}})

    async def scenario() -> None:
        compositor = Compositor(
            [
                LayerNode("execution_context", _execution_context_provider()),
                LayerNode("knowledge", _knowledge_provider(), deps={"execution_context": "execution_context"}),
            ]
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            async with compositor.enter(
                configs={
                    "execution_context": _execution_context_config(),
                    "knowledge": _knowledge_config(
                        dataset_ids=["dataset-1", "dataset-2"],
                        retrieval={"mode": "multiple", "top_k": 2},
                        metadata_filtering={
                            "mode": "manual",
                            "conditions": {
                                "logical_operator": "and",
                                "conditions": [
                                    {
                                        "name": "category",
                                        "comparison_operator": "contains",
                                        "value": "auth",
                                    }
                                ],
                            },
                        },
                    ),
                }
            ) as run:
                knowledge_layer = run.get_layer("knowledge", DifyKnowledgeBaseLayer)
                tool = (await knowledge_layer.get_tools(http_client=http_client))[0]
                result = await tool.function_schema.call(  # pyright: ignore[reportArgumentType]
                    {"set_name": "Support KB", "query": "reset"},
                    None,  # pyright: ignore[reportArgumentType]
                )
                assert result == NO_RESULTS_OBSERVATION

    asyncio.run(scenario())
