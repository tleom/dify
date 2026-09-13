"""Workbench-owned records. Public Agent snapshots are never edited by chat users."""

from sqlalchemy import Boolean, Index, Integer, String, Text, UniqueConstraint, false
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, DefaultFieldsMixin
from .types import StringUUID


class WorkbenchChat(DefaultFieldsMixin, Base):
    __tablename__ = "workbench_chats"
    __table_args__ = (Index("wb_chat_owner", "tenant_id", "account_id"),)
    tenant_id: Mapped[str] = mapped_column(StringUUID)
    account_id: Mapped[str] = mapped_column(StringUUID)
    agent_id: Mapped[str] = mapped_column(StringUUID)
    app_id: Mapped[str] = mapped_column(StringUUID)
    base_snapshot_id: Mapped[str] = mapped_column(StringUUID)
    conversation_id: Mapped[str | None] = mapped_column(StringUUID, nullable=True)
    title: Mapped[str] = mapped_column(String(255), default="新会话")
    pinned: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    version: Mapped[int] = mapped_column(Integer, default=1)
    deleted: Mapped[int] = mapped_column(Integer, default=0)


class WorkbenchRevision(DefaultFieldsMixin, Base):
    __tablename__ = "workbench_revisions"
    __table_args__ = (UniqueConstraint("chat_id", "version", name="wb_revision_version"),)
    tenant_id: Mapped[str] = mapped_column(StringUUID)
    account_id: Mapped[str] = mapped_column(StringUUID)
    chat_id: Mapped[str] = mapped_column(StringUUID)
    version: Mapped[int] = mapped_column(Integer)
    template_snapshot_id: Mapped[str | None] = mapped_column(StringUUID, nullable=True)
    selection: Mapped[str] = mapped_column(Text)
    effective_soul: Mapped[str] = mapped_column(Text)


class WorkbenchRun(DefaultFieldsMixin, Base):
    __tablename__ = "workbench_runs"
    __table_args__ = (
        UniqueConstraint("chat_id", "request_key", name="wb_run_idempotency"),
        Index("wb_run_state", "status"),
    )
    tenant_id: Mapped[str] = mapped_column(StringUUID)
    account_id: Mapped[str] = mapped_column(StringUUID)
    chat_id: Mapped[str] = mapped_column(StringUUID)
    revision_id: Mapped[str] = mapped_column(StringUUID)
    request_key: Mapped[str] = mapped_column(String(128))
    payload: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="queued")
    task_id: Mapped[str | None] = mapped_column(StringUUID, nullable=True)
    backend_run_id: Mapped[str | None] = mapped_column(StringUUID, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    event_log: Mapped[str] = mapped_column(Text, default="[]")
