"""Actual sandbox inventory commands and real runner validation with deterministic model responses."""

import asyncio
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import httpx
import pytest
from pydantic_ai import Tool
from pydantic_ai.exceptions import UnexpectedModelBehavior

from agenton.layers import LayerConfig
from agenton.layers import LifecycleState
from agenton.compositor import LayerSessionSnapshot
from dify_agent.layers.shell.layer import DifyShellLayer, DifyShellLayerConfig
from dify_agent.layers.runtime import DifyRuntimeLayerConfig
from dify_agent.layers.execution_context.configs import DifyExecutionContextLayerConfig
from dify_agent.layers.execution_context.layer import DifyExecutionContextLayer
from dify_agent.layers.workbench_files import WorkbenchFilesLayer
from dify_agent.protocol import DeferredToolResultsPayload, RunLayerSpec, RunSucceededEvent, WorkbenchActivityRunEvent
from dify_agent.runtime.compositor_factory import create_default_layer_providers
from dify_agent.runtime.runner import AgentRunRunner
from dify_agent.runtime.workbench_files import SNAPSHOT_SCRIPT, WorkbenchFileChanges
from dify_agent.runtime_backend import HomeSnapshotBackend, RuntimeBackendProfile
from .test_runner import FakeRunnerExecutionBindingBackend, FakeRunnerShellctlClient
from .test_workbench_activity import _setup, _progress, _call

PREVIEW = "https://files.example.test/files/workbench/signed/chart.png?mode=preview"
DOWNLOAD = "https://files.example.test/files/workbench/signed/chart.png?mode=download"


