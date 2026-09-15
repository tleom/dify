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
from pydantic_ai.exceptions import UnexpectedModelBehavior

from agenton.layers import LayerConfig
from agenton.layers import LifecycleState
from agenton.compositor import LayerSessionSnapshot
from dify_agent.layers.shell.layer import DifyShellLayer, DifyShellLayerConfig
from dify_agent.layers.runtime import DifyRuntimeLayerConfig
from dify_agent.layers.workbench_files import WorkbenchFilesLayer
from dify_agent.protocol import DeferredToolResultsPayload, RunLayerSpec, RunSucceededEvent, WorkbenchActivityRunEvent
from dify_agent.runtime.compositor_factory import create_default_layer_providers
from dify_agent.runtime.runner import AgentRunRunner
from dify_agent.runtime_backend import HomeSnapshotBackend, RuntimeBackendProfile
from .test_runner import FakeRunnerExecutionBindingBackend, FakeRunnerShellctlClient
from .test_workbench_activity import _setup, _progress, _call

PREVIEW = "https://files.example.test/files/workbench/signed/chart.png?mode=preview"
DOWNLOAD = "https://files.example.test/files/workbench/signed/chart.png?mode=download"


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


def test_runner_observes_binary_creation_and_editing_and_exports_real_events(monkeypatch, tmp_path):
    calls = 0

    async def stream(messages, info):
        nonlocal calls
        calls += 1
        if calls <= 2:
            yield {0: _call("shell_run", {"script": "create" if calls == 1 else "edit"}, f"shell-{calls}")}
        elif calls == 3:
            yield {0: _call("workbench_files", {}, "files")}
        else:
            yield f"文件已生成，可打开查看。[下载图片]({DOWNLOAD})\n![图片]({PREVIEW})"

    request, sink, _ = _setup(monkeypatch, stream)
    add_files(request)
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
        result = subprocess.run(
            [sys.executable, "-c", args[2], *args[3:]],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        return SimpleNamespace(output=result.stdout)

    async def shell_run(self, script: str, timeout: float = 10):
        if script == "create":
            code = "from pathlib import Path; import zipfile, base64; z=zipfile.ZipFile('报告.docx','w'); z.writestr('word/document.xml','<document/>'); z.close(); Path('chart.png').write_bytes(base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6ZpAAAAAASUVORK5CYII='))"
        else:
            code = "import zipfile; z=zipfile.ZipFile('报告.docx','a'); z.writestr('word/styles.xml','<styles/>'); z.close()"
        subprocess.run([sys.executable, "-c", code], cwd=tmp_path, check=True)
        return {"done": True, "exit_code": 0}

    monkeypatch.setattr(DifyShellLayer, "run_remote_script_complete", remote)
    monkeypatch.setattr(DifyShellLayer, "_require_workspace_cwd", lambda self: tmp_path.as_posix())
    monkeypatch.setattr(DifyShellLayer, "_tool_run", shell_run)
    profile = RuntimeBackendProfile(
        home_snapshots=cast(HomeSnapshotBackend, object()),
        execution_bindings=FakeRunnerExecutionBindingBackend(FakeRunnerShellctlClient()),
    )

    def transport(request):
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
                run_id="binary",
                request=request,
                sink=sink,
                plugin_daemon_http_client=client,
                dify_api_http_client=client,
                layer_providers=create_default_layer_providers(runtime_backend_profile=profile),
            ).run()

    asyncio.run(scenario())
    events = sink.events["binary"]
    assert isinstance(events[-1], RunSucceededEvent)
    observed = [
        item
        for item in _progress(events, "tool")
        if item.stage == "returned"
        and isinstance(item.output, dict)
        and item.output.get("source") == "workspace_change"
    ]
    assert [(item.tool_name, item.output["path"]) for item in observed] == [
        ("file_create", "chart.png"),
        ("file_create", "报告.docx"),
        ("file_edit", "报告.docx"),
    ]
    target = os.environ.get("WORKBENCH_EVENT_FIXTURE")
    if target:
        values = [
            {"event": "workbench_activity", "_id": f"{i + 1}-0", "data": event.data.model_dump(mode="json")}
            for i, event in enumerate(events)
            if isinstance(event, WorkbenchActivityRunEvent)
        ]
        Path(target).write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")
