"""Runtime execution for one scheduled Dify Agent run.

The runner is storage-agnostic: it normalizes the public Dify composition into
Agenton's graph/config split and executes one model run after the ``on_exit``
policy is validated:

- model runs: enter a fresh ``CompositorRun`` (or resume one from a snapshot),
  pass the current Dify system prompts as run-level instructions, run
  pydantic-ai with either the current ``run.user_prompts`` or deferred external
  tool results, emit stream events with bounded text-delta coalescing and
  agent-message annotations, apply request-level ``on_exit`` signals, and publish
  a terminal success or failure event;
The Pydantic AI model is resolved from the active Agenton layer named by
``DIFY_AGENT_MODEL_LAYER_ID``. An optional history layer contributes stored
message history only through session state. Once pydantic-ai binds and builds
messages in the run capture, every terminal outcome replaces that state with the
captured messages after transient instructions are cleared; a failure or
cancellation before the capture contains messages preserves the restored state.
This preserves compaction rewrites and interrupted partial messages without
saving current system prompts. An optional structured output layer named by
``DIFY_AGENT_OUTPUT_LAYER_ID`` is read after entry and resolved into an output
contract whose type both exposes the output schema to the model and performs
runtime JSON Schema validation through custom Pydantic hooks. When the ask-human
layer is active, the runtime also allows ``DeferredToolRequests`` output and
publishes that deferred request through the normal ``run_succeeded`` event as
``deferred_tool_call`` instead of a final ``output``. Invalid structured outputs
or invalid deferred-tool behavior still trigger normal retries/failures before
Dify Agent emits success. Layers still never own the FastAPI lifespan-owned
plugin daemon or Dify API inner HTTP clients. Successful terminal events contain
both the JSON-safe final output or deferred tool call and the session snapshot;
there are no separate output or snapshot events to correlate.
"""

import asyncio
from collections import Counter
from collections.abc import AsyncIterable, Callable, Mapping
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any, Literal, Protocol, cast, runtime_checkable

import httpx
from graphon.model_runtime.entities.llm_entities import LLMUsage
from pydantic import JsonValue, TypeAdapter
from pydantic_ai import RunContext, capture_run_messages
from pydantic_ai.exceptions import ModelHTTPError, UsageLimitExceeded
from pydantic_ai.messages import (
    AgentStreamEvent,
    FunctionToolResultEvent,
    ModelResponse,
    PartDeltaEvent,
    PartEndEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
)
from pydantic_ai.output import OutputSpec
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults
from pydantic_ai.usage import UsageLimits

from agenton.compositor import CompositorSessionSnapshot, LayerConfigInput, LayerProviderInput
from agenton.layers.types import PydanticAITool
from dify_agent.layers.ask_human.layer import get_ask_human_layer, validate_ask_human_layer_composition
from dify_agent.layers.dify_core_tools.layer import DifyCoreToolsLayer
from dify_agent.layers.dify_plugin.llm_layer import DifyPluginLLMLayer
from dify_agent.layers.dify_plugin.tools_layer import DifyPluginToolsLayer
from dify_agent.layers.knowledge.client import DifyKnowledgeBaseClientError
from dify_agent.layers.knowledge.layer import DifyKnowledgeBaseLayer
from dify_agent.layers.workbench_files import WorkbenchFilesLayer
from dify_agent.protocol.schemas import (
    DIFY_AGENT_MODEL_LAYER_ID,
    AgentRunUsage,
    CreateRunRequest,
    DeferredToolCallPayload,
    RunFailureType,
    normalize_composition,
)
from dify_agent.runtime.agent_factory import create_agent, normalize_user_input
from dify_agent.runtime.agenton_validation import is_agenton_enter_validation_runtime_error
from dify_agent.runtime.compaction import build_compaction_capability
from dify_agent.runtime.compositor_factory import build_pydantic_ai_compositor, create_default_layer_providers
from dify_agent.runtime.event_coalescer import (
    DEFAULT_TEXT_DELTA_FLUSH_INTERVAL_SECONDS,
    DEFAULT_TEXT_DELTA_MAX_CHARS,
    coalesce_agent_stream_events,
)
from dify_agent.runtime.event_sink import (
    RunEventSink,
    emit_pydantic_ai_event,
    emit_run_failed,
    emit_run_started,
    emit_run_succeeded,
)
from dify_agent.runtime.history import (
    get_history_layer,
    replace_run_history,
    validate_history_layer_composition,
)
from dify_agent.runtime.layer_exit_signals import apply_layer_exit_signals, validate_layer_exit_signals
from dify_agent.runtime.output_type import resolve_run_output_contract, validate_output_layer_composition
from dify_agent.runtime.user_prompt_validation import EMPTY_USER_PROMPTS_ERROR, has_non_blank_user_prompt
from dify_agent.runtime.workbench_model_idle import WorkbenchModelIdleCapability
from dify_agent.runtime_backend import BindingLostError