def test_inventory_keeps_documents_and_scripts_without_browser_office_or_cache_noise(tmp_path):
    useful = [
        "报告.docx",
        "报告.pdf",
        "chart.png",
        "scripts/generate_report.py",
        "data/source.json",
        "workbench-office-summary.pdf",
        ".env",
        ".gitignore",
        ".npmrc",
        ".github/workflows/report.yml",
        "uv.lock",
        "poetry.lock",
        "yarn.lock",
        "Gemfile.lock",
        "Cargo.lock",
    ]
    noise = [
        *(f"playwright_chromiumdev_profile-A6TuKz/Default/Cache/item-{index}" for index in range(108)),
        "workbench-office-k36__ov8/user/registrymodifications.xcu",
        "com.google.Chrome.chrome_chrome_url_fetcher_.q3voEv",
        "puppeteer_dev_chrome_profile-test/Default/History",
        "node_modules/package/index.js",
        ".cache/fontlist-v390.json",
        ".dify_conf/internal.yml",
        ".git/objects/internal",
        ".workbench-edit-temporary",
        "__pycache__/generate_report.cpython-312.pyc",
        "render.log",
        "intermediate.tmp",
    ]
    for name in [*useful, *noise]:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("test", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-c", SNAPSHOT_SCRIPT],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    assert sorted(json.loads(result.stdout)) == sorted(useful)
    # Observability must never remove the actual working files.
    assert all((tmp_path / name).is_file() for name in noise)


def test_cross_directory_outputs_are_tracked_without_personal_configuration(tmp_path):
    cwd = tmp_path / "conversations/current"
    cwd.mkdir(parents=True)
    other = tmp_path / "conversations/other"
    other.mkdir()
    commands = []

    async def execute(script, **_kwargs):
        commands.append(script)
        arguments = shlex.split(script)
        assert arguments[-2:] == ["", "/workspace"]
        result = subprocess.run(
            [sys.executable, *arguments[1:-1], str(tmp_path)],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        return SimpleNamespace(output=result.stdout)

    layer = WorkbenchFilesLayer(config=LayerConfig(), inner_api_url="", inner_api_key="")
    shell = SimpleNamespace(
        _require_workspace_cwd=lambda: "/workspace/conversations/current", run_remote_script_complete=execute
    )
    changes = WorkbenchFileChanges(shell=cast(DifyShellLayer, shell), activity=None, files=layer)

    async def scenario():
        await changes.start()
        (other / "报告.md").write_text("report", encoding="utf-8")
        (tmp_path / "memory.md").write_text("preferences", encoding="utf-8")
        (tmp_path / "skills").mkdir()
        (tmp_path / "skills/SKILL.md").write_text("skill", encoding="utf-8")
        await changes.collect(explicit_path="/workspace/conversations/other/报告.md")
        assert layer.runtime_state.changed_paths == {"conversations/other/报告.md"}
        assert layer.delivery_error("完成。", final=True)
        layer._verified["conversations/other/报告.md"] = {"download_url": DOWNLOAD, "preview_url": PREVIEW}
        assert layer.delivery_error(f"[下载]({DOWNLOAD})", final=True) is None
        (other / "报告.md").unlink()
        await changes.collect()
        assert not layer.runtime_state.changed_paths

    asyncio.run(scenario())
    assert len(commands) == 3


def add_files(request):
    context = next(layer.config for layer in request.composition.layers if layer.name == "execution_context")
    context.workbench_run_id, context.user_id, context.app_id = "turn-1", "owner-1", "app-1"
    request.composition.layers.append(
        RunLayerSpec(
            name="workbench_files",
            type="dify.workbench_files",
            deps={"execution_context": "execution_context"},
            config={},
        )
    )


def test_agent_requests_sidebar_preview_then_delivers_without_a_download_link(monkeypatch):
    calls = 0

    async def stream(messages, info):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield {0: _call("open_file_preview", {"path": "报告.docx"}, "preview-call")}
        else:
            yield "文件已生成，已请求打开侧栏预览。"

    request, sink, _ = _setup(monkeypatch, stream)
    add_files(request)
    requests = []

    def transport(request):
        body = json.loads(request.content)
        requests.append(body)
        assert str(request.url).endswith("/agent/workbench/files/preview")
        assert body["backend_run_id"] == "preview-native" and body["request_key"]
        return httpx.Response(
            200, json={"accepted": True, "file": {"path": "conversations/chat/报告.docx", "kind": "file"}}
        )

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            await AgentRunRunner(
                run_id="preview-native",
                request=request,
                sink=sink,
                plugin_daemon_http_client=client,
                dify_api_http_client=client,
            ).run()

    asyncio.run(scenario())
    events = sink.events["preview-native"]
    assert isinstance(events[-1], RunSucceededEvent)
    assert calls == 2 and len(requests) == 1
    text = "".join(item.text for item in _progress(events, "text"))
    assert text == "文件已生成，已请求打开侧栏预览。"


def test_concurrent_workspace_writes_preserve_unmodified_delivery_links():
    layer = WorkbenchFilesLayer(config=LayerConfig(), inner_api_url="", inner_api_key="")
    path = "conversations/current/报告.docx"
    layer.record_changes([path])
    layer._verified[path] = {"download_url": DOWNLOAD, "preview_url": PREVIEW, "kind": "file"}
    layer.record_changes(["conversations/other/预览.pdf", "conversations/current/build.py"])
    assert layer.delivery_error(f"[报告.docx]({DOWNLOAD})", final=True) is None
    layer.record_changes([], ["conversations/other/预览.pdf"])
    assert layer.delivery_error(f"[报告.docx]({DOWNLOAD})", final=True) is None
    layer.record_changes([path])
    assert layer.delivery_error(f"[报告.docx]({DOWNLOAD})", final=True) is not None


@pytest.mark.parametrize("removed", [False, True])
def test_changed_archive_member_invalidates_only_its_containing_directory(removed):
    layer = WorkbenchFilesLayer(config=LayerConfig(), inner_api_url="", inner_api_key="")
    layer._verified["conversations/current"] = {"download_url": DOWNLOAD, "preview_url": PREVIEW, "kind": "directory"}
    layer._verified["conversations/current2"] = {
        "download_url": DOWNLOAD + "2",
        "preview_url": PREVIEW + "2",
        "kind": "directory",
    }
    changed = ["conversations/current/报告.docx"]
    layer.record_changes([] if removed else changed, changed if removed else None)
    assert "conversations/current" not in layer._verified
    assert "conversations/current2" in layer._verified


def test_modifying_a_previewed_file_requires_a_new_delivery_request():
    layer = WorkbenchFilesLayer(config=LayerConfig(), inner_api_url="", inner_api_key="")
    layer.record_changes(["conversations/chat/报告.docx"])
    layer.runtime_state.opened_paths.add("conversations/chat/报告.docx")
    assert layer.delivery_error("文件已生成。", final=True) is None
    layer.record_changes(["conversations/chat/报告.docx"])
    assert layer.delivery_error("文件已生成。", final=True) is not None


def test_presented_files_require_new_verification_after_modification_or_removal():
    layer = WorkbenchFilesLayer(config=LayerConfig(), inner_api_url="", inner_api_key="")
    path = "conversations/chat/report.pdf"
    layer.record_changes([path])
    layer.runtime_state.presented_paths.add(path)
    assert layer.delivery_error("文件已生成。", final=True) is None
    layer.record_changes(["conversations/chat/script.py"])
    assert layer.delivery_error("文件已生成。", final=True) is None
    layer.record_changes([path])
    assert not layer.runtime_state.presented_paths
    assert layer.delivery_error("文件已生成。", final=True) is not None


@pytest.mark.parametrize("invalid", [False, True])
def test_present_files_uses_owned_lookup_and_persists_only_verified_deliveries(monkeypatch, invalid):
    calls = 0

    async def stream(messages, info):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield {
                0: _call("present_files", {"files": [{"path": "报告.pdf", "description": "核验后的报告"}]}, "deliver")
            }
        else:
            yield "文件交付受阻，查询失败。" if invalid else "文件已生成，请打开下方卡片。"

    request, sink, _ = _setup(monkeypatch, stream)
    add_files(request)
    requested = []

    def transport(incoming):
        body = json.loads(incoming.content)
        requested.append(body)
        assert str(incoming.url).endswith("/agent/workbench/files")
        assert body["account_id"] == "owner-1" and body["workbench_run_id"] == "turn-1"
        return httpx.Response(
            200,
            json={
                "cwd": "conversations/chat",
                "entries": []
                if invalid
                else [
                    {
                        "path": "conversations/chat/报告.pdf",
                        "name": "报告.pdf",
                        "kind": "file",
                        "size": 42,
                        "preview_url": PREVIEW,
                        "download_url": DOWNLOAD,
                    }
                ],
            },
        )

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            await AgentRunRunner(
                run_id="delivery",
                request=request,
                sink=sink,
                plugin_daemon_http_client=client,
                dify_api_http_client=client,
            ).run()

    asyncio.run(scenario())
    events = sink.events["delivery"]
    assert isinstance(events[-1], RunSucceededEvent), events[-1]
    state = next(
        layer.runtime_state for layer in events[-1].data.session_snapshot.layers if layer.name == "workbench_files"
    )
    assert state["presented_paths"] == ([] if invalid else ["conversations/chat/报告.pdf"])
    returns = [
        item for item in _progress(events, "tool") if item.stage != "started" and item.tool_name == "present_files"
    ]
    assert len(returns) == 1 and len(requested) == 1
    if invalid:
        assert returns[0].stage == "error" and "files" not in returns[0].output
    else:
        assert returns[0].output["files"][0]["description"] == "核验后的报告"


def test_invented_split_stream_is_withheld_then_model_queries_and_corrects(monkeypatch):
    calls = 0

    async def stream(messages, info):
        nonlocal calls
        calls += 1
        if calls == 1:
            for chunk in [
                "文件已在文件空间可见。[下载](https://files.",
                "example.test/files/workbench/guessed/",
                "chart.png?mode=download)",
            ]:
                yield chunk
        elif calls == 2:
            yield {0: _call("workbench_files", {"path": "chart.png"}, "files")}
        else:
            yield f"文件已在文件空间可见。[下载]({DOWNLOAD})\n![图片]({PREVIEW})"

    request, sink, _ = _setup(monkeypatch, stream)
    add_files(request)
    lookups = []

    def transport(request):
        lookups.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "entries": [{"path": "chart.png", "preview_url": PREVIEW, "download_url": DOWNLOAD}],
                "complete": True,
            },
        )

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            await AgentRunRunner(
                run_id="files",
                request=request,
                sink=sink,
                plugin_daemon_http_client=client,
                dify_api_http_client=client,
            ).run()

    asyncio.run(scenario())
    events = sink.events["files"]
    assert isinstance(events[-1], RunSucceededEvent)
    assert len(lookups) == 1 and calls == 3
    text = "".join(item.text for item in _progress(events, "text"))
    assert "/guessed/" not in text and DOWNLOAD in text and PREVIEW in text
    assert text.count("文件已在文件空间可见。") == 1
    public_stream = [event for event in events if event.type == "pydantic_ai_event"]
    assert public_stream
    assert all("/guessed/" not in json.dumps(event.model_dump(mode="json")) for event in public_stream)


