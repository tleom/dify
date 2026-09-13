"""GXZS identity binding, revocation, replay and signed-request boundaries."""

import base64
import hashlib
import hmac
import importlib.util
import json
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from flask import Flask, g, jsonify
from sqlalchemy import delete, func, select
from werkzeug.exceptions import Forbidden, Unauthorized

from models.account import Account, AccountIntegrate, AccountStatus, Tenant, TenantAccountJoin, TenantAccountRole
from services.workbench import gxzs_identity
from services.workbench.gxzs_assertion import GxzsAssertion, external_id, verify_assertion

KEY = base64.b64encode(bytes(range(32))).decode()
BODY = '{"query":"你好"}'.encode()
TARGET = "/console/api/workbench/chats?limit=20"


def claims(**changes):
    values = {
        "iss": "gxzs",
        "aud": "dify-workbench",
        "sub": "000000:42",
        "tenant_id": "000000",
        "user_id": "42",
        "name": "同名用户",
        "iat": 1000,
        "exp": 1060,
        "jti": str(uuid4()),
        "method": "POST",
        "target": TARGET,
        "body_sha256": hashlib.sha256(BODY).hexdigest(),
        "content_type": "application/json",
        "last_event_id": "",
    }
    return GxzsAssertion.model_validate(values | changes)


def token(value, header=None):
    def encode(part):
        return (
            base64.urlsafe_b64encode(json.dumps(part, ensure_ascii=False, separators=(",", ":")).encode())
            .rstrip(b"=")
            .decode()
        )

    signing_input = f"{encode(header or {'alg': 'HS256', 'typ': 'JWT'})}.{encode(value.model_dump())}"
    signature = (
        base64.urlsafe_b64encode(hmac.digest(base64.b64decode(KEY), signing_input.encode(), "sha256"))
        .rstrip(b"=")
        .decode()
    )
    return f"{signing_input}.{signature}"


def verify(value, **changes):
    args = {
        "key": KEY,
        "issuer": "gxzs",
        "method": "POST",
        "target": TARGET,
        "body": BODY,
        "content_type": "application/json",
        "last_event_id": "",
        "now": 1001,
    }
    return verify_assertion(value, **(args | changes))


def test_assertion_accepts_unicode_and_binds_complete_request():
    original = claims()
    assert verify(token(original)) == original


def auth_app(config_overrides, monkeypatch):
    # Load this controller helper without importing the console package's unrelated routes.
    source = Path(__file__).resolve().parents[4] / "controllers" / "console" / "workbench_auth.py"
    spec = importlib.util.spec_from_file_location("workbench_auth_boundary", source)
    assert spec
    assert spec.loader
    auth = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(auth)
    config_overrides(
        WORKBENCH_ENABLED=True, GXZS_WORKBENCH_ENABLED=True, GXZS_WORKBENCH_SIGNING_KEY=KEY, LOGIN_DISABLED=False
    )
    monkeypatch.setattr("services.workbench.gxzs_assertion.time.time", lambda: 1001)
    from libs.login import current_account_with_tenant, login_required

    app = Flask(__name__)
    app.login_manager = SimpleNamespace(unauthorized=lambda: (jsonify(message="console login required"), 401))

    @app.before_request
    def ordinary_user():
        g._login_user = None

    def identity_view():
        owner = current_account_with_tenant()
        return jsonify(user_id=owner.account.id, tenant_id=owner.tenant_id, role=owner.account.current_role)

    app.add_url_rule(
        "/console/api/workbench/chats", "workbench", auth.workbench_login_required(identity_view), methods=["POST"]
    )
    app.add_url_rule("/console/api/account/profile", "console", login_required(identity_view), methods=["POST"])
    return app


def test_signed_controller_uses_same_account_and_keeps_console_login_separate(
    workspace, sqlite_session, config_overrides, monkeypatch
):
    app = auth_app(config_overrides, monkeypatch)
    client = app.test_client()
    assertion = token(claims())
    response = client.post(
        TARGET, data=BODY, headers={"X-GXZS-Assertion": assertion, "Content-Type": "application/json"}
    )
    assert response.status_code == 200
    assert response.json == {"user_id": resolve_account_id(sqlite_session), "tenant_id": workspace, "role": "normal"}
    assert "Set-Cookie" not in response.headers
    assert (
        client.post(
            TARGET, data=BODY, headers={"X-GXZS-Assertion": assertion, "Content-Type": "application/json"}
        ).status_code
        == 401
    )
    assert client.post(TARGET, data=BODY, content_type="application/json").status_code == 401
    other = token(claims(target="/console/api/account/profile"))
    assert (
        client.post(
            "/console/api/account/profile",
            data=BODY,
            headers={"X-GXZS-Assertion": other, "Content-Type": "application/json"},
        ).status_code
        == 401
    )


def resolve_account_id(session):
    return session.scalar(select(AccountIntegrate.account_id).where(AccountIntegrate.provider == "gxzs"))


@pytest.mark.usefixtures("workspace")
def test_controller_rejects_changed_request_before_provisioning(sqlite_session, config_overrides, monkeypatch):
    client = auth_app(config_overrides, monkeypatch).test_client()
    response = client.post(
        TARGET, data=b"{}", headers={"X-GXZS-Assertion": token(claims()), "Content-Type": "application/json"}
    )
    assert response.status_code == 401
    assert resolve_account_id(sqlite_session) is None


@pytest.mark.usefixtures("workspace")
def test_controller_returns_not_found_until_explicitly_enabled(sqlite_session, config_overrides, monkeypatch):
    client = auth_app(config_overrides, monkeypatch).test_client()
    config_overrides(GXZS_WORKBENCH_ENABLED=False)
    response = client.post(
        TARGET, data=BODY, headers={"X-GXZS-Assertion": token(claims()), "Content-Type": "application/json"}
    )
    assert response.status_code == 404
    assert resolve_account_id(sqlite_session) is None


