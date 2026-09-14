"""Page through authorized indexed content without treating top-k search as a census."""

import base64
import hashlib
import json

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session
from werkzeug.exceptions import BadRequest, Conflict, Forbidden, NotFound

from configs import dify_config
from core.rag.retrieval.dataset_retrieval import DatasetRetrieval
from core.workflow.nodes.knowledge_retrieval.entities import MetadataFilteringCondition
from graphon.model_runtime.entities.llm_entities import LLMMode
from graphon.nodes.llm.entities import ModelConfig
from models.agent_config_entities import AgentKnowledgeMetadataFilteringConfig
from models.dataset import Dataset, Document, DocumentSegment
from models.workbench import WorkbenchChat, WorkbenchRun
from services.entities.knowledge_documents import (
    KnowledgeDocumentInfo,
    KnowledgeDocumentsPayload,
    KnowledgeDocumentsResponse,
    KnowledgeSegmentSlice,
)
from services.workbench.authorization import can_read_dataset
from services.workbench.knowledge import available_sets

_PAGE_ITEMS = 20
_PAGE_CONTENT_CHARS = 12000


class _Cursor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset_id: str
    document_id: str | None
    position: int = Field(ge=0)
    id: str
    offset: int = Field(default=0, ge=0)
    fingerprint: str | None = None

    def encode(self) -> str:
        return base64.urlsafe_b64encode(self.model_dump_json().encode()).decode()


def _decode_cursor(request: KnowledgeDocumentsPayload) -> _Cursor | None:
    if request.cursor is None:
        return None
    try:
        value = _Cursor.model_validate_json(base64.b64decode(request.cursor, altchars=b"-_", validate=True))
    except (ValueError, ValidationError) as exc:
        raise BadRequest("文档分页游标无效，请重新读取") from exc
    if value.dataset_id != request.dataset_id or value.document_id != request.document_id:
        raise BadRequest("分页游标不属于当前知识库或文档")
    return value


def _authorize(
    session: Session, request: KnowledgeDocumentsPayload
) -> tuple[Dataset, AgentKnowledgeMetadataFilteringConfig]:
    caller = request.caller
    if not dify_config.WORKBENCH_ENABLED or caller.user_from != "account":
        raise Forbidden()
    run = session.scalar(
        select(WorkbenchRun)
        .join(WorkbenchChat, WorkbenchChat.id == WorkbenchRun.chat_id)
        .where(
            WorkbenchRun.id == request.workbench_run_id,
            WorkbenchRun.tenant_id == caller.tenant_id,
            WorkbenchRun.account_id == caller.user_id,
            WorkbenchRun.status == "running",
            WorkbenchChat.tenant_id == caller.tenant_id,
            WorkbenchChat.account_id == caller.user_id,
            WorkbenchChat.app_id == caller.app_id,
            WorkbenchChat.deleted == 0,
        )
    )
    if run is None:
        raise Forbidden("知识库读取任务已结束或不可访问")
    selected = json.loads(run.payload).get("effective_soul", {}).get("knowledge", {}).get("sets", [])
    knowledge_set = next(
        (item for item in selected if any(dataset["id"] == request.dataset_id for dataset in item.get("datasets", []))),
        None,
    )
    if knowledge_set is None:
        raise Forbidden("只能读取当前任务已选择的知识库")
    # Recheck current account visibility and retrieval permission on every page,
    # including after a user pause or a knowledge permission change.
    if request.dataset_id not in {item["id"] for item in available_sets(caller.tenant_id, caller.user_id)}:
        raise Forbidden("知识库访问权限已变更")
    if not can_read_dataset(caller.tenant_id, caller.user_id, request.dataset_id):
        raise Forbidden("没有知识库内容查看权限，无法列举或读取完整文档")
    dataset = session.scalar(
        select(Dataset).where(Dataset.id == request.dataset_id, Dataset.tenant_id == caller.tenant_id)
    )
    if dataset is None:
        raise NotFound("知识库已不存在")
    if dataset.provider == "external":
        raise Conflict("外部知识库没有提供文档枚举接口，无法据此确认全文或完整列表")
    filtering = AgentKnowledgeMetadataFilteringConfig.model_validate(knowledge_set.get("metadata_filtering") or {})
    return dataset, filtering


def _document_query(
    session: Session,
    request: KnowledgeDocumentsPayload,
    dataset: Dataset,
    filtering: AgentKnowledgeMetadataFilteringConfig,
):
    query = select(Document).where(
        Document.tenant_id == request.caller.tenant_id,
        Document.dataset_id == dataset.id,
        Document.enabled.is_(True),
        Document.archived.is_(False),
    )
    if filtering.mode == "automatic":
        raise Conflict("自动元数据过滤依赖具体查询，请先使用知识库检索")
    if filtering.mode == "manual":
        if filtering.conditions is None:
            raise Conflict("任务的知识库过滤条件不完整，请重新选择知识库")
        ids, _ = DatasetRetrieval().get_metadata_filter_condition(
            session=session,
            dataset_ids=[dataset.id],
            query="",
            tenant_id=request.caller.tenant_id,
            user_id=request.caller.user_id,
            metadata_filtering_mode="manual",
            metadata_model_config=ModelConfig(provider="", name="", mode=LLMMode.CHAT, completion_params={}),
            metadata_filtering_conditions=MetadataFilteringCondition.model_validate(filtering.conditions.model_dump()),
            inputs={},
        )
        query = query.where(Document.id.in_((ids or {}).get(dataset.id, [])))
    return query