def test_persistent_invented_link_fails_without_streaming_it(monkeypatch):
    async def stream(messages, info):
        yield "[下载](https://files.example.test/files/guessed.pdf)"

    request, sink, _ = _setup(monkeypatch, stream)
    add_files(request)

    async def scenario():
        async with httpx.AsyncClient() as client:
            with pytest.raises(UnexpectedModelBehavior):
                await AgentRunRunner(
                    run_id="invalid",
                    request=request,
                    sink=sink,
                    plugin_daemon_http_client=client,
                    dify_api_http_client=client,
                ).run()

    asyncio.run(scenario())
    assert not _progress(sink.events["invalid"], "text")
    assert sink.events["invalid"][-1].type == "run_failed"


def test_old_deferred_task_can_acquire_file_reader_without_repeating_work(monkeypatch):
    requests = 0

    async def stream(messages, info):
        nonlocal requests
        requests += 1
        if requests == 1:
            yield {0: _call("update_shared_environment", {"reason": "需要依赖", "python": ["pillow"]}, "env")}
        elif requests == 2:
            yield {0: _call("workbench_files", {"path": "chart.png"}, "files")}
        else:
            yield f"[下载]({DOWNLOAD})\n![图]({PREVIEW})"

    request, sink, execute = _setup(monkeypatch, stream)
    context = next(layer.config for layer in request.composition.layers if layer.name == "execution_context")
    context.workbench_run_id, context.user_id, context.app_id = "turn-1", "owner-1", "app-1"
    request.composition.layers.append(
        RunLayerSpec(name="workbench_environment", type="dify.workbench_environment", config={})
    )
    first = asyncio.run(execute("before-upgrade"))
    snapshot = first[-1].data.session_snapshot
    add_files(request)
    request.session_snapshot = snapshot.model_copy(
        update={
            "layers": [
                *snapshot.layers,
                LayerSessionSnapshot(name="workbench_files", lifecycle_state=LifecycleState.NEW, runtime_state={}),
            ]
        }
    )
    request.deferred_tool_results = DeferredToolResultsPayload(calls={"env": {"status": "completed"}})

    async def resume():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, json={"entries": [{"path": "chart.png", "preview_url": PREVIEW, "download_url": DOWNLOAD}]}
                )
            )
        ) as client:
            await AgentRunRunner(
                run_id="after-upgrade",
                request=request,
                sink=sink,
                plugin_daemon_http_client=client,
                dify_api_http_client=client,
            ).run()

    asyncio.run(resume())
    assert isinstance(sink.events["after-upgrade"][-1], RunSucceededEvent)
    assert requests == 3
    resumed = _progress(sink.events["after-upgrade"], "tool")
    assert not any(item.stage == "started" and item.tool_name == "update_shared_environment" for item in resumed)
    assert DOWNLOAD in "".join(item.text for item in _progress(sink.events["after-upgrade"], "text"))


