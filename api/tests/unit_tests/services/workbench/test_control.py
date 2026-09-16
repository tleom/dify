"""Mode commands use real SQL transactions and the existing run admission path."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from dify_agent.protocol.workbench_control import GoalState, TodoItem
from werkzeug.exceptions import BadRequest, Conflict, Forbidden

from models.workbench import WorkbenchCommand, WorkbenchControl, WorkbenchRun, WorkbenchRunEvent
from services.workbench import control, followups, service
from tests.unit_tests.services.workbench.test_followups import queue_fixture

queue = pytest.fixture(queue_fixture)


def test_goal_carries_personal_skill_mentions_into_each_round(
    queue: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from services.workbench import resources

    monkeypatch.setattr(resources, "personal_skill_catalog", lambda *_: [{"id": "personal:case", "name": "case"}])
    result = control.issue(
        queue.owner[0],
        queue.owner[1],
        queue.chat_id,
        command="/goal 核验案件",
        request_key="goal-with-skill",
        resource_mentions={"skills": ["personal:case"]},
    )
    identifier = result["state"]["goal"]["last_run_id"]
    for index in range(2):
        payload = json.loads(queue.get(identifier).payload)
        assert payload["resource_mentions"]["skills"] == ["personal:case"]
        assert '"scope": "personal"' in payload["mention_prompt"]
        queue.finish(identifier)
        if index == 0:
            next_run = control.drive_goal(queue.owner[0], queue.owner[1], queue.chat_id)
            assert next_run is not None
            identifier = next_run["id"]


def test_compact_instruction_is_the_following_task_and_empty_compact_only_summarizes(queue: SimpleNamespace) -> None:
    first = queue.send("建立上下文")
    queue.finish(first["id"])
    with queue.factory.begin() as session:
        chat = service._chat(session, queue.owner[0], queue.owner[1], queue.chat_id, lock=True)
        chat.conversation_id = str(uuid4())
    result = control.issue(
        queue.owner[0], queue.owner[1], queue.chat_id, command="/compact 继续生成报告", request_key="compact-following"
    )
    payload = json.loads(queue.get(result["run"]["id"]).payload)
    assert payload["query"] == "继续生成报告"
    assert payload["control"]["continue_after"] is True
    assert result["run"]["command"] == "compact"
    assert result["run"]["is_continuation"] is False
    assert payload["is_continuation"] is False
    assert "focus" not in payload["control"]
    queue.finish(result["run"]["id"])
    result = control.issue(
        queue.owner[0], queue.owner[1], queue.chat_id, command="/compact", request_key="compact-only"
    )
    payload = json.loads(queue.get(result["run"]["id"]).payload)
    assert payload["query"] == "压缩上下文"
    assert payload["control"]["continue_after"] is False
    assert result["run"]["command"] == "compact"
    assert result["run"]["is_continuation"] is False
    old = queue.get(result["run"]["id"])
    old.payload = json.dumps({**payload, "command": None, "is_continuation": True})
    assert service.run_dto(old)["is_continuation"] is False
    assert service.run_dto(old)["command"] == "compact"


def test_plan_tag_is_persisted_and_legacy_tag_comes_from_its_own_command(queue: SimpleNamespace) -> None:
    result = control.issue(
        queue.owner[0], queue.owner[1], queue.chat_id, command="/plan 设计处理方案", request_key="plan-tag"
    )
    run = queue.get(result["run"]["id"])
    assert service.run_dto(run)["command"] == "plan"
    payload = json.loads(run.payload)
    assert payload["query"] == "设计处理方案"
    payload.pop("command")
    run.payload = json.dumps(payload)
    with queue.factory() as session:
        assert control.with_commands(session, [run], [service.run_dto(run)])[0]["command"] == "plan"
    run.account_id = str(uuid4())
    with queue.factory() as session:
        assert control.with_commands(session, [run], [service.run_dto(run)])[0]["command"] is None


def command(queue: SimpleNamespace, text: str, key: str | None = None) -> control.WorkbenchControlState:
    result = control.issue(queue.owner[0], queue.owner[1], queue.chat_id, command=text, request_key=key or str(uuid4()))
    return control.WorkbenchControlState.model_validate(result["state"])


@pytest.mark.parametrize("legacy_limit", [None, 256])
def test_goal_continues_after_256_final_responses_until_explicit_pause(
    queue: SimpleNamespace, legacy_limit: int | None
) -> None:
    state = command(queue, "/goal 全面核验所有文件")
    assert state.goal is not None
    queue.finish(state.goal.last_run_id)
    with queue.factory.begin() as session:
        chat = service._chat(session, queue.owner[0], queue.owner[1], queue.chat_id, lock=True)
        current = control.load(session, chat)
        assert current.goal is not None
        current.goal.rounds_started = 256
        current.goal.max_rounds = legacy_limit
        control.save(session, chat, current)
    following = control.drive_goal(queue.owner[0], queue.owner[1], queue.chat_id)
    assert following is not None
    saved = control.read(queue.owner[0], queue.owner[1], queue.chat_id)["goal"]
    assert saved["rounds_started"] == 257
    assert saved["max_rounds"] is None
    queue.finish(following["id"])
    command(queue, "/goal pause")
    assert control.drive_goal(queue.owner[0], queue.owner[1], queue.chat_id) is None
    resumed = command(queue, "/goal resume")
    assert resumed.goal is not None
    assert resumed.goal.rounds_started == 258


def test_goal_clock_survives_read_pause_resume_and_completion(
    queue: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    start = datetime(2026, 9, 16, 1)
    instant = [start]
    monkeypatch.setattr(control, "naive_utc_now", lambda: instant[0])
    first = command(queue, "/goal 整理证据")
    assert first.goal is not None
    assert first.goal.active_since == start.replace(tzinfo=UTC).timestamp()
    instant[0] += timedelta(seconds=63)
    paused = command(queue, "/goal pause")
    assert paused.goal is not None
    assert paused.goal.elapsed_seconds == 63
    assert paused.goal.active_since is None
    instant[0] += timedelta(hours=1)
    read = control.read(queue.owner[0], queue.owner[1], queue.chat_id)["goal"]
    assert read["elapsed_seconds"] == 63
    resumed = command(queue, "/goal resume")
    assert resumed.goal is not None
    assert resumed.goal.active_since == instant[0].replace(tzinfo=UTC).timestamp()
    instant[0] += timedelta(seconds=7)
    with queue.factory.begin() as session:
        chat = service._chat(session, queue.owner[0], queue.owner[1], queue.chat_id, lock=True)
        state = control.load(session, chat)
        assert state.goal is not None
        state = control.finish_goal(
            state, goal_id=state.goal.id, revision=state.goal.revision, phase="complete", reason="已核对"
        )
        control.save(session, chat, state)
    complete = control.read(queue.owner[0], queue.owner[1], queue.chat_id)["goal"]
    assert complete["elapsed_seconds"] == 70
    assert complete["active_since"] is None


def test_legacy_goal_clock_restores_command_phase_history(queue: SimpleNamespace) -> None:
    start = datetime(2026, 9, 16, 1)
    goal = GoalState(objective="已有目标", phase="complete")
    with queue.factory.begin() as session:
        session.add(
            WorkbenchControl(
                tenant_id=queue.owner[0],
                account_id=queue.owner[1],
                chat_id=queue.chat_id,
                state=json.dumps({"revision": 4, "goal": goal.model_dump()}),
            )
        )
        for revision, seconds, phase in [(1, 0, "active"), (2, 12, "paused"), (3, 100, "active"), (4, 110, "complete")]:
            value = {**goal.model_dump(), "phase": phase}
            session.add(
                WorkbenchCommand(
                    tenant_id=queue.owner[0],
                    account_id=queue.owner[1],
                    chat_id=queue.chat_id,
                    request_key=str(revision),
                    command="legacy",
                    created_at=start + timedelta(seconds=seconds),
                    result=json.dumps({"state": {"revision": revision, "goal": value}}),
                )
            )
    for _ in range(2):
        restored = control.read(queue.owner[0], queue.owner[1], queue.chat_id)["goal"]
        assert restored["elapsed_seconds"] == 22
        assert restored["active_since"] is None


def test_clear_todos_is_owned_revision_checked_and_retry_safe(queue: SimpleNamespace) -> None:
    from werkzeug.exceptions import NotFound

    with queue.factory.begin() as session:
        chat = service._chat(session, queue.owner[0], queue.owner[1], queue.chat_id, lock=True)
        state = control.WorkbenchControlState(
            goal=GoalState(objective="保留目标", phase="paused"), todos=[TodoItem(content="已核对", status="completed")]
        )
        control.save(session, chat, state)
    with pytest.raises(NotFound):
        control.clear_todos(queue.owner[0], str(uuid4()), queue.chat_id, request_key="other", expected_revision=0)
    with pytest.raises(Conflict):
        control.clear_todos(queue.owner[0], queue.owner[1], queue.chat_id, request_key="old", expected_revision=99)
    cleared = control.clear_todos(
        queue.owner[0], queue.owner[1], queue.chat_id, request_key="clear", expected_revision=0
    )
    assert cleared["todos"] == []
    assert cleared["goal"]["objective"] == "保留目标"
    assert control.read(queue.owner[0], queue.owner[1], queue.chat_id)["todos"] == []
    with queue.factory.begin() as session:
        chat = service._chat(session, queue.owner[0], queue.owner[1], queue.chat_id, lock=True)
        state = control.load(session, chat)
        state.todos = [TodoItem(content="后来生成的清单", status="pending")]
        state.revision += 1
        control.save(session, chat, state)
    retry = control.clear_todos(queue.owner[0], queue.owner[1], queue.chat_id, request_key="clear", expected_revision=0)
    assert retry["todos"][0]["content"] == "后来生成的清单"


def test_goal_command_idempotency_and_pause_prevent_duplicate_runs(queue: SimpleNamespace) -> None:
    first = command(queue, "/goal 校验文件", "same")
    again = command(queue, "/goal 校验文件", "same")
    assert first.goal is not None
    assert again.goal is not None
    assert first.goal.id == again.goal.id
    assert again.goal.rounds_started == 1
    assert control.drive_goal(queue.owner[0], queue.owner[1], queue.chat_id) is None
    command(queue, "/goal pause")
    run_id = first.goal.last_run_id
    queue.finish(run_id)
    assert control.drive_goal(queue.owner[0], queue.owner[1], queue.chat_id) is None
    with pytest.raises(Conflict):
        command(queue, "/goal another", "same")


def test_goal_waits_for_user_messages_and_resets_todos_only_at_admission(queue: SimpleNamespace) -> None:
    run = queue.send("原消息")
    queue.running(run["id"])
    ticket = queue.get(run["id"]).backend_run_id
    payload = control.AgentControlPayload(
        tenant_id=queue.owner[0],
        account_id=queue.owner[1],
        app_id=queue.app_id,
        workbench_run_id=run["id"],
        backend_run_id=ticket,
        action="todo_write",
        request_key="todo",
        data={"todos": [{"content": "当前步骤", "status": "in_progress"}]},
    )
    control.agent_control(payload)
    queued = queue.send("下一条消息")
    assert control.read(queue.owner[0], queue.owner[1], queue.chat_id)["todos"][0]["content"] == "当前步骤"
    queue.finish(run["id"])
    assert queue.advance() == queued["id"]
    assert control.read(queue.owner[0], queue.owner[1], queue.chat_id)["todos"] == []
    with pytest.raises(Forbidden):
        control.agent_control(payload)


def test_plan_review_requires_explicit_action_and_disables_goal_driver(queue: SimpleNamespace) -> None:
    command(queue, "/plan")
    goal = command(queue, "/goal 输出报告")
    assert goal.goal is not None
    assert goal.goal.rounds_started == 1
    run = {"id": goal.goal.last_run_id}
    assert control.drive_goal(*queue.owner, queue.chat_id) is None
    with queue.factory.begin() as session:
        chat = service._chat(session, queue.owner[0], queue.owner[1], queue.chat_id, lock=True)
        row = session.get(WorkbenchRun, run["id"])
        state = control.load(session, chat)
        state.plan.review, state.plan.review_run_id = "# 实施计划\n\n检查后生成。", row.id
        control.save(session, chat, state)
        payload = json.loads(row.payload)
        payload["pending"] = {
            "tool_name": "exit_plan_mode",
            "tool_call_id": "plan-call",
            "args": {
                "title": "计划",
                "question": "选择下一步",
                "markdown": "# 实施计划\n\n检查后生成。",
                "fields": [{"name": "feedback", "label": "意见", "type": "paragraph", "required": False}],
                "actions": [{"id": "approve", "label": "开始执行"}, {"id": "keep_planning", "label": "继续规划"}],
            },
        }
        row.payload, row.status = json.dumps(payload), "waiting_input"
    row = queue.get(run["id"])
    request_id = service.input_request_id(row, json.loads(row.payload))
    with pytest.raises(BadRequest):
        service.resume(queue.owner[0], queue.owner[1], run["id"], {}, None, request_id)
    service.resume(queue.owner[0], queue.owner[1], run["id"], {}, "approve", request_id)
    assert control.read(queue.owner[0], queue.owner[1], queue.chat_id)["plan"]["active"] is False
    assert control.read(*queue.owner, queue.chat_id)["plan"]["approved"] == "# 实施计划\n\n检查后生成。"


def test_first_goal_message_and_legacy_attachment_placeholder_restore_original_text(queue, monkeypatch):
    from services.workbench import files as file_service

    text = "请基于附件完成完整报告。\n保留全部原始证据，并核对每一项结果。"
    files = [{"type": "document", "transfer_method": "local_file", "upload_file_id": str(uuid4())}]
    # Upload ownership validation has its own tests; this fixture exercises run/command persistence.
    monkeypatch.setattr(file_service, "validate_attachments", lambda *_args: (["/workspace/source.docx"], []))
    result = control.issue(*queue.owner, queue.chat_id, command="/goal " + text, files=files, request_key="original")
    first = queue.get(result["run"]["id"])
    assert result["run"]["query"] == text
    assert result["run"]["is_continuation"] is False
    payload = json.loads(first.payload)
    assert payload["query"] == text
    assert payload["control"]["round"] == 1
    command(queue, "/goal edit 更新后的目标")
    # A legacy run with an existing tag must still recover its own command text.
    first.payload = json.dumps({**payload, "query": "目标参考附件", "files": files})
    with queue.factory() as session:
        recovered = control.with_commands(session, [first], [service.run_dto(first)])[0]
    assert recovered["query"] == text
    assert recovered["command"] == "goal"
    queue.finish(first.id)
    following = control.drive_goal(*queue.owner, queue.chat_id)
    assert following["is_continuation"] is True
    assert "更新后的目标" in following["query"]


def test_initial_goal_request_recovers_after_enqueue_failure_even_in_plan_mode(queue, monkeypatch):
    command(queue, "/plan")
    with monkeypatch.context() as patch:
        patch.setattr(service, "enqueue", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("publish gap")))
        with pytest.raises(RuntimeError, match="publish gap"):
            command(queue, "/goal 核对原始长文本和附件", "recover-original")
    recovered = control.drive_goal(*queue.owner, queue.chat_id)
    assert recovered["query"] == "核对原始长文本和附件"
    assert recovered["is_continuation"] is False
    assert control.read(*queue.owner, queue.chat_id)["goal"]["rounds_started"] == 1
    assert control.drive_goal(*queue.owner, queue.chat_id) is None
    queue.finish(recovered["id"])
    assert control.drive_goal(*queue.owner, queue.chat_id) is None


def test_queued_goal_retains_control_and_cancelled_goal_input_does_not_execute(queue):
    busy = queue.send("先处理当前任务")
    result = control.issue(*queue.owner, queue.chat_id, command="/goal 后续目标", request_key="queued-goal")
    assert result["run"]["status"] == "waiting_turn"
    assert result["state"]["goal"]["rounds_started"] == 0
    queue.finish(busy["id"])
    assert queue.advance() == result["run"]["id"]
    saved = control.read(*queue.owner, queue.chat_id)["goal"]
    assert saved["rounds_started"] == 1
    assert saved["last_run_id"] == result["run"]["id"]
    assert json.loads(queue.get(saved["last_run_id"]).payload)["control"]["source"] == "user"
    command(queue, "/goal clear")
    stale = control.issue(*queue.owner, queue.chat_id, command="/goal 即将清除", request_key="stale-goal")
    command(queue, "/goal clear")
    queue.finish(result["run"]["id"])
    assert queue.advance() is None
    assert queue.get(stale["run"]["id"]).status == "cancelled"


def test_exhausted_failure_blocks_same_goal_generation(queue: SimpleNamespace) -> None:
    result = command(queue, "/goal 核验")
    assert result.goal is not None
    queue.finish(result.goal.last_run_id, "failed")
    control.settle(queue.owner[0], queue.owner[1], queue.chat_id)
    assert control.read(queue.owner[0], queue.owner[1], queue.chat_id)["goal"]["phase"] == "blocked"
    command(queue, "/goal resume")
    assert control.read(queue.owner[0], queue.owner[1], queue.chat_id)["goal"]["rounds_started"] == 2


def plan_pending(queue, run_id, plan, version):
    with queue.factory.begin() as session:
        row = session.get(WorkbenchRun, run_id)
        data = json.loads(row.payload)
        data["pending"] = {
            "tool_name": "exit_plan_mode",
            "tool_call_id": f"plan-{version}",
            "metadata": {"plan_version": version},
            "args": {
                "question": "是否按此方案执行？",
                "markdown": plan,
                "fields": [{"name": "feedback", "type": "paragraph", "label": "意见", "required": False}],
                "actions": [{"id": "approve", "label": "开始执行"}, {"id": "keep_planning", "label": "继续规划"}],
            },
        }
        row.payload, row.status = json.dumps(data), "waiting_input"
    row = queue.get(run_id)
    return service.input_request_id(row, json.loads(row.payload))


def agent_payload(queue, run_id, action, data=None, key="operation"):
    return control.AgentControlPayload(
        tenant_id=queue.owner[0],
        account_id=queue.owner[1],
        app_id=queue.app_id,
        workbench_run_id=run_id,
        backend_run_id=queue.get(run_id).backend_run_id,
        action=action,
        request_key=key,
        data=data or {},
    )


def test_review_feedback_requires_a_new_version_and_stale_card_cannot_approve(queue):
    from services.workbench.recovery import expire_input

    result = control.issue(*queue.owner, queue.chat_id, command="/plan 制定方案", request_key="start-plan")
    identifier = result["run"]["id"]
    queue.running(identifier)
    first = "# 第一版\n核对附件。"
    payload = agent_payload(queue, identifier, "review_plan", {"plan": first}, "first-review")
    control.agent_control(payload)
    request_id = plan_pending(queue, identifier, first, 1)
    with pytest.raises(Conflict, match="修改意见"):
        service.resume(*queue.owner, identifier, {"feedback": "增加核验"}, "approve", request_id)
    assert expire_input(identifier, owner=queue.owner, request_id=request_id, manual=True) is False
    service.resume(*queue.owner, identifier, {"feedback": "增加核验"}, "keep_planning", request_id)
    assert control.read(*queue.owner, queue.chat_id)["plan"]["active"]
    queue.running(identifier)
    second = "# 第二版\n核对附件和来源。"
    control.agent_control(agent_payload(queue, identifier, "review_plan", {"plan": second}, "second-review"))
    stale_id = plan_pending(queue, identifier, first, 1)
    with pytest.raises(Conflict, match="计划已改变"):
        service.resume(*queue.owner, identifier, {}, "approve", stale_id)
    request_id = plan_pending(queue, identifier, second, 2)
    service.resume(*queue.owner, identifier, {}, "approve", request_id)
    plan = control.read(*queue.owner, queue.chat_id)["plan"]
    assert not plan["active"]
    assert plan["approved"] == second
    assert plan["approved_version"] == 2


def test_identical_todo_has_no_new_progress_event_and_planning_rejects_list(queue):
    run = queue.send("执行一个复杂任务")
    queue.running(run["id"])
    data = {"todos": [{"content": "核对", "status": "in_progress"}]}
    first = control.agent_control(agent_payload(queue, run["id"], "todo_write", data, "list-1"))
    with queue.factory() as session:
        cursor = session.query(WorkbenchRunEvent).filter_by(run_id=run["id"]).count()
    repeated = control.agent_control(agent_payload(queue, run["id"], "todo_write", data, "list-2"))
    assert repeated["state"]["revision"] == first["state"]["revision"]
    with queue.factory() as session:
        assert session.query(WorkbenchRunEvent).filter_by(run_id=run["id"]).count() == cursor
    command(queue, "/plan")
    with pytest.raises(Conflict, match="完整方案"):
        control.agent_control(agent_payload(queue, run["id"], "todo_write", data, "list-3"))


def test_goal_first_continuing_and_resumed_rounds_accept_steering(queue: SimpleNamespace) -> None:
    result = command(queue, "/goal 核验报告")
    for index in range(3):
        run_id = control.read(queue.owner[0], queue.owner[1], queue.chat_id)["goal"]["last_run_id"]
        row = queue.get(run_id)
        assert json.loads(row.payload)["followup_protocol"] == 1
        queue.running(run_id)
        queued = queue.send(f"方向调整 {index}", followup_protocol=1)
        followups.steer(queue.owner[0], queue.owner[1], queued["id"], run_id)
        messages = queue.poll(run_id)["messages"]
        assert any(f"方向调整 {index}" in item["content"] for item in messages)
        queue.finish(run_id, "failed" if index == 1 else "completed")
        if index == 1:
            control.settle(queue.owner[0], queue.owner[1], queue.chat_id)
            command(queue, "/goal resume")
        elif index == 0:
            control.drive_goal(queue.owner[0], queue.owner[1], queue.chat_id)
    assert result.goal is not None
    assert result.goal.objective == "核验报告"


def test_later_ordinary_run_does_not_hide_failed_compaction(queue: SimpleNamespace) -> None:
    first = queue.send("建立可压缩的对话")
    queue.finish(first["id"])
    with queue.factory.begin() as session:
        chat = service._chat(session, queue.owner[0], queue.owner[1], queue.chat_id, lock=True)
        chat.conversation_id = str(uuid4())
    result = command(queue, "/compact")
    assert result.compaction is not None
    identifier = result.compaction.id
    with queue.factory.begin() as session:
        rows = session.query(WorkbenchRun).filter(WorkbenchRun.chat_id == queue.chat_id).all()
        compact = next(row for row in rows if json.loads(row.payload).get("control", {}).get("id") == identifier)
        compact.status = "failed"
        compact.error = "完成状态同步失败"
    newer = queue.send("稍后继续处理其他资料")
    control.settle(queue.owner[0], queue.owner[1], queue.chat_id)
    state = control.read(queue.owner[0], queue.owner[1], queue.chat_id)
    assert state["compaction"]["phase"] == "failed"
    assert state["compaction"]["message"] == "完成状态同步失败"
    assert queue.get(newer["id"]).status == "queued"
