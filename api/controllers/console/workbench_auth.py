"""Authentication for workbench resources only; never a console login endpoint."""

from collections.abc import Callable
from functools import wraps

from flask import Response, g, request
from werkzeug.exceptions import NotFound

from configs import dify_config
from libs.login import login_required
from services.workbench.gxzs_assertion import verify_assertion
from services.workbench.gxzs_identity import consume_assertion, resolve_account


def workbench_login_required[**P, R](view: Callable[P, R]) -> Callable[P, R | Response]:
    """Keep existing console login intact, or accept a signed gateway request."""
    ordinary_login = login_required(view)

    @wraps(view)
    def decorated(*args: P.args, **kwargs: P.kwargs) -> R | Response:
        assertion = request.headers.get("X-GXZS-Assertion")
        if assertion is None:
            return ordinary_login(*args, **kwargs)
        if not dify_config.GXZS_WORKBENCH_ENABLED or not dify_config.WORKBENCH_ENABLED:
            raise NotFound()
        # Gunicorn preserves the raw encoded query; full_path is the Flask test fallback.
        target = request.environ.get("RAW_URI") or request.full_path.removesuffix("?")
        claims = verify_assertion(
            assertion,
            key=dify_config.GXZS_WORKBENCH_SIGNING_KEY,
            issuer=dify_config.GXZS_WORKBENCH_ISSUER,
            method=request.method,
            target=target,
            body=request.get_data(cache=True),
            content_type=request.headers.get("Content-Type", ""),
            last_event_id=request.headers.get("Last-Event-ID", ""),
        )
        consume_assertion(claims)
        g._login_user = resolve_account(claims)
        return view(*args, **kwargs)

    return decorated