@pytest.mark.parametrize(
    "text, valid",
    [
        ("[资料](https://example.test/docs)", True),
        ("[Download Python](https://python.org/downloads/)", True),
        ("[下载文档](https://example.test/docs)", True),
        (f"![图][preview]\n\n[preview]: {PREVIEW}", True),
        (f"[下载]({PREVIEW})", False),
        (f"![图]({DOWNLOAD})", False),
        ("[下载](sandbox:/workspace/report.docx)", False),
        ('<img src="https://example.test/guessed.png">', False),
        ("[下载](report.docx)", False),
        ("[查看报告](https://files.example.test/generated/report.docx)", False),
        ("```markdown\n![示例](https://example.test/example.png)\n```", True),
    ],
)
def test_markdown_targets_and_examples(text, valid):
    layer = WorkbenchFilesLayer(config=LayerConfig(), inner_api_url="", inner_api_key="")
    layer._verified["chart"] = {"preview_url": PREVIEW, "download_url": DOWNLOAD}
    assert (layer.delivery_error(text, final=True) is None) is valid


@pytest.mark.parametrize("activity_enabled", [False, True])
@pytest.mark.parametrize("tool_name", ["shell_run", "file_create", "file_edit"])
def test_wrapped_arguments_recover_after_invalid_attempts_with_activity_disabled(
    monkeypatch, activity_enabled, tool_name
):
    executed = []

    def shell_run(script: str) -> str:
        executed.append({"script": script})
        return "done"

    def file_create(path: str, content: str) -> str:
        executed.append({"path": path, "content": content})
        return "done"

    def file_edit(path: str, old_text: str, new_text: str) -> str:
        executed.append({"path": path, "old_text": old_text, "new_text": new_text})
        return "done"

    tool, expected = {
        "shell_run": (shell_run, {"script": "echo ready"}),
        "file_create": (file_create, {"path": "note.txt", "content": "ready"}),
        "file_edit": (file_edit, {"path": "note.txt", "old_text": "old", "new_text": "ready"}),
    }[tool_name]
    calls = 0

    async def stream(messages, info):
        nonlocal calls
        calls += 1
        if calls <= 3:
            nested = '{"truncated":' if calls <= 2 else json.dumps(expected)
            yield {0: _call(tool_name, {"arguments": nested}, f"attempt-{calls}")}
        else:
            yield "操作完成。"

    request, _, execute = _setup(monkeypatch, stream, [Tool(tool, max_retries=2)])
    add_files(request)
    if not activity_enabled:
        request.composition.layers = [
            layer for layer in request.composition.layers if layer.name != "workbench_activity"
        ]
    asyncio.run(execute())
    assert calls == 4
    assert executed == [expected]


