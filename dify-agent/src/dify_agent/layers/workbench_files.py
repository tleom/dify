"""Current-chat file discovery with server-issued preview and download URLs."""

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from typing import ClassVar, Self

import httpx
from markdown_it import MarkdownIt
from pydantic import BaseModel, Field
from pydantic_ai import RunContext, Tool

from agenton.layers import LayerConfig, LayerDeps, PlainLayer
from dify_agent.layers.execution_context.layer import DifyExecutionContextLayer


class WorkbenchFilesDeps(LayerDeps):
    execution_context: DifyExecutionContextLayer


class WorkbenchFilesState(BaseModel):
    workbench_run_id: str | None = None
    changed_paths: set[str] = Field(default_factory=set)
    path_protocol: int = 0


@dataclass
class WorkbenchFilesLayer(PlainLayer[WorkbenchFilesDeps, LayerConfig, WorkbenchFilesState]):
    type_id: ClassVar[str | None] = "dify.workbench_files"
    config: LayerConfig
    inner_api_url: str
    inner_api_key: str
    _verified: dict[str, dict[str, str]] = field(default_factory=dict, init=False, repr=False)
    _lookup_failed: bool = field(default=False, init=False, repr=False)
    _directory: str = field(default="", init=False, repr=False)

    async def on_context_create(self) -> None:
        self.runtime_state = WorkbenchFilesState(
            workbench_run_id=self.deps.execution_context.config.workbench_run_id, path_protocol=1
        )

    async def on_context_resume(self) -> None:
        if self.runtime_state.workbench_run_id != self.deps.execution_context.config.workbench_run_id:
            await self.on_context_create()
        self._verified.clear()
        self._lookup_failed = False

    def bind_directory(self, prefix: str) -> None:
        self._directory = prefix.rstrip("/") + "/"
        if self.runtime_state.path_protocol == 0:
            self.runtime_state.changed_paths = {prefix + path for path in self.runtime_state.changed_paths}
            self.runtime_state.path_protocol = 1

    def canonical_path(self, path: str) -> str:
        if path.startswith(("/workspace", "conversations/")):
            return _relative_path(path)
        return (self._directory + _relative_path(path)).rstrip("/")

    def record_changes(self, paths: list[str], removed: list[str] | None = None) -> None:
        self.runtime_state.changed_paths.update(paths)
        self.runtime_state.changed_paths.difference_update(removed or [])
        # Fixed URLs survive edits, but delivery must confirm the current file exists.
        self._verified.clear()
        self._lookup_failed = False

    def covers_changed_path(self, path: str, *, directory: bool) -> bool:
        return any(
            changed == path or (directory and (not path or changed.startswith(path.rstrip("/") + "/")))
            for changed in self.runtime_state.changed_paths
        )

    def delivery_error(self, text: str, *, final: bool) -> str | None:
        """Validate rendered Markdown targets, including reference links and split model deltas."""
        previews = {item["preview_url"] for item in self._verified.values() if item.get("preview_url")}
        downloads = {item["download_url"] for item in self._verified.values() if item.get("download_url")}
        supplied: set[str] = set()
        for block in MarkdownIt().parse(text):
            if block.type == "html_block" and re.search(r"<(?:img|a)\b", block.content, re.I):
                return "文件交付请使用 Markdown 链接和图片语法，不能使用 HTML 标签。"
            children = block.children or []
            for index, token in enumerate(children):
                if token.type == "image":
                    if token.attrGet("src") not in previews:
                        return "图片地址必须逐字使用 workbench_files 返回的 preview_url。"
                elif token.type == "html_inline" and re.search(r"<(?:img|a)\b", token.content, re.I):
                    return "文件交付请使用 Markdown 链接和图片语法，不能使用 HTML 标签。"
                elif token.type == "link_open":
                    url = str(token.attrGet("href") or "")
                    label = ""
                    for child in children[index + 1 :]:
                        if child.type == "link_close":
                            break
                        label += child.content
                    download = bool(re.search(r"下载|download", label, re.I))
                    if _file_target(url) or re.search(r"\.(?:docx?|xlsx?|pptx?|pdf|zip|png|jpe?g)\b", label, re.I):
                        if url not in (downloads if download else previews | downloads):
                            return "文件链接尚未核对。先调用 workbench_files，再逐字使用它返回的实际链接。"
                    if url in downloads:
                        supplied.add(url)
                elif token.type == "text":
                    for match in re.finditer(
                        r"(?:https?://|sandbox:|file:|/workspace/|conversations/)[^\s<>]+", token.content
                    ):
                        url = match.group().rstrip("。，,;；)）")
                        if _file_target(url) and url not in previews | downloads:
                            return "请勿提供本地、沙箱或未经文件空间查询确认的文件地址。"
        delivered = bool(re.search(r"文件已(?:生成|保存|在文件空间)|可(?:直接)?下载|可打开查看", text))
        blocked = self._lookup_failed and bool(
            re.search(
                r"(?:查询|交付|下载|链接|文件空间).{0,16}(?:失败|受阻|不可用|无法)|无法.{0,16}(?:确认|下载|交付|查询)",
                text,
            )
        )
        supplied_changed = any(
            item["download_url"] in supplied
            and self.covers_changed_path(path, directory=item.get("kind") == "directory")
            for path, item in self._verified.items()
        )
        if final and self.runtime_state.changed_paths and not supplied_changed and not blocked:
            return "请查询并提供本次生成或修改文件对应的实际 download_url，不能用其他已有文件的链接代替。"
        if delivered and not supplied and not blocked:
            return "交付前先查询文件空间确认产物可见，并提供实际 download_url；如查询失败，请明确说明交付受阻。"
        return None

    @classmethod
    def from_config(cls, config: LayerConfig) -> Self:
        raise TypeError("WorkbenchFilesLayer requires server-injected inner API settings")

    @property
    def prefix_prompts(self) -> list[str]:
        return [
            "默认将生成文件保存到当前会话目录；用户明确指定时可访问个人 /workspace 下的其他目录。交付文件前调用 workbench_files 查询实际文件，"
            "确认文件已在文件空间可见，告知用户可打开查看，并逐字使用工具返回的 download_url 提供下载链接。"
            "在对话中展示图片时使用 ![图片说明](preview_url)，其中 preview_url 必须逐字取自该文件查询结果。"
            "不要猜测或拼接地址，不要用本地路径、sandbox: 地址、临时上传链接替代文件空间链接。"
            "查询失败时如实说明，不能声称文件已可下载。path 默认 . 列出当前会话文件，"
            "也可传入文件或子目录路径，跨对话或个人资源请传入 /workspace/... 绝对路径。complete=false 时按具体路径查询未显示的文件。"
            "downloadable=false 表示该文件或目录无法下载，应拆分过大文件或说明交付受阻。"
        ]

    async def get_tools(self, *, http_client: httpx.AsyncClient) -> list[Tool[object]]:
        context = self.deps.execution_context.config
        if not (context.workbench_run_id and context.user_from == "account" and context.user_id and context.app_id):
            return []
        identity = {
            "tenant_id": context.tenant_id,
            "account_id": context.user_id,
            "app_id": context.app_id,
            "workbench_run_id": context.workbench_run_id,
        }

        async def workbench_files(_ctx: RunContext[object], path: str = ".") -> str:
            """Verify files in this chat's file space and get their actual fixed preview/download URLs."""
            if not path or len(path) > 1024:
                return '{"error":"文件路径无效"}'
            requested = self.canonical_path(path)
            for verified_path in list(self._verified):
                if not requested or verified_path == requested or verified_path.startswith(requested + "/"):
                    self._verified.pop(verified_path)
            try:
                response = await http_client.post(
                    self.inner_api_url.rstrip("/") + "/inner/api/agent/workbench/files",
                    headers={"X-Inner-Api-Key": self.inner_api_key},
                    json={**identity, "path": path},
                    timeout=90,
                )
                response.raise_for_status()
                # Parse once to reject a proxy's HTML response; preserve server URLs exactly.
                data = response.json()
                if isinstance(data.get("cwd"), str):
                    self.bind_directory(data["cwd"])
                self._lookup_failed = bool(data.get("error")) or any(
                    isinstance(entry, dict)
                    and entry.get("downloadable") is False
                    and (
                        not self.runtime_state.changed_paths
                        or self.covers_changed_path(
                            _relative_path(str(entry.get("path") or entry.get("name"))),
                            directory=entry.get("kind") == "directory",
                        )
                    )
                    for entry in data.get("entries", [])
                )
                for entry in data.get("entries", []):
                    if (
                        isinstance(entry, dict)
                        and entry.get("downloadable") is not False
                        and isinstance(entry.get("download_url"), str)
                        and isinstance(entry.get("preview_url"), str)
                    ):
                        self._verified[_relative_path(str(entry.get("path") or entry.get("name")))] = {
                            "download_url": entry["download_url"],
                            "preview_url": entry["preview_url"],
                            "kind": str(entry.get("kind") or "file"),
                        }
                return response.text
            except (httpx.HTTPError, ValueError):
                self._lookup_failed = not self.runtime_state.changed_paths or self.covers_changed_path(
                    self.canonical_path(path), directory=True
                )
                return '{"error":"文件空间查询失败，尚未确认文件可见和下载链接，请重试或说明阻塞"}'

        return [Tool(workbench_files)]


def _relative_path(path: str) -> str:
    if path == "/workspace":
        return ""
    path = path.removeprefix("/workspace/")
    return "" if path == "." else path.rstrip("/")


def _file_target(url: str) -> bool:
    if url.startswith(("sandbox:", "file:", "/workspace/", "conversations/")):
        return True
    try:
        path = urlsplit(url).path
    except ValueError:
        return True
    return "/files/" in path or bool(
        re.search(r"\.(?:docx?|xlsx?|pptx?|pdf|zip|png|jpe?g|gif|webp|svg|csv|txt)$", path, re.I)
    )
