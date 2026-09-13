"""Focused identity tests need only account tables, with no workflow/plugin runtime.

Run this directory with --confcutdir to verify the authentication boundary without
installing optional vector stores or starting external services.
"""

from collections.abc import Callable, Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from core.db import session_factory as factory_module
from models.account import Account, AccountIntegrate, Tenant, TenantAccountJoin
from models.base import TypeBase
from tests.unit_tests.config_override import apply_config_overrides


@pytest.fixture
def config_overrides(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    def apply(**values: object) -> None:
        apply_config_overrides(monkeypatch, **values)

    return apply


@pytest.fixture
def sqlite_session(monkeypatch: pytest.MonkeyPatch) -> Iterator[Session]:
    engine = create_engine("sqlite://")
    TypeBase.metadata.create_all(
        engine,
        tables=[
            TypeBase.metadata.tables[model.__tablename__]
            for model in (Account, AccountIntegrate, Tenant, TenantAccountJoin)
        ],
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(factory_module, "_session_maker", factory)
    try:
        with factory() as session:
            yield session
    finally:
        engine.dispose()
