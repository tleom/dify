"""Model-authored workbench progress with durable, execution-scoped identities.

The layer owns serializable activity state. Its runtime capability supplies the
event sink and observes the SDK's execution boundaries; no business tool schema
or result is rewritten to carry presentation metadata.
"""

import json
import logging
import re
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal, Self
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter
from pydantic_ai import RunContext, Tool
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.tools import ToolDefinition

from agenton.layers import LayerConfig, NoLayerDeps, PydanticAILayer
from dify_agent.protocol.schemas import WorkbenchActivityData, WorkbenchProgressData, WorkbenchToolData

TOOL_NAME = "report_activity"
logger = logging.getLogger(__name__)
_JSON = TypeAdapter(JsonValue)


def public_value(value: Any) -> JsonValue:
    """Use the same JSON boundary as the public tool stream, with a text fallback."""
    try:
        return _JSON.validate_python(value)
    except ValueError:
        return str(value)


def result_metadata(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        match = re.match(r"^<metadata>([\s\S]*?)</metadata>", value)
        try:
            value = json.loads(match.group(1) if match else value)
        except (ValueError, TypeError):
            return {}
    return value if isinstance(value, dict) else {}


def short_text(value: str, limit: int) -> str:
    value = " ".join("".join(char for char in value if unicodedata.category(char) not in {"Cc", "Cf"}).split())
    return value if 0 < len(value) <= limit else ""


class WorkbenchActivityConfig(LayerConfig):
    workbench_run_id: str = Field(min_length=1)
    enabled: bool = True
    max_reports_without_work: int = Field(default=4, ge=1, le=20)


class ActivityCall(BaseModel):
    call_id: str
    tool_call_id: str
    tool_name: str
    activity_id: str | None = None
    state: Literal["running", "returned", "error"] = "running"
    job_id: str | None = None


class WorkbenchActivityState(BaseModel):
    workbench_run_id: str | None = None
    current_id: str | None = None
    activities: dict[str, WorkbenchActivityData] = Field(default_factory=dict)
    calls: dict[str, ActivityCall] = Field(default_factory=dict)
    jobs: dict[str, bool] = Field(default_factory=dict)
    reports_without_work: int = 0
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


@dataclass
class WorkbenchActivityLayer(PydanticAILayer[NoLayerDeps, object, WorkbenchActivityConfig, WorkbenchActivityState]):
    type_id: ClassVar[str | None] = "dify.workbench_activity"
    config: WorkbenchActivityConfig
    _native_run_id: str = field(default="", init=False, repr=False)
    _publish: Callable[[WorkbenchProgressData], Awaitable[None]] | None = field(default=None, init=False, repr=False)
    _parents: dict[str, str | None] = field(default_factory=dict, init=False, repr=False)
    _defaults: dict[str, str | None] = field(default_factory=dict, init=False, repr=False)
    _reports: dict[str, str | None] = field(default_factory=dict, init=False, repr=False)
    _call_parts: dict[str, ToolCallPart] = field(default_factory=dict, init=False, repr=False)
    _active_calls: dict[str, str] = field(default_factory=dict, init=False, repr=False)

    @classmethod
    def from_config(cls, config: WorkbenchActivityConfig) -> Self:
        return cls(config=config)

    async def on_context_create(self) -> None:
        self.runtime_state = WorkbenchActivityState(workbench_run_id=self.config.workbench_run_id)

    async def on_context_resume(self) -> None:
        if self.runtime_state.workbench_run_id != self.config.workbench_run_id:
            await self.on_context_create()

    @property
    def prefix_prompts(self):
        return [self._prompt]

    @staticmethod
    def _prompt() -> str:
        return (
            "Use report_activity to briefly tell the user what you are doing and why, in the user's language. "
            "Group tool work by its specific purpose, not by the entire user request or by tool names. "
            "Before related commands, begin an activity with a concise purpose title (normally 6-16 Chinese characters). "
            "Keep commands that pursue the same concrete result in that activity. When the purpose changes, begin a new "
            "activity: diagnosing a read failure, installing its missing dependency, and verifying the original read "
            "are distinct purposes even within one user task. Never use one broad activity to cover all of them. "
            "Use goal for explanatory detail, and keep title a short purpose phrase without commands, counts or status labels. "
            "Update the same activity only to clarify its purpose; close it after its result is checked, preserving the "
            "concise title. On environment resume, finish the installation activity and begin the verification purpose "
            "before retrying. Report meaningful purpose changes, not every call or log line. "
            "Normal explanations and final answers remain normal assistant text. Do not reveal hidden reasoning, credentials, "
            "full commands or full tool results in titles. Keep updates factual: an installation succeeding does not prove "
            "the original problem is fixed; retry the original operation before claiming recovery. Close only after checking "
            "results. Await human/environment results before describing subsequent work. If reporting is unavailable or "
            "rejected, continue the business task without repeatedly trying to report."
        )

    @property
    def tools(self):
        return [Tool(self._report, name=TOOL_NAME, takes_ctx=True, sequential=True, prepare=self._prepare)]

    def _prepare(self, _ctx: RunContext[object], definition: ToolDefinition):
        if not self.config.enabled or self.runtime_state.reports_without_work >= self.config.max_reports_without_work:
            return None
        current = self.runtime_state.activities.get(self.runtime_state.current_id or "")
        if current is not None:
            definition.description = (definition.description or "") + (
                f" Current activity: {current.activity_id}; title: {current.title}. "
                "Omit activity_id to update the current activity."
            )
        return definition

    def plan_response(self, response: ModelResponse) -> None:
        """Remember model emission order before validation or deferred-tool collection.

        SDK validation happens before the sequential execution barriers, and
        external calls can be collected after ordinary calls. Neither validation
        order nor a shared 'last active' value is an execution identity.
        """
        self._parents.clear()
        self._defaults.clear()
        self._reports.clear()
        self._call_parts.clear()
        self._active_calls.clear()
        predecessor: str | None = None
        for call in response.tool_calls:
            self._parents[call.tool_call_id] = predecessor
            self._defaults[call.tool_call_id] = self.runtime_state.current_id
            self._call_parts[call.tool_call_id] = call
            if call.tool_name == TOOL_NAME:
                predecessor = call.tool_call_id

    def activity_for(self, tool_call_id: str) -> str | None:
        parent = self._parents.get(tool_call_id)
        while parent is not None:
            if parent in self._reports:
                return self._reports[parent]
            parent = self._parents.get(parent)
        return self._defaults.get(tool_call_id, self.runtime_state.current_id)

    async def publish(self, data: WorkbenchProgressData) -> None:
        if self._publish is not None:
            await self._publish(data)

    async def _report(
        self,
        ctx: RunContext[object],
        action: Literal["begin", "update", "close"],
        title: str = "",
        goal: str = "",
        activity_id: str | None = None,
        evidence_call_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Report a short public purpose summary; this never performs or authorizes business work."""
        state = self.runtime_state
        call_id = ctx.tool_call_id or ""
        previous_id = self.activity_for(call_id)
        self._reports[call_id] = previous_id
        state.reports_without_work += 1
        # Keep a concise existing purpose when a provider only supplies a new
        # goal/result. A new activity can still use the model-authored goal.
        previous = state.activities.get(activity_id or previous_id or "")
        title = short_text(title or (previous.title if previous else goal), 120)
        if not title or action not in {"begin", "update", "close"}:
            return {"accepted": False, "reason": "Keep the previous summary and continue the task."}
        if state.reports_without_work > self.config.max_reports_without_work:
            return {"accepted": False, "reason": "Continue business work before reporting again."}
        identifier = activity_id or previous_id
        if action == "begin":
            if activity_id is not None:
                return {"accepted": False, "reason": "New activity IDs are assigned by the runtime."}
            identifier = str(uuid4())
        elif identifier not in state.activities:
            return {"accepted": False, "reason": "Use an activity from this workbench run."}
        assert identifier is not None
        evidence = list(dict.fromkeys(evidence_call_ids or []))
        if len(evidence) > 20 or any(
            not any(
                call.tool_call_id == value and call.activity_id == identifier and call.state != "running"
                for call in state.calls.values()
            )
            for value in evidence
        ):
            return {"accepted": False, "reason": "Evidence must refer to returned calls from this task."}
        if action == "close" and any(
            call.activity_id == identifier
            and (call.state == "running" or (call.job_id and not state.jobs.get(call.job_id)))
            for call in state.calls.values()
        ):
            return {"accepted": False, "reason": "Some results are still pending. Continue or wait for them."}
        previous = state.activities.get(identifier)
        data = WorkbenchActivityData(
            workbench_run_id=self.config.workbench_run_id,
            activity_id=identifier,
            revision=(previous.revision + 1 if previous else 1),
            action=action,
            title=title,
            goal=short_text(goal, 240) or (previous.goal if previous else title),
            evidence_call_ids=evidence,
        )
        if previous is not None and (previous.title, previous.goal, previous.action) == (
            data.title,
            data.goal,
            data.action,
        ):
            self._reports[call_id] = identifier
            return {"accepted": True, "activity_id": identifier, "revision": previous.revision}
        state.activities[identifier] = data
        state.current_id = identifier
        self._reports[call_id] = identifier
        try:
            await self.publish(data)
        except Exception:
            logger.warning("Could not publish workbench activity summary", exc_info=True)
        return {"accepted": True, "activity_id": identifier, "revision": data.revision}

    async def start_call(self, call: ToolCallPart, args: Any) -> ActivityCall | None:
        if call.tool_name == TOOL_NAME:
            return None
        if call.tool_call_id in self._active_calls:
            return self.runtime_state.calls[self._active_calls[call.tool_call_id]]
        base = f"{self._native_run_id}:{call.tool_call_id}"
        identifier = base
        occurrence = 1
        while identifier in self.runtime_state.calls:
            occurrence += 1
            identifier = f"{base}:{occurrence}"
        binding = ActivityCall(
            call_id=identifier,
            tool_call_id=call.tool_call_id,
            tool_name=call.tool_name,
            activity_id=self.activity_for(call.tool_call_id),
        )
        self.runtime_state.calls[identifier] = binding
        self._active_calls[call.tool_call_id] = identifier
        self.runtime_state.reports_without_work = 0
        await self.publish(
            WorkbenchToolData(
                workbench_run_id=self.config.workbench_run_id,
                call_id=identifier,
                tool_call_id=call.tool_call_id,
                tool_name=call.tool_name,
                activity_id=binding.activity_id,
                stage="started",
                input=public_value(args),
            )
        )
        return binding

    async def finish_call(self, raw_id: str, tool_name: str, output: Any, *, failed: bool = False) -> None:
        if tool_name == TOOL_NAME:
            return
        identifier = self._active_calls.get(raw_id)
        binding = self.runtime_state.calls.get(identifier or "")
        if binding is None:
            # A resumed external result belongs to its original execution, not the new attempt.
            candidates = [
                call
                for call in self.runtime_state.calls.values()
                if call.tool_call_id == raw_id and call.state == "running"
            ]
            if len(candidates) == 1:
                binding = candidates[0]
            else:
                part = self._call_parts.get(raw_id, ToolCallPart(tool_name, {}, tool_call_id=raw_id))
                try:
                    args = part.args_as_dict()
                except ValueError:
                    args = part.args
                binding = await self.start_call(part, args)
        if binding is None:
            return
        metadata = result_metadata(output)
        failed = failed or bool(metadata.get("error") or metadata.get("is_error") or metadata.get("isError"))
        failed = failed or metadata.get("status") in {"error", "failed"}
        failed = failed or (isinstance(metadata.get("exit_code"), int) and metadata["exit_code"] != 0)
        # The knowledge layer deliberately softens transport/access errors into
        # explicit model observations; they remain failed attempts in the UI.
        if tool_name.startswith("knowledge_base_") and isinstance(output, str):
            failed = failed or output.startswith(
                (
                    "Knowledge base access failed;",
                    "Knowledge base search is temporarily unavailable;",
                )
            )
        binding.state = "error" if failed else "returned"
        if metadata.get("job_id") is not None:
            binding.job_id = str(metadata["job_id"])
            self.runtime_state.jobs[binding.job_id] = metadata.get("done") is True
        await self.publish(
            WorkbenchToolData(
                workbench_run_id=self.config.workbench_run_id,
                call_id=binding.call_id,
                tool_call_id=raw_id,
                tool_name=tool_name,
                activity_id=binding.activity_id,
                stage="error" if failed else "returned",
                output=public_value(output),
            )
        )
