"""Verify a short-lived GXZS assertion bound to the exact workbench request.

The gateway derives these identities from its live login session. No email matching,
caller-supplied workspace, or console bearer token is accepted by this contract.
Replay consumption is owned by gxzs_identity after cryptographic validation.
"""

import base64
import hashlib
import hmac
import json
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from werkzeug.exceptions import ServiceUnavailable, Unauthorized


class GxzsAssertion(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    iss: str = Field(min_length=1, max_length=128)
    aud: Literal["dify-workbench"]
    sub: str = Field(min_length=3, max_length=150)
    tenant_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    user_id: str = Field(pattern=r"^[0-9]{1,20}$")
    name: str = Field(min_length=1, max_length=255)
    iat: int
    exp: int
    jti: str = Field(pattern=r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$")
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
    target: str = Field(min_length=1, max_length=9000)
    body_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    content_type: str = Field(max_length=200)
    last_event_id: str = Field(max_length=41)


def _decode(value: str) -> bytes:
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)


def verify_assertion(
    token: str,
    *,
    key: str,
    issuer: str,
    method: str,
    target: str,
    body: bytes,
    content_type: str,
    last_event_id: str,
    now: int | None = None,
) -> GxzsAssertion:
    """Validate HS256, identity, lifetime and request binding without side effects."""
    try:
        secret = base64.b64decode(key, validate=True)
        if len(secret) < 32:
            raise ValueError("short key")
    except (ValueError, TypeError) as error:
        raise ServiceUnavailable("工作台身份服务尚未配置") from error
    try:
        if len(token) > 16384:
            raise ValueError("oversized assertion")
        header, payload, signature = token.split(".")
        if json.loads(_decode(header)) != {"alg": "HS256", "typ": "JWT"}:
            raise ValueError("unsupported header")
        expected = hmac.digest(secret, f"{header}.{payload}".encode("ascii"), "sha256")
        if not hmac.compare_digest(expected, _decode(signature)):
            raise ValueError("invalid signature")
        claims = GxzsAssertion.model_validate_json(_decode(payload))
        timestamp = int(time.time()) if now is None else now
        if (
            claims.iss != issuer
            or claims.sub != f"{claims.tenant_id}:{claims.user_id}"
            or not 0 < claims.exp - claims.iat <= 60
            or claims.iat > timestamp + 5
            or claims.exp <= timestamp
            or claims.method != method
            or claims.target != target
            or claims.content_type != content_type
            or claims.last_event_id != last_event_id
            or not hmac.compare_digest(claims.body_sha256, hashlib.sha256(body).hexdigest())
        ):
            raise ValueError("assertion does not match request")
        return claims
    except (ValueError, TypeError, UnicodeError, ValidationError) as error:
        raise Unauthorized("工作台登录凭证无效或已过期") from error


def external_id(claims: GxzsAssertion) -> str:
    """Stable opaque identity; joining names with JSON avoids delimiter collisions."""
    value = json.dumps([claims.iss, claims.tenant_id, claims.user_id], separators=(",", ":"))
    return hashlib.sha256(value.encode()).hexdigest()
