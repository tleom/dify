"""Continuation must retain stopped turns and enforce the complete chat owner."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from werkzeug.exceptions import Conflict, NotFound

from services.workbench import branches


def run(
    run_id: str, status: str = "cancelled", parent: str | None = None, message: str | None = None, **history: object
) -> SimpleNamespace:
    return SimpleNamespace(
        id=run_id,
        status=status,
        chat_id="chat",
        tenant_id="tenant",
        account_id="account",
        payload=json.dumps({"branch_parent_run_id": parent, **history}),
        event_log=json.dumps([{"message_id": message}] if message else []),
    )


def resolve(runs: list[SimpleNamespace], payload: dict[str, object]) -> dict[str, object]:
    session = Mock()
    session.scalars.return_value = runs
    chat = SimpleNamespace(id="chat", tenant_id="tenant", account_id="account")
    result = branches.resolve_parent(session, chat, payload)
    query = session.scalars.call_args.args[0].compile()
    assert query.params == {
        "chat_id_1": "chat",
        "tenant_id_1": "tenant",
        "account_id_1": "account",
        "status_1": ["discarded", "steered"],
    }
    return result


def test_stopped_turn_without_native_message_remains_the_parent() -> None:
    first = run("first", "completed", message="native-first")
    stopped = run("stopped", parent="first", input_history={"messages": ["prior"]})
    result = resolve([first, stopped], {"parent_run_id": "stopped", "parent_message_id": None})
    assert result == {"branch_parent_run_id": "stopped", "parent_message_id": None}
    parent = result["branch_parent_run_id"]
    assert isinstance(parent, str)
    next_run = run("next", parent=parent)
    assert branches.parent_links([first, stopped, next_run]) == {"first": None, "stopped": "first", "next": "stopped"}
    assert branches.output_history(None, stopped) == {"messages": ["prior"]}


def test_explicit_parent_preserves_selected_version_and_captured_output() -> None:
    chosen = run("chosen", message="native", output_history={"messages": ["captured"]})
    assert (
        resolve([chosen, run("other")], {"parent_run_id": "chosen", "parent_message_id": "native"})[
            "branch_parent_run_id"
        ]
        == "chosen"
    )
    assert branches.output_history(None, chosen) == {"messages": ["captured"]}


@pytest.mark.parametrize(
    "payload",
    [
        {"parent_run_id": "foreign"},
        {"parent_run_id": "owned", "parent_message_id": "foreign-message"},
        {"parent_run_id": None, "parent_message_id": "native"},
    ],
)
def test_rejects_parent_outside_owned_chat_or_mismatched_native_message(payload: dict[str, object]) -> None:
    with pytest.raises(NotFound):
        resolve([run("owned", message="native")], payload)


def test_active_parent_is_still_rejected() -> None:
    with pytest.raises(Conflict):
        resolve([run("active", "running")], {"parent_run_id": "active"})


def test_legacy_root_and_regeneration_keep_existing_semantics() -> None:
    runs = [run("first"), run("second", parent="first")]
    assert resolve(runs, {"parent_message_id": None})["branch_parent_run_id"] is None
    assert resolve(runs, {})["branch_parent_run_id"] == "second"
    assert resolve(runs, {"regenerate_from": "second", "parent_run_id": "second"})["branch_parent_run_id"] == "first"


def test_cancelled_queue_entry_retains_context_from_its_owned_ancestor() -> None:
    session = Mock()
    session.scalar.return_value = run("previous", "completed", output_history={"messages": ["previous conversation"]})
    queued = run("queued", parent="previous")
    assert branches.output_history(session, queued) == {"messages": ["previous conversation"]}
    query = session.scalar.call_args.args[0].compile()
    assert query.params == {"id_1": "previous", "chat_id_1": "chat", "tenant_id_1": "tenant", "account_id_1": "account"}
    session.scalar.return_value = None
    with pytest.raises(NotFound):
        branches.output_history(session, queued)
