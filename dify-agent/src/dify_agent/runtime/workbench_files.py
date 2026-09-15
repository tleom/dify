"""Observe sandbox file changes and hold answer text until its file links are verified."""

import asyncio
import json
import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from pydantic_ai import ModelRetry
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelResponse

from dify_agent.layers.shell.layer import DifyShellLayer
from dify_agent.layers.workbench_files import WorkbenchFilesLayer
from dify_agent.protocol.schemas import WorkbenchToolData
from dify_agent.runtime.workbench_activity import WorkbenchActivityCapability

SNAPSHOT_SCRIPT = """
import json, os, stat
from pathlib import Path
root = Path.cwd().resolve()
files = {}
# TMPDIR points at the conversation directory. Browser and Office profiles are
# runtime housekeeping, not generated deliverables or report-building scripts.
ignored_names = {
    '.cache', '.dify_conf', '.git', '.mypy_cache', '.pytest_cache', '.ruff_cache',
    '.venv', '__pycache__', 'node_modules', 'venv',
}
ignored_prefixes = (
    'workbench-office-', 'playwright_chromiumdev_profile-',
    'playwright_firefoxdev_profile-', 'playwright_webkitdev_profile-',
    'playwright-artifacts-', 'puppeteer_dev_chrome_profile-',
    'puppeteer_dev_firefox_profile-',
)
ignored_suffixes = ('.log', '.tmp', '.temp', '.pyc', '.pyo', '.swp', '.swo', '~')
def visible(name, directory=False):
    name = name.lower()
    return not (
        name in ignored_names or name.startswith(('.workbench-edit-', 'com.google.chrome.chrome_chrome_url_fetcher_'))
        or (directory and name.startswith(ignored_prefixes)) or name.endswith(ignored_suffixes)
    )
for directory, names, filenames in os.walk(root, followlinks=False):
    names[:] = sorted(name for name in names if visible(name, directory=True) and not (Path(directory) / name).is_symlink())
    for name in sorted(filenames):
        if not visible(name):
            continue
        path = Path(directory) / name
        try:
            value = path.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(value.st_mode):
            continue
        if len(files) >= 10000:
            raise RuntimeError('Conversation file inventory exceeds 10000 files')
        files[path.relative_to(root).as_posix()] = [value.st_size, value.st_mtime_ns, value.st_ctime_ns, value.st_ino]
print(json.dumps(files, ensure_ascii=False))
"""


@dataclass
class WorkbenchFileChanges:
    shell: DifyShellLayer
    activity: WorkbenchActivityCapability | None
    files: WorkbenchFilesLayer | None = None
    previous: dict[str, list[int]] = field(default_factory=dict)
    sequence: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def snapshot(self) -> dict[str, list[int]]:
        result = await self.shell.run_remote_script_complete(
            "python3 -c " + shlex.quote(SNAPSHOT_SCRIPT), timeout=15, max_output_bytes=4 * 1024 * 1024
        )
        value = json.loads(result.output)
        if not isinstance(value, dict) or any(not isinstance(item, list) or len(item) != 4 for item in value.values()):
            raise ValueError("无法读取当前会话的文件变更记录")
        return value

    async def start(self) -> None:
        self.previous = await self.snapshot()
        if self.files is not None:
            removed = self.files.runtime_state.changed_paths - self.previous.keys()
            if removed:
                self.files.record_changes([], list(removed))

    async def collect(self, *, explicit_path: str | None = None) -> None:
        # Parallel tools share one inventory; serialized observations never duplicate a change.
        async with self.lock:
            current = await self.snapshot()
            changed = [path for path in current if self.previous.get(path) != current[path]]
            removed = self.previous.keys() - current.keys()
            if self.files is not None and (changed or removed):
                self.files.record_changes(changed, list(removed))
            if self.activity is None:
                self.previous = current
                return
            root = self.shell._require_workspace_cwd().rstrip("/")
            explicit = explicit_path.removeprefix(root + "/") if explicit_path else None
            for path in changed:
                if path == explicit:
                    continue
                self.sequence += 1
                identifier = f"{self.activity.run_id}:file-change:{self.sequence}"
                name = "file_edit" if path in self.previous else "file_create"
                common = dict(
                    workbench_run_id=self.activity.layer.config.workbench_run_id,
                    call_id=identifier,
                    tool_call_id=identifier,
                    tool_name=name,
                    activity_id=self.activity.layer.runtime_state.current_id,
                )
                await self.activity.emit(WorkbenchToolData(**common, stage="started", input={"path": path}))
                await self.activity.emit(
                    WorkbenchToolData(
                        **common,
                        stage="returned",
                        output={"path": path, "bytes": current[path][0], "source": "workspace_change"},
                    )
                )
            self.previous = current


@dataclass
class WorkbenchFileDeliveryCapability(AbstractCapability[None]):
    files: WorkbenchFilesLayer
    publish: Callable[[ModelResponse, int], Awaitable[None]]
    ready: Callable[[], bool]
    changes: WorkbenchFileChanges | None = None

    async def after_model_request(self, ctx, *, request_context, response: ModelResponse) -> ModelResponse:
        if self.changes is not None:
            await self.changes.collect()
        text = response.text or ""
        if text:
            error = self.files.delivery_error(text, final=not response.tool_calls)
            if error:
                raise ModelRetry(error)
            if self.ready():
                await self.publish(response, ctx.run_step)
        return response