_AGENT_OUTPUT_ADAPTER = TypeAdapter(object)
_MAX_AGENT_STEPS_PER_RUN = 500
DEFAULT_AGENT_RUN_TIMEOUT_SECONDS = 60 * 60


@runtime_checkable
class _HasUsage(Protocol):
    usage: object


@runtime_checkable
class _HasInputTokens(Protocol):
    input_tokens: int | None


@runtime_checkable
class _HasOutputTokens(Protocol):
    output_tokens: int | None


@runtime_checkable
class _HasTotalTokens(Protocol):
    total_tokens: int | None


@runtime_checkable
class _HasAccumulatedUsage(Protocol):
    @property
    def accumulated_usage(self) -> LLMUsage | None: ...


class AgentRunValidationError(ValueError):
    """Raised when a run request is valid JSON but cannot execute."""


def _run_failed_error_payload(exc: Exception) -> tuple[str, RunFailureType | None, str | None]:
    """Return the public failed-run error text, type, and structured reason."""
    message = str(exc) or type(exc).__name__
    reason: str | None = None

    if isinstance(exc, UsageLimitExceeded):
        return message, RunFailureType.AGENT_RUN_LIMIT_EXCEEDED, None

    if isinstance(exc, BindingLostError):
        return message, None, "binding_lost"

    if isinstance(exc, ModelHTTPError):
        body = exc.body
        if isinstance(body, Mapping):
            body_message = body.get("message")
            if isinstance(body_message, str) and body_message:
                message = body_message

            error_type = body.get("error_type")
            if isinstance(error_type, str) and error_type:
                reason = error_type

        if reason is None and exc.status_code == 429:
            reason = "InvokeRateLimitError"

    if isinstance(exc, DifyKnowledgeBaseClientError):
        reason = exc.error_code or "DifyKnowledgeBaseClientError"

    return message, None, reason


def _has_model_layer(request: CreateRunRequest) -> bool:
    """Return whether the public composition includes the reserved model layer."""
    return any(layer.name == DIFY_AGENT_MODEL_LAYER_ID for layer in request.composition.layers)


def _extract_agent_message_delta(event: AgentStreamEvent) -> str | None:
    """Return agent-message text content from Pydantic AI stream events."""
    if isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
        return event.delta.content_delta
    if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart):
        return event.part.content
    return None


@dataclass(slots=True)
class RunSuccessOutcome:
    """Normalized successful runner output before event emission."""

    result_kind: Literal["output", "deferred_tool_call"]
    output: JsonValue | None
    deferred_tool_call: DeferredToolCallPayload | None
    session_snapshot: CompositorSessionSnapshot
    usage: AgentRunUsage | None