@pytest.mark.parametrize("activity_enabled", [False, True])
@pytest.mark.parametrize("suspend", [False, True])
def test_runner_observes_binary_creation_and_editing_and_exports_real_events(
    monkeypatch, tmp_path, activity_enabled, suspend
):
    calls = 0
    old_url = "https://files.example.test/files/workbench/signed/old.txt?mode=download"
    conversation = tmp_path / "conversations/chat"
    conversation.mkdir(parents=True)
    (conversation / "old.txt").write_text("already existed", encoding="utf-8")

    async def stream(messages, info):
        nonlocal calls
        calls += 1
        if calls <= 2:
            yield {0: _call("shell_run", {"script": "create" if calls == 1 else "edit"}, f"shell-{calls}")}
        elif calls == 3 and suspend:
            yield {0: _call("update_shared_environment", {"reason": "需要依赖", "python": ["pillow"]}, "env")}
        elif calls == 3 + int(suspend):
            yield {0: _call("workbench_files", {}, "files")}
        elif calls == 4 + int(suspend):
            yield f"[下载旧文件]({old_url})"
        else:
            yield f"文件已生成，可打开查看。[下载图片]({DOWNLOAD})\n![图片]({PREVIEW})"

    request, sink, _ = _setup(monkeypatch, stream)
    add_files(request)
    if suspend:
        request.composition.layers.append(
            RunLayerSpec(name="workbench_environment", type="dify.workbench_environment", config={})
        )
    if not activity_enabled:
        request.composition.layers = [
            layer for layer in request.composition.layers if layer.type != "dify.workbench_activity"
        ]
    request.composition.layers.extend(
        [
            RunLayerSpec(
                name="runtime", type="dify.runtime", config=DifyRuntimeLayerConfig(backend_binding_ref="binding-1")
            ),
            RunLayerSpec(
                name="shell",
                type="dify.shell",
                deps={"execution_context": "execution_context", "runtime": "runtime"},
                config=DifyShellLayerConfig(),
            ),
        ]
    )

    async def remote(self, script, **kwargs):
        args = shlex.split(script)
        assert args[:2] == ["python3", "-c"]
        assert args[-2:] == ["", "/workspace"]
        result = subprocess.run(
            [sys.executable, "-c", args[2], "", str(tmp_path)],
            cwd=conversation,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        return SimpleNamespace(output=result.stdout)

    async def shell_run(self, script: str, timeout: float = 10):
        if script == "create":
            code = "from pathlib import Path; import zipfile, base64; z=zipfile.ZipFile('报告.docx','w'); z.writestr('word/document.xml','<document/>'); z.close(); Path('chart.png').write_bytes(base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6ZpAAAAAASUVORK5CYII=')); p=Path('playwright_chromiumdev_profile-test/Default'); p.mkdir(parents=True); (p/'History').write_text('browser cache'); p=Path('workbench-office-test/user'); p.mkdir(parents=True); (p/'registrymodifications.xcu').write_text('office settings')"
        else:
            code = "import zipfile; z=zipfile.ZipFile('报告.docx','a'); z.writestr('word/styles.xml','<styles/>'); z.close()"
        subprocess.run([sys.executable, "-c", code], cwd=conversation, check=True)
        return {"done": True, "exit_code": 0}

    monkeypatch.setattr(DifyShellLayer, "run_remote_script_complete", remote)
    monkeypatch.setattr(DifyShellLayer, "_require_workspace_cwd", lambda self: "/workspace/conversations/chat")
    monkeypatch.setattr(DifyShellLayer, "_tool_run", shell_run)
    profile = RuntimeBackendProfile(
        home_snapshots=cast(HomeSnapshotBackend, object()),
        execution_bindings=FakeRunnerExecutionBindingBackend(FakeRunnerShellctlClient()),
    )

    def transport(request):
        return httpx.Response(
            200,
            json={
                "entries": [
                    {"path": "conversations/chat/chart.png", "preview_url": PREVIEW, "download_url": DOWNLOAD},
                    {"path": "conversations/chat/old.txt", "preview_url": old_url, "download_url": old_url},
                ],
                "complete": True,
            },
        )

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            for identifier in ["binary", "binary-resume"] if suspend else ["binary"]:
                if identifier == "binary-resume":
                    snapshot = sink.events["binary"][-1].data.session_snapshot
                    pending = next(layer for layer in snapshot.layers if layer.name == "workbench_files")
                    assert set(pending.runtime_state["changed_paths"]) == {
                        "conversations/chat/chart.png",
                        "conversations/chat/报告.docx",
                    }
                    request.session_snapshot = snapshot
                    request.deferred_tool_results = DeferredToolResultsPayload(calls={"env": {"status": "completed"}})
                await AgentRunRunner(
                    run_id=identifier,
                    request=request,
                    sink=sink,
                    plugin_daemon_http_client=client,
                    dify_api_http_client=client,
                    layer_providers=create_default_layer_providers(runtime_backend_profile=profile),
                ).run()

    asyncio.run(scenario())
    events = sink.events["binary"] + (sink.events["binary-resume"] if suspend else [])
    assert isinstance(events[-1], RunSucceededEvent)
    assert calls == 5 + int(suspend)
    public_stream = "".join(
        json.dumps(event.model_dump(mode="json"), ensure_ascii=False)
        for event in events
        if event.type == "pydantic_ai_event"
    )
    assert "下载旧文件" not in public_stream and DOWNLOAD in public_stream
    observed = [
        item
        for item in _progress(events, "tool")
        if item.stage == "returned" and isinstance(item.output, dict) and item.output.get("source") == "workspace_image"
    ]
    expected = [
        ("image_preview", "conversations/chat/chart.png"),
    ]
    assert [(item.tool_name, item.output["path"]) for item in observed] == (expected if activity_enabled else [])
    target = os.environ.get("WORKBENCH_EVENT_FIXTURE")
    if target and activity_enabled and not suspend:
        values = [
            {"event": "workbench_activity", "_id": f"{i + 1}-0", "data": event.data.model_dump(mode="json")}
            for i, event in enumerate(events)
            if isinstance(event, WorkbenchActivityRunEvent)
        ]
        Path(target).write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")


def test_new_logical_run_clears_pending_files_but_resume_preserves_them():
    context = DifyExecutionContextLayer(
        config=DifyExecutionContextLayerConfig(
            tenant_id="tenant", agent_mode="agent_app", invoke_from="web-app", workbench_run_id="first"
        ),
        daemon_url="",
        daemon_api_key="",
    )
    layer = WorkbenchFilesLayer(config=LayerConfig(), inner_api_url="", inner_api_key="")
    layer.bind_deps({"execution_context": context})
    asyncio.run(layer.on_context_create())
    layer.record_changes(["chart.png", "removed.txt"])
    layer.record_changes([], ["removed.txt"])
    layer._verified["chart.png"] = {"preview_url": PREVIEW, "download_url": DOWNLOAD}
    asyncio.run(layer.on_context_resume())
    assert layer.runtime_state.changed_paths == {"chart.png"}
    assert not layer._verified
    assert layer.delivery_error("操作完成。", final=True)
    context.config.workbench_run_id = "next"
    asyncio.run(layer.on_context_resume())
    assert not layer.runtime_state.changed_paths
    assert layer.delivery_error("操作完成。", final=True) is None


def test_unavailable_download_can_be_reported_without_a_fabricated_link(monkeypatch):
    calls = 0

    async def stream(messages, info):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield {0: _call("workbench_files", {"path": "large.bin"}, "files")}
        else:
            yield "文件已生成，但文件下载受阻，需要拆分过大的文件。"

    request, sink, _ = _setup(monkeypatch, stream)
    add_files(request)

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"entries": [{"path": "large.bin", "downloadable": False}]})
            )
        ) as client:
            await AgentRunRunner(
                run_id="unavailable",
                request=request,
                sink=sink,
                plugin_daemon_http_client=client,
                dify_api_http_client=client,
            ).run()

    asyncio.run(scenario())
    assert calls == 2
    assert isinstance(sink.events["unavailable"][-1], RunSucceededEvent)
    assert "文件下载受阻" in "".join(item.text for item in _progress(sink.events["unavailable"], "text"))