@pytest.mark.parametrize(
    "changes",
    [
        {"method": "DELETE"},
        {"target": "/console/api/account/profile"},
        {"body": b"{}"},
        {"issuer": "other"},
        {"content_type": "text/plain"},
        {"last_event_id": "9-0"},
        {"now": 1060},
        {"now": 990},
        {"key": base64.b64encode(bytes(range(1, 33))).decode()},
    ],
)
def test_rejects_changed_request_or_expired_signature(changes):
    with pytest.raises(Unauthorized):
        verify(token(claims()), **changes)


@pytest.mark.parametrize("changes", [{"sub": "000001:42"}, {"exp": 1120}, {"iat": 1060, "exp": 1060}])
def test_rejects_inconsistent_identity_or_lifetime(changes):
    with pytest.raises(Unauthorized):
        verify(token(claims(**changes)))


def test_rejects_unsigned_or_unsupported_algorithm():
    with pytest.raises(Unauthorized):
        verify(token(claims(), {"alg": "none", "typ": "JWT"}))
    with pytest.raises(Unauthorized):
        verify(token(claims()).rsplit(".", 1)[0] + ".")


class RedisMemory:
    def __init__(self):
        self.keys = set()

    def lock(self, *_args, **_kwargs):
        return nullcontext()

    def set(self, key, _value, **kwargs):
        assert kwargs == {"nx": True, "ex": 120}
        if key in self.keys:
            return None
        self.keys.add(key)
        return True


@pytest.fixture
def workspace(sqlite_session, config_overrides, monkeypatch):
    tenant = Tenant(name="公信测试空间")
    sqlite_session.add(tenant)
    sqlite_session.commit()
    config_overrides(
        GXZS_WORKBENCH_ENABLED=True,
        GXZS_WORKBENCH_TENANTS={"000000": tenant.id},
        WORKBENCH_AGENT_TEMPLATES={tenant.id: "agent-a"},
    )
    monkeypatch.setattr(gxzs_identity, "redis_client", RedisMemory())
    return tenant.id


@pytest.mark.usefixtures("workspace")
def test_nonce_is_consumed_exactly_once():
    value = claims()
    gxzs_identity.consume_assertion(value)
    with pytest.raises(Unauthorized):
        gxzs_identity.consume_assertion(value)


def test_same_gxzs_identity_has_one_passwordless_normal_dify_member(workspace, sqlite_session):
    first = gxzs_identity.resolve_account(claims())
    second = gxzs_identity.resolve_account(claims(name="修改后的名字"))
    assert first.id == second.id
    assert second.name == "修改后的名字"
    assert second.current_tenant_id == workspace
    assert second.password is None
    assert second.current_role == TenantAccountRole.NORMAL
    assert sqlite_session.scalar(select(func.count()).select_from(AccountIntegrate)) == 1
    assert sqlite_session.scalar(select(func.count()).select_from(TenantAccountJoin)) == 1


def test_same_name_different_user_or_tenant_never_merges(workspace, sqlite_session, config_overrides):
    other = Tenant(name="另一个租户")
    sqlite_session.add(other)
    sqlite_session.commit()
    config_overrides(GXZS_WORKBENCH_TENANTS={"000000": workspace, "000001": other.id})
    accounts = [
        gxzs_identity.resolve_account(value).id
        for value in [claims(), claims(user_id="43", sub="000000:43"), claims(tenant_id="000001", sub="000001:42")]
    ]
    assert len(set(accounts)) == 3
    assert external_id(claims()) == external_id(claims(name="别名"))


@pytest.mark.usefixtures("workspace")
def test_removed_membership_is_not_recreated(sqlite_session):
    account = gxzs_identity.resolve_account(claims())
    sqlite_session.execute(delete(TenantAccountJoin).where(TenantAccountJoin.account_id == account.id))
    sqlite_session.commit()
    with pytest.raises(Forbidden):
        gxzs_identity.resolve_account(claims())
    assert sqlite_session.scalar(select(func.count()).select_from(TenantAccountJoin)) == 0


@pytest.mark.usefixtures("workspace")
def test_disabled_dify_account_is_not_reactivated(sqlite_session):
    account = gxzs_identity.resolve_account(claims())
    stored = sqlite_session.get(Account, account.id)
    stored.status = AccountStatus.BANNED
    sqlite_session.commit()
    with pytest.raises(Forbidden):
        gxzs_identity.resolve_account(claims())


@pytest.mark.usefixtures("workspace")
def test_unmapped_tenant_is_rejected_before_provisioning(sqlite_session):
    with pytest.raises(Forbidden):
        gxzs_identity.resolve_account(claims(tenant_id="000002", sub="000002:42"))
    assert sqlite_session.scalar(select(func.count()).select_from(AccountIntegrate)) == 0


def test_workbench_grant_is_limited_to_mapped_workspace_and_published_template(
    workspace, sqlite_session, config_overrides
):
    account = gxzs_identity.resolve_account(claims())
    assert gxzs_identity.can_run_template(sqlite_session, workspace, account.id, "agent-a")
    assert not gxzs_identity.can_run_template(sqlite_session, workspace, account.id, "agent-b")
    assert not gxzs_identity.can_run_template(sqlite_session, "another-workspace", account.id, "agent-a")
    assert not gxzs_identity.can_run_template(sqlite_session, workspace, str(uuid4()), "agent-a")
    config_overrides(GXZS_WORKBENCH_ENABLED=False)
    assert not gxzs_identity.can_run_template(sqlite_session, workspace, account.id, "agent-a")