class AgentRunRunner:
    """Executes one run and writes only public run events to its sink."""

    sink: RunEventSink

    request: CreateRunRequest
    run_id: str
    layer_providers: tuple[LayerProviderInput, ...]
    plugin_daemon_http_client: httpx.AsyncClient
    dify_api_http_client: httpx.AsyncClient
    is_cancelled: Callable[[], bool]
    run_timeout_seconds: float
    stream_text_delta_coalescing_enabled: bool
    stream_text_delta_flush_interval_seconds: float
    stream_text_delta_max_chars: int
    _terminal_session_snapshot: CompositorSessionSnapshot | None
    _terminal_usage: AgentRunUsage | None

    def __init__(
        self,
        *,
        sink: RunEventSink,
        request: CreateRunRequest,
        run_id: str,
        plugin_daemon_http_client: httpx.AsyncClient,
        dify_api_http_client: httpx.AsyncClient,
        layer_providers: tuple[LayerProviderInput, ...] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
        run_timeout_seconds: float = DEFAULT_AGENT_RUN_TIMEOUT_SECONDS,
        stream_text_delta_coalescing_enabled: bool = True,
        stream_text_delta_flush_interval_seconds: float = DEFAULT_TEXT_DELTA_FLUSH_INTERVAL_SECONDS,
        stream_text_delta_max_chars: int = DEFAULT_TEXT_DELTA_MAX_CHARS,
    ) -> None:
        if stream_text_delta_flush_interval_seconds <= 0:
            raise ValueError("stream_text_delta_flush_interval_seconds must be positive")
        if stream_text_delta_max_chars <= 0:
            raise ValueError("stream_text_delta_max_chars must be positive")
        self.sink = sink
        self.request = request
        self.run_id = run_id
        self.plugin_daemon_http_client = plugin_daemon_http_client
        self.dify_api_http_client = dify_api_http_client
        self.layer_providers = layer_providers if layer_providers is not None else create_default_layer_providers()
        self.is_cancelled = is_cancelled or (lambda: False)
        self.run_timeout_seconds = run_timeout_seconds
        self.stream_text_delta_coalescing_enabled = stream_text_delta_coalescing_enabled
        self.stream_text_delta_flush_interval_seconds = stream_text_delta_flush_interval_seconds
        self.stream_text_delta_max_chars = stream_text_delta_max_chars
        self._terminal_session_snapshot = None
        self._terminal_usage = None

    @property
    def terminal_session_snapshot(self) -> CompositorSessionSnapshot | None:
        """Return the snapshot captured after the current compositor context exited."""
        return self._terminal_session_snapshot

    @property
    def terminal_usage(self) -> AgentRunUsage | None:
        """Return usage accumulated before the current run reached any terminal state."""
        return self._terminal_usage

    async def run(self) -> None:
        """Execute the run and emit the documented event sequence."""
        self._terminal_session_snapshot = None
        self._terminal_usage = None
        if self.is_cancelled():
            return
        _ = await emit_run_started(self.sink, run_id=self.run_id)

        try:
            outcome = await self._run_agent()
        except Exception as exc:
            if self.is_cancelled():
                return
            message, error_type, reason = _run_failed_error_payload(exc)
            finalization = await emit_run_failed(
                self.sink,
                run_id=self.run_id,
                error=message,
                error_type=error_type,
                reason=reason,
                session_snapshot=self._terminal_session_snapshot,
                usage=self._terminal_usage,
            )
            if finalization.applied:
                raise
            return

        if self.is_cancelled():
            return
        _ = await emit_run_succeeded(
            self.sink,
            run_id=self.run_id,
            **(
                {"output": outcome.output}
                if outcome.result_kind == "output"
                else {"deferred_tool_call": outcome.deferred_tool_call}
            ),
            session_snapshot=outcome.session_snapshot,
            usage=outcome.usage,
        )

    async def _run_agent(self) -> RunSuccessOutcome:
        """Run the normalized request through the model path.

        Known request-shaped Agenton enter-time failures are normalized to
        ``AgentRunValidationError``. That includes the existing small class of
        enter-time ``RuntimeError`` values reported by Agenton plus
        layer-construction or snapshot-hydration ``ValueError`` failures that
        arise before the run becomes active, such as missing shell settings for a
        requested ``dify.shell`` layer or malformed serialized shell offsets.
        Output/history-layer graph invariants are validated from the public
        composition before entering Agenton so misnamed or extra reserved layers
        never silently degrade. Later runtime failures still propagate as
        execution errors so they become terminal failed runs rather than client
        validation responses. Structured output uses a resolved contract whose
        type itself encodes both the model-facing schema and the runtime
        validation hooks, so invalid model outputs can be corrected before Dify
        Agent emits success.
        """
        try:
            validate_output_layer_composition(self.request.composition)
            validate_history_layer_composition(self.request.composition)
            validate_ask_human_layer_composition(self.request.composition)
            graph_config, layer_configs = normalize_composition(self.request.composition)
            compositor = build_pydantic_ai_compositor(graph_config, providers=self.layer_providers)
            validate_layer_exit_signals(compositor, self.request.on_exit)
        except (KeyError, TypeError, ValueError) as exc:
            raise AgentRunValidationError(str(exc)) from exc

        if not _has_model_layer(self.request):
            raise AgentRunValidationError(f"Missing required '{DIFY_AGENT_MODEL_LAYER_ID}' layer.")
        return await self._run_model(compositor=compositor, layer_configs=layer_configs)

    async def _run_model(
        self,
        *,
        compositor: Any,
        layer_configs: dict[str, LayerConfigInput],
    ) -> RunSuccessOutcome:
        """Run the normal model/deferred-tool path inside an entered Agenton run."""
        entered_run = False
        output: JsonValue | None = None
        deferred_tool_call: DeferredToolCallPayload | None = None
        result_kind: Literal["output", "deferred_tool_call"] | None = None
        usage: AgentRunUsage | None = None
        model: Any = None
        run = None
        try:
            if self.request.rebuild_layers and self.request.deferred_tool_results is not None:
                raise ValueError("Deferred continuations cannot rebuild their layer composition")
            restore_snapshot = None if self.request.rebuild_layers else self.request.session_snapshot
            async with compositor.enter(configs=layer_configs, session_snapshot=restore_snapshot) as run:
                if self.request.rebuild_layers and self.request.session_snapshot is not None:
                    from agenton_collections.layers.pydantic_ai.history import PydanticAIHistoryRuntimeState

                    previous = next(
                        (layer for layer in self.request.session_snapshot.layers if layer.name == "history"), None
                    )
                    history = get_history_layer(run)
                    if previous is not None and history is not None:
                        history.replace_messages(
                            PydanticAIHistoryRuntimeState.model_validate(previous.runtime_state).messages
                        )
                entered_run = True
                apply_layer_exit_signals(run, self.request.on_exit)
                from dify_agent.layers.workbench_control import WorkbenchControlLayer
                from dify_agent.runtime.workbench_control import WorkbenchControlCapability

                control_layer = next(
                    (slot.layer for slot in run.slots.values() if isinstance(slot.layer, WorkbenchControlLayer)), None
                )
                if control_layer is not None:
                    control_layer.http_client = self.dify_api_http_client
                    control_layer.run_id = self.run_id
                    await control_layer.request()
                    await control_layer.resource_request(initialize=True)
                control_capability = WorkbenchControlCapability(control_layer) if control_layer else None
                user_prompts = run.user_prompts
                deferred_tool_results = _resolve_deferred_tool_results(self.request)
                if deferred_tool_results is None and not has_non_blank_user_prompt(user_prompts):
                    raise AgentRunValidationError(EMPTY_USER_PROMPTS_ERROR)
                knowledge_layer = next(
                    (
                        slot.layer
                        for slot in run.slots.values()
                        if isinstance(slot.layer, DifyKnowledgeBaseLayer) and slot.layer.config.workbench_run_id
                    ),
                    None,
                )
                from dify_agent.layers.workbench_mentions import WorkbenchMentionsLayer

                mentions_layer = next(
                    (slot.layer for slot in run.slots.values() if isinstance(slot.layer, WorkbenchMentionsLayer)),
                    None,
                )
                from dify_agent.layers.workbench_activity import WorkbenchActivityLayer
                from dify_agent.runtime.workbench_activity import WorkbenchActivityCapability

                activity_layer = next(
                    (slot.layer for slot in run.slots.values() if isinstance(slot.layer, WorkbenchActivityLayer)),
                    None,
                )
                activity = (
                    WorkbenchActivityCapability(layer=activity_layer, sink=self.sink, run_id=self.run_id)
                    if activity_layer is not None
                    else None
                )
                from dify_agent.layers.shell.argument_compatibility import ShellArgumentCompatibilityCapability
                from dify_agent.layers.shell.layer import DifyShellLayer
                from dify_agent.layers.workbench_files import WorkbenchFilesLayer
                from dify_agent.layers.workbench_activity import result_metadata
                from dify_agent.runtime.workbench_files import WorkbenchFileChanges, WorkbenchFileDeliveryCapability

                files_layer = next(
                    (slot.layer for slot in run.slots.values() if isinstance(slot.layer, WorkbenchFilesLayer)), None
                )
                if files_layer is not None:
                    files_layer.run_id = self.run_id
                from dify_agent.layers.workbench_followups import WorkbenchFollowupsLayer
                from dify_agent.runtime.workbench_followups import WorkbenchFollowupsCapability

                followups_layer = next(
                    (slot.layer for slot in run.slots.values() if isinstance(slot.layer, WorkbenchFollowupsLayer)), None
                )
                followups = (
                    WorkbenchFollowupsCapability(followups_layer, self.dify_api_http_client, self.run_id)
                    if followups_layer is not None
                    else None
                )
                shell_layer = next(
                    (slot.layer for slot in run.slots.values() if isinstance(slot.layer, DifyShellLayer)), None
                )
                shell_arguments = (
                    ShellArgumentCompatibilityCapability() if files_layer is not None or activity is not None else None
                )
                from dify_agent.runtime.workbench_tool_recovery import WorkbenchToolRecoveryCapability
                from dify_agent.runtime.workbench_checkpoint import HistoryCheckpointSink, WorkbenchHistoryCheckpoint

                tool_recovery = (
                    WorkbenchToolRecoveryCapability()
                    if self.request.execution_ticket or files_layer is not None or activity is not None
                    else None
                )
                model_idle = WorkbenchModelIdleCapability() if tool_recovery is not None else None
                checkpoint = (
                    WorkbenchHistoryCheckpoint(
                        sink=self.sink,
                        run_id=self.run_id,
                        seen_ids=followups_layer.runtime_state.seen_ids if followups_layer is not None else None,
                    )
                    if tool_recovery is not None and isinstance(self.sink, HistoryCheckpointSink)
                    else None
                )
                changes = (
                    WorkbenchFileChanges(shell_layer, activity, files_layer)
                    if shell_layer is not None and (files_layer is not None or activity is not None)
                    else None
                )
                if changes is not None:
                    await changes.start()

                async def publish_verified(response: ModelResponse, step: int) -> None:
                    for index, part in enumerate(response.parts):
                        if not isinstance(part, TextPart) or not part.content:
                            continue
                        event = PartStartEvent(index=index, part=part)
                        if activity is not None:
                            await activity.observe(event, run_step=step, text_delta=part.content)
                        await emit_pydantic_ai_event(
                            self.sink, run_id=self.run_id, data=event, agent_message_delta=part.content
                        )

                delivery = (
                    WorkbenchFileDeliveryCapability(
                        files=files_layer,
                        publish=publish_verified,
                        ready=lambda: (
                            not (
                                (knowledge_layer is not None and knowledge_layer.missing_searches)
                                or (mentions_layer is not None and mentions_layer.missing_groups)
                            )
                        ),
                        changes=changes,
                    )
                    if files_layer is not None
                    else None
                )

                async def handle_events(_ctx: RunContext[Any], events: AsyncIterable[AgentStreamEvent]) -> None:
                    published_events = coalesce_agent_stream_events(
                        events,
                        enabled=self.stream_text_delta_coalescing_enabled,
                        flush_interval_seconds=self.stream_text_delta_flush_interval_seconds,
                        max_chars=self.stream_text_delta_max_chars,
                    )
                    async for event in published_events:
                        if self.is_cancelled():
                            raise asyncio.CancelledError
                        if model_idle is not None:
                            model_idle.observe(event)
                        if mentions_layer is not None:
                            mentions_layer.record_event(event)
                        text_delta = _extract_agent_message_delta(event)
                        if delivery is not None and (
                            text_delta is not None
                            or (isinstance(event, PartEndEvent) and isinstance(event.part, TextPart))
                        ):
                            # Release each complete model response only after file-link validation.
                            continue
                        if text_delta is not None and knowledge_layer is not None and knowledge_layer.missing_searches:
                            continue
                        if text_delta is not None and mentions_layer is not None and mentions_layer.missing_groups:
                            continue
                        if activity is not None:
                            await activity.observe(event, run_step=_ctx.run_step, text_delta=text_delta)
                        if changes is not None and isinstance(event, FunctionToolResultEvent):
                            part = event.part
                            explicit = (
                                result_metadata(part.content).get("path")
                                if part.tool_name in {"file_create", "file_edit"}
                                else None
                            )
                            await changes.collect(explicit_path=explicit if isinstance(explicit, str) else None)
                        _ = await emit_pydantic_ai_event(
                            self.sink,
                            run_id=self.run_id,
                            data=event,
                            agent_message_delta=text_delta,
                        )
                        if tool_recovery is not None:
                            tool_recovery.observe(event)

                try:
                    output_contract = resolve_run_output_contract(run)
                    history_layer = get_history_layer(run)
                    message_history = history_layer.message_history if history_layer is not None else None
                    ask_human_layer = get_ask_human_layer(run)
                    from dify_agent.layers.workbench_environment import TOOL_NAME, WorkbenchEnvironmentLayer

                    try:
                        environment_layer = run.get_layer("workbench_environment", WorkbenchEnvironmentLayer)
                    except KeyError:
                        environment_layer = None
                    llm_layer = run.get_layer(DIFY_AGENT_MODEL_LAYER_ID, DifyPluginLLMLayer)
                    compaction = build_compaction_capability(
                        context_window_tokens=llm_layer.config.context_window_tokens,
                        model_settings=llm_layer.config.model_settings,
                        workbench=tool_recovery is not None,
                    )
                    from dify_agent.runtime.workbench_tool_output import WorkbenchToolOutputLimits

                    tool_output = (
                        WorkbenchToolOutputLimits(shell_layer, compaction.target_tokens)
                        if tool_recovery is not None
                        and shell_layer is not None
                        and compaction is not None
                        and compaction.target_tokens is not None
                        else None
                    )
                    if self.request.execution_ticket:
                        from dify_agent.runtime.context_status import WorkbenchContextStatus

                        compaction = WorkbenchContextStatus(
                            compaction=compaction,
                            window_tokens=llm_layer.config.context_window_tokens,
                            sink=self.sink,
                            run_id=self.run_id,
                        )
                    model = llm_layer.get_model(
                        http_client=self.dify_api_http_client,
                        agent_run_id=self.run_id,
                    )
                    tools = await _resolve_run_tools(
                        run,
                        plugin_daemon_http_client=self.plugin_daemon_http_client,
                        dify_api_http_client=self.dify_api_http_client,
                    )
                except (KeyError, TypeError, RuntimeError, ValueError) as exc:
                    raise AgentRunValidationError(str(exc)) from exc

                if deferred_tool_results is not None and history_layer is None:
                    raise AgentRunValidationError(
                        "Deferred tool results require a 'history' layer with prior message history."
                    )

                compact_command = control_layer.runtime_state.control if control_layer is not None else None
                manual_compaction = bool(compact_command and compact_command.get("kind") == "compact")
                continue_after_compaction = bool(
                    manual_compaction and compact_command and compact_command.get("continue_after")
                )
                if manual_compaction and deferred_tool_results is None:
                    from dify_agent.runtime.manual_compaction import compact_history

                    async with asyncio.timeout(self.run_timeout_seconds):
                        output, compact_usage = await compact_history(
                            layer=control_layer,
                            model=model,
                            history=history_layer,
                            checkpoint=checkpoint,
                            sink=self.sink,
                            run_id=self.run_id,
                            window_tokens=llm_layer.config.context_window_tokens,
                        )
                    usage = _serialize_agent_usage(compact_usage)
                    self._terminal_usage = usage
                    result_kind = "output"
                    message_history = history_layer.message_history if history_layer is not None else None
                if not manual_compaction or continue_after_compaction or deferred_tool_results is not None:
                    from dify_agent.runtime.knowledge import require_knowledge_before_answer

                    agent = create_agent(
                        model,
                        tools=tools,
                        **(
                            # Unknown names bypass tool hooks. Keep the SDK's per-name
                            # retry ceiling above any possible model step count; the
                            # capability enforces five consecutive results instead.
                            {"output_retries": 4, "tool_retries": _MAX_AGENT_STEPS_PER_RUN}
                            if tool_recovery is not None
                            else {"output_retries": 2}
                            if mentions_layer is not None or delivery is not None
                            else {}
                        ),
                        output_type=_resolve_agent_output_type(
                            output_contract.output_type,
                            ask_human_layer is not None or environment_layer is not None or control_layer is not None,
                        ),
                    )
                    require_knowledge_before_answer(agent, knowledge_layer)
                    if mentions_layer is not None:
                        mentions_layer.require_before_answer(agent)
                    run_timeout = asyncio.timeout(self.run_timeout_seconds)
                    try:
                        with capture_run_messages() as captured_messages:
                            try:
                                async with run_timeout, model_idle.guard() if model_idle is not None else nullcontext():
                                    result = await agent.run(
                                        None
                                        if deferred_tool_results is not None
                                        else normalize_user_input(user_prompts),
                                        message_history=message_history,
                                        deferred_tool_results=deferred_tool_results,
                                        event_stream_handler=handle_events,
                                        instructions=run.prompts or None,
                                        capabilities=[
                                            capability
                                            for capability in (
                                                followups,
                                                control_capability,
                                                checkpoint,
                                                model_idle,
                                                compaction,
                                                shell_arguments,
                                                tool_output,
                                                tool_recovery,
                                                activity,
                                                delivery,
                                            )
                                            if capability is not None
                                        ],
                                        usage_limits=UsageLimits(request_limit=_MAX_AGENT_STEPS_PER_RUN),
                                    )
                            finally:
                                if captured_messages:
                                    replace_run_history(history_layer, captured_messages)
                    except TimeoutError as exc:
                        if not run_timeout.expired():
                            raise
                        raise UsageLimitExceeded(
                            f"Agent run exceeded the configured limit of {self.run_timeout_seconds:g} seconds"
                        ) from exc
                    complete_usage = model.accumulated_usage if isinstance(model, _HasAccumulatedUsage) else None
                    usage = _serialize_agent_usage(
                        complete_usage if complete_usage is not None else _result_usage(result)
                    )
                    self._terminal_usage = usage
                    if isinstance(result.output, DeferredToolRequests):
                        plan_review = bool(result.output.calls and result.output.calls[0].tool_name == "exit_plan_mode")
                        deferred_layer = (
                            environment_layer
                            if (result.output.calls and result.output.calls[0].tool_name == TOOL_NAME)
                            else ask_human_layer
                        )
                        if deferred_layer is None and not (plan_review and control_layer is not None):
                            raise AgentRunValidationError(
                                "Deferred tool requests were returned, but no active ask_human layer is available for validation."
                            )
                        if history_layer is None:
                            raise AgentRunValidationError(
                                "ask_human deferred tool requests require a 'history' layer so the pending tool call can be resumed."
                            )
                        if plan_review and control_layer is not None:
                            deferred_tool_call = await control_layer.plan_review(result.output)
                        else:
                            assert deferred_layer is not None
                            deferred_tool_call = deferred_layer.build_deferred_tool_call_payload(result.output)
                        result_kind = "deferred_tool_call"
                    else:
                        output = _serialize_agent_output(result.output)
                        result_kind = "output"
        except RuntimeError as exc:
            if not entered_run and is_agenton_enter_validation_runtime_error(exc):
                raise AgentRunValidationError(str(exc)) from exc
            raise
        except ValueError as exc:
            if not entered_run:
                raise AgentRunValidationError(str(exc)) from exc
            raise
        finally:
            if entered_run and run is not None:
                self._terminal_session_snapshot = run.session_snapshot
            if isinstance(model, _HasAccumulatedUsage):
                accumulated_usage = _serialize_agent_usage(model.accumulated_usage)
                if accumulated_usage is not None:
                    self._terminal_usage = accumulated_usage

        if run is None or run.session_snapshot is None:
            raise RuntimeError("Agenton run did not produce a session snapshot after exit.")
        if result_kind is None:
            raise RuntimeError("Agent run did not resolve either a final output or a deferred tool call.")

        return RunSuccessOutcome(
            result_kind=result_kind,
            output=output,
            deferred_tool_call=deferred_tool_call,
            session_snapshot=run.session_snapshot,
            usage=usage,
        )