@pytest.mark.parametrize("failure", ["http", "unavailable"])
def test_file_changes_require_a_new_lookup_after_a_previous_failure(failure):
    context = DifyExecutionContextLayer(
        config=DifyExecutionContextLayerConfig(
            tenant_id="tenant",
            agent_mode="agent_app",
            invoke_from="web-app",
            workbench_run_id="run",
            user_from="account",
            user_id="owner",
            app_id="app",
        ),
        daemon_url="",
        daemon_api_key="",
    )
    layer = WorkbenchFilesLayer(config=LayerConfig(), inner_api_url="https://api.example.test", inner_api_key="")
    layer.bind_deps({"execution_context": context})
    calls = 0
    layer.bind_directory("conversations/chat/")

    def transport(request):
        nonlocal calls
        calls += 1
        if calls in {1, 3}:
            return (
                httpx.Response(503)
                if failure == "http"
                else httpx.Response(
                    200, json={"entries": [{"path": "conversations/chat/chart.png", "downloadable": False}]}
                )
            )
        return httpx.Response(
            200,
            json={
                "entries": [
                    {
                        "path": "conversations/chat/chart.png",
                        "preview_url": PREVIEW,
                        "download_url": DOWNLOAD,
                    }
                ]
            },
        )

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            tool = (await layer.get_tools(http_client=client))[0]
            await tool.function(None, path="chart.png")
            layer.record_changes(["conversations/chat/chart.png"])
            assert layer.delivery_error("文件下载受阻。", final=True) is not None
            await tool.function(None, path="chart.png")
            assert layer.delivery_error(f"[下载]({DOWNLOAD})", final=True) is None
            await tool.function(None, path="chart.png")
            assert layer.delivery_error(f"[下载]({DOWNLOAD})", final=True) is not None

    asyncio.run(scenario())
    assert calls == 3


@pytest.mark.parametrize(
    "path,kind,valid",
    [
        ("old/chart.png", "file", False),
        ("new/chart.png", "file", True),
        ("new", "directory", True),
        ("ne", "directory", False),
    ],
)
def test_delivered_url_matches_generated_path_or_containing_directory(path, kind, valid):
    layer = WorkbenchFilesLayer(config=LayerConfig(), inner_api_url="", inner_api_key="")
    layer.record_changes(["new/chart.png"])
    layer._verified[path] = {"preview_url": PREVIEW, "download_url": DOWNLOAD, "kind": kind}
    assert (layer.delivery_error(f"[下载]({DOWNLOAD})", final=True) is None) is valid
