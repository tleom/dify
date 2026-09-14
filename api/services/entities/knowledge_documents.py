"""Read-only pagination for the indexed documents of a workbench knowledge set."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from fields.base import ResponseModel
from services.entities.knowledge_retrieval_inner import InnerKnowledgeRetrieveCaller


class KnowledgeDocumentsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workbench_run_id: str = Field(min_length=1)
    caller: InnerKnowledgeRetrieveCaller
    dataset_id: str = Field(min_length=1)
    operation: Literal["list", "read"]
    document_id: str | None = Field(default=None, min_length=1)
    cursor: str | None = Field(default=None, min_length=1, max_length=4096)

    @model_validator(mode="after")
    def validate_document(self):
        if (self.operation == "read") != (self.document_id is not None):
            raise ValueError("document_id is required only for read")
        return self


class KnowledgeDocumentInfo(ResponseModel):
    id: str
    name: str
    indexing_status: str
    readable: bool


class KnowledgeSegmentSlice(ResponseModel):
    id: str
    position: int
    content: str
    offset: int
    total_chars: int
    complete: bool


class KnowledgeDocumentsResponse(ResponseModel):
    operation: Literal["list", "read"]
    dataset_id: str
    document_id: str | None = None
    document_name: str | None = None
    documents: list[KnowledgeDocumentInfo] = Field(default_factory=list)
    segments: list[KnowledgeSegmentSlice] = Field(default_factory=list)
    total: int
    unavailable_count: int = 0
    next_cursor: str | None = None
    complete: bool
    scope: str