def _serialize_agent_output(output: object) -> JsonValue:
    """Convert arbitrary pydantic-ai output into the public JSON-safe payload type."""
    return cast(JsonValue, _AGENT_OUTPUT_ADAPTER.dump_python(output, mode="json"))


def _result_usage(result: object) -> object | None:
    """Return pydantic-ai result usage across method/property API variants."""
    if not isinstance(result, _HasUsage):
        return None

    usage = result.usage
    if isinstance(usage, (_HasInputTokens, _HasOutputTokens)):
        return usage
    if callable(usage):
        usage_getter = cast(Callable[[], object], usage)
        return usage_getter()
    return usage


def _serialize_agent_usage(usage: object | None) -> AgentRunUsage | None:
    """Convert complete daemon or fallback pydantic-ai usage into the public shape."""
    if usage is None:
        return None
    if isinstance(usage, LLMUsage):
        return AgentRunUsage.model_validate(usage.model_dump(mode="python"))
    input_tokens = int(usage.input_tokens or 0) if isinstance(usage, _HasInputTokens) else 0
    output_tokens = int(usage.output_tokens or 0) if isinstance(usage, _HasOutputTokens) else 0
    total_tokens = int(usage.total_tokens or 0) if isinstance(usage, _HasTotalTokens) else 0
    return AgentRunUsage(
        prompt_tokens=input_tokens,
        completion_tokens=output_tokens,
        total_tokens=total_tokens,
    )


