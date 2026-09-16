import base64
from contextlib import nullcontext
from unittest.mock import Mock

import pytest
from werkzeug.exceptions import BadRequest

from services.workbench import office_preview


def test_conversion_cache_tracks_file_content_and_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    cache: dict[str, bytes] = {}
    redis = Mock(
        get=cache.get,
        setex=lambda key, _ttl, data: cache.update({key: data}),
        lock=lambda *_args, **_kwargs: nullcontext(),
    )
    manager = Mock(return_value={"data": base64.b64encode(b"%PDF-test").decode()})
    monkeypatch.setattr(office_preview, "redis_client", redis)
    monkeypatch.setattr(office_preview, "manager", manager)
    monkeypatch.setattr(office_preview, "ensure_workspace", lambda *_: "workspace")
    data = base64.b64encode(b"test-document").decode()
    for _ in range(2):
        assert office_preview.preview("tenant", "a", name="中文.docx", data=data) == b"%PDF-test"
    assert manager.call_count == 1
    office_preview.preview("tenant", "b", name="中文.docx", data=data)
    office_preview.preview("tenant", "a", name="中文.docx", data=base64.b64encode(b"changed").decode())
    assert manager.call_count == 3


@pytest.mark.parametrize(("name", "data"), [("script.py", "dGVzdA=="), ("file.docx", "!bad"), ("file.docx", "")])
def test_invalid_document_never_reaches_sandbox(name: str, data: str, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = Mock()
    monkeypatch.setattr(office_preview, "manager", manager)
    with pytest.raises(BadRequest):
        office_preview.preview("tenant", "owner", name=name, data=data)
    manager.assert_not_called()