def _after(position, identifier, cursor: _Cursor, *, include_current: bool = False):
    return or_(
        position > cursor.position,
        and_(position == cursor.position, identifier >= cursor.id if include_current else identifier > cursor.id),
    )


def _segment_text(segment: DocumentSegment) -> str:
    # Indexed Q&A answers are part of the document, not a disposable search preview.
    return segment.content + ("\nAnswer: " + segment.answer if segment.answer else "")


def _segment_cursor(request: KnowledgeDocumentsPayload, segment: DocumentSegment, offset: int = 0) -> str:
    return _Cursor(
        dataset_id=request.dataset_id,
        document_id=request.document_id,
        position=segment.position,
        id=segment.id,
        offset=offset,
        fingerprint=hashlib.sha256(_segment_text(segment).encode()).hexdigest(),
    ).encode()


def read_documents(session: Session, request: KnowledgeDocumentsPayload) -> KnowledgeDocumentsResponse:
    dataset, filtering = _authorize(session, request)
    cursor = _decode_cursor(request)
    documents = _document_query(session, request, dataset, filtering)
    if request.operation == "list":
        total = session.scalar(select(func.count()).select_from(documents.subquery())) or 0
        unavailable = (
            session.scalar(
                select(func.count()).select_from(documents.where(Document.indexing_status != "completed").subquery())
            )
            or 0
        )
        if cursor:
            documents = documents.where(_after(Document.position, Document.id, cursor))
        rows = list(session.scalars(documents.order_by(Document.position, Document.id).limit(_PAGE_ITEMS + 1)))
        page = rows[:_PAGE_ITEMS]
        next_cursor = None
        if len(rows) > _PAGE_ITEMS:
            last = page[-1]
            next_cursor = _Cursor(dataset_id=dataset.id, document_id=None, position=last.position, id=last.id).encode()
        return KnowledgeDocumentsResponse(
            operation="list",
            dataset_id=dataset.id,
            documents=[
                KnowledgeDocumentInfo(
                    id=row.id,
                    name=row.name,
                    indexing_status=row.indexing_status,
                    readable=row.indexing_status == "completed",
                )
                for row in page
            ],
            total=total,
            unavailable_count=unavailable,
            next_cursor=next_cursor,
            complete=next_cursor is None,
            scope="当前启用且未归档、符合知识库元数据过滤条件的文档；未完成索引的文档不能据此核对全文。",
        )

    document = session.scalar(documents.where(Document.id == request.document_id))
    if document is None:
        raise NotFound("文档不存在、未启用或不符合当前知识库过滤条件")
    if document.indexing_status != "completed":
        raise Conflict("文档尚未完成索引，无法读取完整索引内容")
    all_segments = select(DocumentSegment).where(
        DocumentSegment.tenant_id == request.caller.tenant_id,
        DocumentSegment.dataset_id == dataset.id,
        DocumentSegment.document_id == document.id,
    )
    segments = all_segments.where(DocumentSegment.enabled.is_(True), DocumentSegment.status == "completed")
    total = session.scalar(select(func.count()).select_from(segments.subquery())) or 0
    all_count = session.scalar(select(func.count()).select_from(all_segments.subquery())) or 0
    if cursor:
        current = session.scalar(segments.where(DocumentSegment.id == cursor.id))
        if (
            current is None
            or current.position != cursor.position
            or hashlib.sha256(_segment_text(current).encode()).hexdigest() != cursor.fingerprint
        ):
            raise Conflict("文档内容已改变，请从头读取以避免遗漏")
        segments = segments.where(_after(DocumentSegment.position, DocumentSegment.id, cursor, include_current=True))
    rows = list(session.scalars(segments.order_by(DocumentSegment.position, DocumentSegment.id).limit(_PAGE_ITEMS + 1)))
    slices: list[KnowledgeSegmentSlice] = []
    remaining = _PAGE_CONTENT_CHARS
    next_cursor = None
    for segment in rows:
        start = cursor.offset if cursor and segment.id == cursor.id else 0
        text = _segment_text(segment)
        if start > len(text):
            raise BadRequest("文档分页位置无效")
        if remaining == 0 or len(slices) == _PAGE_ITEMS:
            next_cursor = _segment_cursor(request, segment, start)
            break
        end = min(start + remaining, len(text))
        slices.append(
            KnowledgeSegmentSlice(
                id=segment.id,
                position=segment.position,
                content=text[start:end],
                offset=start,
                total_chars=len(text),
                complete=end == len(text),
            )
        )
        remaining -= end - start
        if end < len(text):
            next_cursor = _segment_cursor(request, segment, end)
            break
    return KnowledgeDocumentsResponse(
        operation="read",
        dataset_id=dataset.id,
        document_id=document.id,
        document_name=document.name,
        segments=slices,
        total=total,
        unavailable_count=all_count - total,
        next_cursor=next_cursor,
        complete=next_cursor is None,
        scope="按原顺序读取当前启用且索引完成的文档正文与问答内容；不含禁用或未完成索引的片段，也不代表原始附件已全部解析。",
    )
