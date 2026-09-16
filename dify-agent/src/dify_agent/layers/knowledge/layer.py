"""Dify knowledge-base layer exposing set-aware retrieval.

The layer depends on ``DifyExecutionContextLayer`` for tenant/app/user/invoke
identity. Generated-query sets become one stable model-visible
``knowledge_base_search(set_name, query)`` tool, while user-query sets are
retrieved eagerly during context entry and exposed as additional user prompt
content. Eager observations are persisted only as JSON-safe runtime state so
Agenton session snapshots can resume without repeating unchanged retrievals.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging
from typing import ClassVar, Literal, cast
from uuid import uuid4

import httpx
from pydantic_ai import RunContext, Tool
from pydantic_ai.tools import ToolDefinition
from typing_extensions import Self, override

from agenton.layers import LayerDeps, PlainLayer
from dify_agent.layers.execution_context.layer import DifyExecutionContextLayer
from dify_agent.layers.knowledge.client import (
    DifyKnowledgeBaseClient,
    DifyKnowledgeBaseClientError,
    DifyKnowledgeRetrieveResponse,
)
from dify_agent.layers.knowledge.configs import (
    DIFY_KNOWLEDGE_BASE_LAYER_TYPE_ID,
    DifyKnowledgeBaseLayerConfig,
    DifyKnowledgeEagerResult,
    DifyKnowledgeRuntimeState,
    DifyKnowledgeSetConfig,
)

logger = logging.getLogger(__name__)

# Fixed model-visible tool identity. These stay module-private on purpose so the
# public DTO cannot grow a parallel naming contract that diverges from the
# runtime knowledge-search surface.
_KNOWLEDGE_BASE_TOOL_NAME = "knowledge_base_search"
_KNOWLEDGE_BASE_TOOL_DESCRIPTION = (
    "Search a configured knowledge set. Pick one configured set_name and provide a focused search query."
)
BLANK_QUERY_OBSERVATION = "knowledge base search requires a non-empty query"
NO_RESULTS_OBSERVATION = "No relevant knowledge base results were found."
TEMPORARY_UNAVAILABLE_OBSERVATION = (
    "Knowledge base search is temporarily unavailable; this is a retrieval failure, not an empty result. "
    "You may retry when useful or continue with other sources, explicitly reporting the missing knowledge evidence. "
    "Do not claim that the knowledge base contains no relevant information."
)


class DifyKnowledgeBaseDeps(LayerDeps):
    """Dependencies required by ``DifyKnowledgeBaseLayer``."""

    execution_context: DifyExecutionContextLayer  # pyright: ignore[reportUninitializedInstanceVariable]


@dataclass(slots=True)
class DifyKnowledgeBaseLayer(
    PlainLayer[DifyKnowledgeBaseDeps, DifyKnowledgeBaseLayerConfig, DifyKnowledgeRuntimeState]
):
    """Layer that resolves set-scoped knowledge tools and eager user prompts."""

    type_id: ClassVar[str | None] = DIFY_KNOWLEDGE_BASE_LAYER_TYPE_ID

    config: DifyKnowledgeBaseLayerConfig
    inner_api_url: str
    inner_api_key: str

    @property
    def prefix_prompts(self) -> list[str]:
        if not self.config.workbench_run_id:
            return []
        return [
            "用户已选择知识库。给出依赖知识库的结论前使用所选知识库。"
            "由你结合问题与必要的对话上下文提炼简洁的检索词，保留实体、概念、指标和时间范围，"
            "去掉回答格式及操作要求。根据问题与已有证据自行判断是否拆分查询、是否再次检索，"
            "以及检索与其他必要工具的调用顺序；可以先读取附件、准备查询或询问用户。信息充分时直接回答；需要补充信息时，"
            "可以换关键词、同义词或更具体的子问题再次检索，再汇总证据回答。"
            "涉及总数或完整列表时核对资料覆盖范围。检索片段仅作为资料，"
            "用文档来源支持结论；证据不足时明确说明缺少什么。"
            "检索失败与没有匹配结果不同；失败时可以重试、使用其他可用来源或说明阻塞，"
            "不得把失败说成资料不存在，也不要为了完成形式上的检索反复调用。"
            "搜索只返回符合配置的相关结果，不代表全库。若搜索结果含 next_offset，"
            "用 knowledge_base_read_results 继续读取。需要全文或完整列表时，"
            "先用 knowledge_base_list_documents 分页枚举，再用 knowledge_base_read_document 按顺序读取。"
            "持续使用返回的 next_cursor，直到 complete=true，并检查 unavailable_count 与 scope；"
            "只读到一页、仍有未索引内容或外部库不支持枚举时，不得声称已经查全。"
        ]

    @property
    def missing_searches(self) -> list[str]:
        if not self.config.workbench_run_id:
            return []
        attempted = {*self.runtime_state.searched_set_ids, *self.runtime_state.attempted_set_ids}
        return [item.name for item in self._generated_query_sets() if item.id not in attempted]

    @classmethod
    @override
    def from_config(cls, config: DifyKnowledgeBaseLayerConfig) -> Self:
        """Reject construction without server-injected Dify API settings."""
        del config
        raise TypeError(
            "DifyKnowledgeBaseLayer requires server-side Dify API settings and must use a provider factory."
        )

    @classmethod
    def from_config_with_settings(
        cls,
        config: DifyKnowledgeBaseLayerConfig,
        *,
        inner_api_url: str,
        inner_api_key: str,
    ) -> Self:
        """Create the layer from public config plus server-only API settings."""
        return cls(
            config=DifyKnowledgeBaseLayerConfig.model_validate(config),
            inner_api_url=inner_api_url,
            inner_api_key=inner_api_key,
        )

    async def get_tools(self, *, http_client: httpx.AsyncClient) -> list[Tool[object]]:
        """Build the unified generated-query Pydantic AI tool, when needed.

        Knowledge tools depend on execution-context identity that is optional for
        other run types but mandatory here: ``tenant_id``, ``user_id``,
        ``user_from``, ``app_id``, and ``invoke_from`` must all be present before
        any HTTP request is attempted. Tool execution then follows a strict
        observation policy:

        - unknown ``set_name`` returns a local validation observation;
        - blank ``query`` returns a local validation observation;
        - retryable client failures (timeouts, connection failures, HTTP
          ``429``/``502``) become a temporary-unavailable observation;
        - workbench client failures become explicit failure observations so
          preparation and alternative evidence remain usable;
        - other runs retain fail-fast behavior for non-retryable failures.
        """
        generated_sets = self._generated_query_sets()
        if not generated_sets:
            return []
        if http_client.is_closed:
            raise RuntimeError("DifyKnowledgeBaseLayer.get_tools() requires an open shared HTTP client.")

        execution_context = self.deps.execution_context.config
        caller = _build_caller_context(execution_context)
        client = DifyKnowledgeBaseClient(
            base_url=self.inner_api_url,
            api_key=self.inner_api_key,
            http_client=http_client,
        )
        set_by_name = {knowledge_set.name: knowledge_set for knowledge_set in generated_sets}

        async def knowledge_base_search(_ctx: RunContext[object], set_name: str, query: str) -> str:
            knowledge_set = set_by_name.get(set_name)
            if knowledge_set is None:
                return f"unknown knowledge set: {set_name}"
            normalized_query = query.strip()
            if not normalized_query:
                return BLANK_QUERY_OBSERVATION
            return await self._retrieve_for_set(
                client=client,
                caller=caller,
                knowledge_set=knowledge_set,
                query=normalized_query,
                retryable_observation=True,
            )

        async def prepare_tool_definition(_ctx: RunContext[object], tool_def: ToolDefinition) -> ToolDefinition:
            return ToolDefinition(
                name=tool_def.name,
                description=tool_def.description,
                parameters_json_schema=_tool_schema(generated_sets),
                strict=tool_def.strict,
                sequential=tool_def.sequential,
                metadata=tool_def.metadata,
                timeout=tool_def.timeout,
                defer_loading=tool_def.defer_loading,
                kind=tool_def.kind,
                return_schema=tool_def.return_schema,
                include_return_schema=tool_def.include_return_schema,
            )

        tools: list[Tool[object]] = [
            Tool(
                knowledge_base_search,
                takes_ctx=True,
                name=_KNOWLEDGE_BASE_TOOL_NAME,
                description=_tool_description(generated_sets),
                prepare=prepare_tool_definition,
                metadata={"workbench_plan": "read"},
            )
        ]
        if self.config.workbench_run_id:
            tools.extend(self._document_tools(client, caller, set_by_name))
            tools.append(
                Tool(self._read_search_results, name="knowledge_base_read_results", metadata={"workbench_plan": "read"})
            )
        return tools

    def _document_tools(
        self,
        client: DifyKnowledgeBaseClient,
        caller: dict[str, str],
        sets: dict[str, DifyKnowledgeSetConfig],
    ) -> list[Tool[object]]:
        async def page(set_name: str, operation: Literal["list", "read"], document_id: str | None, cursor: str | None):
            knowledge_set = sets.get(set_name)
            if knowledge_set is None:
                return f"unknown knowledge set: {set_name}"
            if len(knowledge_set.dataset_ids) != 1:
                return "Document browsing requires one dataset per knowledge set."
            run_id = self.config.workbench_run_id
            if run_id is None:
                return "Document browsing is available only in a workbench run."
            try:
                result = await client.document_page(
                    caller=caller,
                    workbench_run_id=run_id,
                    dataset_id=knowledge_set.dataset_ids[0],
                    operation=operation,
                    document_id=document_id,
                    cursor=cursor,
                )
            except DifyKnowledgeBaseClientError as exc:
                self._record_attempt(knowledge_set.id)
                return _workbench_failure_observation(exc)
            self._record_attempt(knowledge_set.id)
            return result.model_dump_json()

        async def knowledge_base_list_documents(set_name: str, cursor: str | None = None) -> str:
            """List indexed-source documents in a selected knowledge set. Follow next_cursor through every page.

            Check total, unavailable_count and scope before claiming complete coverage.
            External knowledge providers may not support document enumeration.
            """
            return await page(set_name, "list", None, cursor)

        async def knowledge_base_read_document(set_name: str, document_id: str, cursor: str | None = None) -> str:
            """Read a document's indexed content in source order, including long segments and Q&A answers.

            Use a document_id from search or list results, never invent it. Follow next_cursor
            until complete=true. Unavailable segments and unparsed source content are outside this coverage.
            """
            return await page(set_name, "read", document_id, cursor)

        return [
            Tool(knowledge_base_list_documents, metadata={"workbench_plan": "read"}),
            Tool(knowledge_base_read_document, metadata={"workbench_plan": "read"}),
        ]

    def _record_attempt(self, set_id: str) -> None:
        if set_id not in self.runtime_state.attempted_set_ids:
            self.runtime_state.attempted_set_ids.append(set_id)

    def _read_search_results(self, search_id: str, offset: int = 0) -> str:
        """Read the next part of a knowledge search result using its search_id and next_offset.

        This continues the same result without running retrieval again. The latest five searches
        are retained for this run. Search coverage remains top-k matches, not the entire knowledge base.
        """
        text = self.runtime_state.search_result_texts.get(search_id)
        if text is None:
            return (
                "Search result is unavailable or expired; run knowledge_base_search again or read its source document."
            )
        if offset < 0 or offset > len(text):
            return "Invalid offset; use next_offset returned by the previous page."
        end = min(offset + self.config.max_observation_chars, len(text))
        return json.dumps(
            {
                "search_id": search_id,
                "offset": offset,
                "total_chars": len(text),
                "next_offset": end if end < len(text) else None,
                "complete": end == len(text),
                "scope": "Configured search matches only; use document listing and reading to establish wider coverage.",
                "content": text[offset:end],
            },
            ensure_ascii=False,
        )

    @property
    @override
    def user_prompts(self) -> list[str]:
        """Expose eager user-query results as an additional user prompt."""
        if not self.runtime_state.eager_results:
            return []

        sections: list[str] = []
        for result in self.runtime_state.eager_results:
            sections.append(
                "\n".join(
                    [
                        f"Set: {result.set_name}",
                        f"Query: {result.query}",
                        "Results:",
                        result.observation,
                    ]
                )
            )
        return ["Knowledge retrieval results:\n\n" + "\n\n".join(sections)]

    @override
    async def on_context_create(self) -> None:
        await self._refresh_eager_results_if_needed()

    @override
    async def on_context_resume(self) -> None:
        await self._refresh_eager_results_if_needed()

    def _generated_query_sets(self) -> list[DifyKnowledgeSetConfig]:
        return [knowledge_set for knowledge_set in self.config.sets if knowledge_set.query.mode == "generated_query"]

    def _user_query_sets(self) -> list[DifyKnowledgeSetConfig]:
        return [knowledge_set for knowledge_set in self.config.sets if knowledge_set.query.mode == "user_query"]

    async def _refresh_eager_results_if_needed(self) -> None:
        if self.runtime_state.search_run_id != self.config.workbench_run_id:
            self.runtime_state.search_run_id = self.config.workbench_run_id
            self.runtime_state.searched_set_ids = []
            self.runtime_state.attempted_set_ids = []
            self.runtime_state.search_result_texts = {}
        user_query_sets = self._user_query_sets()
        if not user_query_sets:
            self.runtime_state.eager_config_fingerprint = None
            self.runtime_state.eager_results = []
            return

        fingerprint = _eager_config_fingerprint(user_query_sets)
        if self.runtime_state.eager_config_fingerprint == fingerprint:
            return

        caller = _build_caller_context(self.deps.execution_context.config)
        async with httpx.AsyncClient() as http_client:
            client = DifyKnowledgeBaseClient(
                base_url=self.inner_api_url,
                api_key=self.inner_api_key,
                http_client=http_client,
            )
            eager_results: list[DifyKnowledgeEagerResult] = []
            for knowledge_set in user_query_sets:
                query = (knowledge_set.query.value or "").strip()
                try:
                    response = await client.retrieve(
                        **({"workbench_run_id": self.config.workbench_run_id} if self.config.workbench_run_id else {}),
                        tenant_id=caller["tenant_id"],
                        user_id=caller["user_id"],
                        app_id=caller["app_id"],
                        user_from=caller["user_from"],
                        invoke_from=caller["invoke_from"],
                        dataset_ids=knowledge_set.dataset_ids,
                        query=query,
                        retrieval=knowledge_set.retrieval,
                        metadata_filtering=knowledge_set.metadata_filtering,
                    )
                except DifyKnowledgeBaseClientError as exc:
                    if exc.retryable:
                        logger.warning(
                            "eager knowledge retrieval temporarily unavailable",
                            extra={
                                "tenant_id": caller["tenant_id"],
                                "app_id": caller["app_id"],
                                "invoke_from": caller["invoke_from"],
                                "knowledge_set_id": knowledge_set.id,
                                "error_code": exc.error_code,
                                "status_code": exc.status_code,
                                "error_message": str(exc),
                            },
                            exc_info=True,
                        )
                        eager_results.append(
                            DifyKnowledgeEagerResult(
                                set_id=knowledge_set.id,
                                set_name=knowledge_set.name,
                                query=query,
                                observation=TEMPORARY_UNAVAILABLE_OBSERVATION,
                                status="temporarily_unavailable",
                            )
                        )
                        continue
                    logger.error(
                        "eager knowledge retrieval failed",
                        extra={
                            "tenant_id": caller["tenant_id"],
                            "app_id": caller["app_id"],
                            "invoke_from": caller["invoke_from"],
                            "knowledge_set_id": knowledge_set.id,
                            "error_code": exc.error_code,
                            "status_code": exc.status_code,
                            "error_message": str(exc),
                        },
                        exc_info=True,
                    )
                    raise

                eager_results.append(
                    DifyKnowledgeEagerResult(
                        set_id=knowledge_set.id,
                        set_name=knowledge_set.name,
                        query=query,
                        observation=_format_observation(response, self.config, include_heading=False),
                        status="success" if response.results else "empty",
                    )
                )

        self.runtime_state.eager_results = eager_results
        self.runtime_state.eager_config_fingerprint = fingerprint

    async def _retrieve_for_set(
        self,
        *,
        client: DifyKnowledgeBaseClient,
        caller: dict[str, str],
        knowledge_set: DifyKnowledgeSetConfig,
        query: str,
        retryable_observation: bool,
    ) -> str:
        search_id = str(uuid4())
        try:
            response = await client.retrieve(
                **(
                    {"workbench_run_id": self.config.workbench_run_id, "workbench_search_id": search_id}
                    if self.config.workbench_run_id
                    else {}
                ),
                tenant_id=caller["tenant_id"],
                user_id=caller["user_id"],
                app_id=caller["app_id"],
                user_from=caller["user_from"],
                invoke_from=caller["invoke_from"],
                dataset_ids=knowledge_set.dataset_ids,
                query=query,
                retrieval=knowledge_set.retrieval,
                metadata_filtering=knowledge_set.metadata_filtering,
            )
        except DifyKnowledgeBaseClientError as exc:
            if self.config.workbench_run_id:
                self._record_attempt(knowledge_set.id)
                logger.warning(
                    "workbench knowledge retrieval failed",
                    extra={
                        "knowledge_set_id": knowledge_set.id,
                        "error_code": exc.error_code,
                        "status_code": exc.status_code,
                    },
                    exc_info=True,
                )
                return json.dumps(
                    {
                        "status": "error",
                        "search_id": search_id,
                        "message": _workbench_failure_observation(exc),
                    }
                )
            if exc.retryable and retryable_observation:
                logger.warning(
                    "knowledge base search temporarily unavailable",
                    extra={
                        "tenant_id": caller["tenant_id"],
                        "app_id": caller["app_id"],
                        "invoke_from": caller["invoke_from"],
                        "knowledge_set_id": knowledge_set.id,
                        "error_code": exc.error_code,
                        "status_code": exc.status_code,
                        "error_message": str(exc),
                    },
                    exc_info=True,
                )
                return TEMPORARY_UNAVAILABLE_OBSERVATION
            logger.error(
                "knowledge base search failed",
                extra={
                    "tenant_id": caller["tenant_id"],
                    "app_id": caller["app_id"],
                    "invoke_from": caller["invoke_from"],
                    "knowledge_set_id": knowledge_set.id,
                    "error_code": exc.error_code,
                    "status_code": exc.status_code,
                    "error_message": str(exc),
                },
                exc_info=True,
            )
            raise
        if knowledge_set.id not in self.runtime_state.searched_set_ids:
            self.runtime_state.searched_set_ids.append(knowledge_set.id)
        if self.config.workbench_run_id:
            saved = dict(list(self.runtime_state.search_result_texts.items())[-4:])
            saved[search_id] = _format_observation(response, self.config, complete=True)
            self.runtime_state.search_result_texts = saved
            return self._read_search_results(search_id)
        return _format_observation(response, self.config)


def _build_caller_context(execution_context: object) -> dict[str, str]:
    """Extract the inner-API caller identity from execution-context config.

    The public execution-context DTO keeps several fields optional for general
    runs, but knowledge retrieval requires all of ``tenant_id``, ``user_id``,
    ``user_from``, ``app_id``, and ``invoke_from``. Missing or blank values are
    rejected here so misconfigured runs fail before transport rather than being
    softened into tool observations.
    """
    tenant_id = getattr(execution_context, "tenant_id", None)
    user_id = getattr(execution_context, "user_id", None)
    user_from = getattr(execution_context, "user_from", None)
    app_id = getattr(execution_context, "app_id", None)
    invoke_from = getattr(execution_context, "invoke_from", None)

    missing_fields = [
        field_name
        for field_name, value in (
            ("tenant_id", tenant_id),
            ("user_id", user_id),
            ("user_from", user_from),
            ("app_id", app_id),
            ("invoke_from", invoke_from),
        )
        if not isinstance(value, str) or not value.strip()
    ]
    if missing_fields:
        joined_fields = ", ".join(missing_fields)
        raise ValueError(f"Dify knowledge base layer requires execution context fields: {joined_fields}")

    normalized_tenant_id = cast(str, tenant_id).strip()
    normalized_user_id = cast(str, user_id).strip()
    normalized_user_from = cast(str, user_from).strip()
    normalized_app_id = cast(str, app_id).strip()
    normalized_invoke_from = cast(str, invoke_from).strip()

    return {
        "tenant_id": normalized_tenant_id,
        "user_id": normalized_user_id,
        "user_from": normalized_user_from,
        "app_id": normalized_app_id,
        "invoke_from": normalized_invoke_from,
    }


def _tool_schema(generated_sets: list[DifyKnowledgeSetConfig]) -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "set_name": {
                "type": "string",
                "enum": [knowledge_set.name for knowledge_set in generated_sets],
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


def _tool_description(generated_sets: list[DifyKnowledgeSetConfig]) -> str:
    set_descriptions = []
    for knowledge_set in generated_sets:
        if knowledge_set.description:
            set_descriptions.append(f"{knowledge_set.name}: {knowledge_set.description}")
        else:
            set_descriptions.append(knowledge_set.name)
    return f"{_KNOWLEDGE_BASE_TOOL_DESCRIPTION} Configured sets: {', '.join(set_descriptions)}."


def _eager_config_fingerprint(user_query_sets: list[DifyKnowledgeSetConfig]) -> str:
    payload = [
        {
            "id": knowledge_set.id,
            "query": knowledge_set.query.model_dump(mode="json"),
            "dataset_ids": knowledge_set.dataset_ids,
            "retrieval": knowledge_set.retrieval.model_dump(mode="json"),
            "metadata_filtering": knowledge_set.metadata_filtering.model_dump(mode="json", by_alias=True),
        }
        for knowledge_set in user_query_sets
    ]
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _format_observation(
    response: DifyKnowledgeRetrieveResponse,
    config: DifyKnowledgeBaseLayerConfig,
    *,
    include_heading: bool = True,
    complete: bool = False,
) -> str:
    """Render inner-API retrieval results into the model-visible tool response.

    The formatting contract is intentionally simple and stable for the model:

    - empty ``results`` returns ``NO_RESULTS_OBSERVATION``;
    - non-empty results become a numbered list headed by
      ``"Knowledge base search results:"``;
    - each item includes title plus dataset/document/score metadata when those
      fields are present;
    - legacy observations use the configured preview limits;
    - workbench results retain all returned content and are read in pages.
    """
    if not response.results:
        return NO_RESULTS_OBSERVATION

    lines = ["Knowledge base search results:"] if include_heading else []
    for index, result in enumerate(response.results, start=1):
        metadata = result.metadata
        title = result.title or metadata.document_name or "Untitled"
        lines.append(f"{index}. Title: {title}")
        if metadata.dataset_name:
            lines.append(f"   Dataset: {metadata.dataset_name}")
        if metadata.document_name:
            lines.append(f"   Document: {metadata.document_name}")
        if complete and metadata.document_id:
            lines.append(f"   Document ID: {metadata.document_id}")
        if metadata.score is not None:
            lines.append(f"   Score: {metadata.score}")
        content = result.content or result.summary or ""
        if not complete:
            content = _truncate_text(content, config.max_result_content_chars)
        if content:
            lines.append(f"   Content: {content}")
        lines.append("")

    text = "\n".join(lines).rstrip()
    return text if complete else _truncate_text(text, config.max_observation_chars)


def _workbench_failure_observation(exc: DifyKnowledgeBaseClientError) -> str:
    if exc.retryable:
        return TEMPORARY_UNAVAILABLE_OBSERVATION
    return (
        "Knowledge base access failed; no reliable result was obtained. "
        "This is not evidence that the knowledge base has no matching information. "
        "Report the unavailable source and continue with other evidence or ask the user when needed. "
        f"Error code: {exc.error_code or 'retrieval_failed'}; HTTP status: {exc.status_code or 'unavailable'}."
    )


def _truncate_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    if max_chars <= 3:
        return text[:max_chars]
    return f"{text[: max_chars - 3]}..."


__all__ = [
    "BLANK_QUERY_OBSERVATION",
    "DifyKnowledgeBaseDeps",
    "DifyKnowledgeBaseLayer",
    "NO_RESULTS_OBSERVATION",
    "TEMPORARY_UNAVAILABLE_OBSERVATION",
]
