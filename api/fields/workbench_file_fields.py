"""File-space metadata shared with the current-chat Agent query."""

from typing import Literal

from fields.base import ResponseModel


class WorkbenchFileResponse(ResponseModel):
    downloadable: bool = True
    download_url: str | None = None
    preview_url: str | None = None
    name: str
    path: str
    kind: Literal["file", "directory", "blocked"]
    size: int
    modified: float
    version: str | None


class WorkbenchFileLinksResponse(ResponseModel):
    data: WorkbenchFileResponse


class AgentWorkbenchFilesResponse(ResponseModel):
    cwd: str | None = None
    directory: str
    entries: list[WorkbenchFileResponse]
    complete: bool


class WorkbenchFilePreviewResponse(ResponseModel):
    accepted: bool
    file: WorkbenchFileResponse
