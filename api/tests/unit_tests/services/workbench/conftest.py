"""Focused identity tests need only account tables, with no workflow/plugin runtime.

Run this directory with --confcutdir to verify the authentication boundary without
installing optional vector stores or starting external services.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from configs import dify_config
from core.db import session_factory as factory_module
from models.account import Account, AccountIntegrate, Tenant, TenantAccountJoin
from models.base import TypeBase


@pytest.fixture
def config_overrides(monkeypatch):
    def apply(**values):
        for key, value in values.items():
            monkeypatch.setattr(dify_config, key, value)

    return apply


@pytest.fixture
def sqlite_session(monkeypatch):
    engine = create_engine("sqlite://")
    TypeBase.metadata.create_all(
        engine, tables=[model.__table__ for model in (Account, AccountIntegrate, Tenant, TenantAccountJoin)]
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(factory_module, "_session_maker", factory)
    try:
        with factory() as session:
            yield session
    finally:
        engine.dispose()