def _resolve_agent_output_type(output_type: OutputSpec[object], allow_deferred_tools: bool) -> OutputSpec[object]:
    """Return the run output type, optionally augmented with deferred-tool support."""
    if not allow_deferred_tools:
        return output_type
    return cast(OutputSpec[object], [output_type, DeferredToolRequests])


def _resolve_deferred_tool_results(request: CreateRunRequest) -> DeferredToolResults | None:
    """Convert public deferred tool results into the pydantic-ai resume input."""
    if request.deferred_tool_results is None:
        return None
    return request.deferred_tool_results.to_pydantic_ai()


async def _resolve_run_tools(
    run: Any,
    *,
    plugin_daemon_http_client: httpx.AsyncClient,
    dify_api_http_client: httpx.AsyncClient,
) -> list[PydanticAITool[object]]:
    """Return the static compositor tools plus any Dify runtime tools."""
    resolved_tools = list(cast(list[PydanticAITool[object]], run.tools))
    for slot in run.slots.values():
        layer = slot.layer
        if isinstance(layer, DifyPluginToolsLayer):
            resolved_tools.extend(
                await layer.get_tools(
                    http_client=plugin_daemon_http_client,
                    dify_api_http_client=dify_api_http_client,
                )
            )
        if isinstance(layer, DifyCoreToolsLayer):
            resolved_tools.extend(await layer.get_tools(http_client=dify_api_http_client))
        if isinstance(layer, (DifyKnowledgeBaseLayer, WorkbenchFilesLayer)):
            resolved_tools.extend(await layer.get_tools(http_client=dify_api_http_client))
    _validate_unique_tool_names(resolved_tools)
    return resolved_tools


def _validate_unique_tool_names(tools: list[PydanticAITool[object]]) -> None:
    """Reject duplicate tool names across static and dynamic tool sources."""
    duplicate_names = sorted(name for name, count in Counter(tool.name for tool in tools).items() if count > 1)
    if duplicate_names:
        names = ", ".join(duplicate_names)
        raise ValueError(f"Agent run requires unique tool names across all layers, got duplicates: {names}.")


__all__ = ["AgentRunRunner", "AgentRunValidationError"]
