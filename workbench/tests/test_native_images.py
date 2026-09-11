"""Image bytes reach Dify's native file pipeline after account-scoped version validation."""
import base64
import io
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PIL import Image
from werkzeug.exceptions import BadRequest, Conflict

from services.workbench.files import generation_query, validate_attachments


@pytest.mark.parametrize("query", ["这是啥", "裁剪这张图", "", "  "])
def test_visual_input_does_not_inject_a_file_task(query):
    payload = {"query": query, "sandbox_paths": ["/workspace/shared/photo.PNG"], "image_files": [{"type": "image"}]}
    assert generation_query(payload) == (query if query.strip() else "请描述图片。")


def test_mixed_attachments_keep_document_locator_only():
    payload = {"query": "核对图片和文档", "sandbox_paths": ["/workspace/shared/photo.png", "/workspace/shared/资料.docx"],
               "image_files": [{"type": "image"}]}
    query = generation_query(payload)
    assert "photo.png" not in query
    assert "/workspace/shared/资料.docx" in query


def test_legacy_file_only_run_still_has_a_locator():
    assert "/workspace/shared/photo.png" in generation_query({"query": "", "sandbox_paths": ["/workspace/shared/photo.png"]})


def test_continuation_does_not_repeat_file_instructions():
    assert generation_query({"query": "继续", "sandbox_paths": ["/workspace/shared/资料.docx"], "continuation": {"result": "ok"}}) == "继续"


def test_native_image_and_normal_file(mocker):
    image = io.BytesIO()
    Image.new("RGB", (8, 8), "blue").save(image, format="PNG")
    content = image.getvalue()
    operate = mocker.patch("services.workbench.files.operate", side_effect=[
        {"version": "v1", "data": base64.b64encode(content).decode()},
        {"version": "v2", "data": base64.b64encode(b"notes").decode()},
    ])
    session = MagicMock()
    user = SimpleNamespace(id="account")
    session.get.return_value = user
    mocker.patch("services.workbench.files.session_factory.create_session").return_value.__enter__.return_value = session
    mocker.patch("services.workbench.files.session_factory.get_session_maker")
    upload = mocker.patch("services.file_service.FileService").return_value.upload_file
    upload.return_value = SimpleNamespace(id="native-image")
    paths, images = validate_attachments("tenant", "account", [
        {"path": "shared/photo.png", "version": "v1"}, {"path": "shared/notes.txt", "version": "v2"},
    ])
    assert paths == ["/workspace/shared/photo.png", "/workspace/shared/notes.txt"]
    assert images == [{"type": "image", "transfer_method": "local_file", "upload_file_id": "native-image"}]
    upload.assert_called_once_with(filename="photo.png", content=content, mimetype="image/png", user=user, tenant_id="tenant")
    assert all(call.args[:2] == ("tenant", "account") for call in operate.call_args_list)


@pytest.mark.parametrize(("version", "expected"), [("changed", Conflict), ("v1", BadRequest)])
def test_changed_or_invalid_image_is_rejected(mocker, version, expected):
    mocker.patch("services.workbench.files.operate", return_value={"version": version, "data": base64.b64encode(b"invalid").decode()})
    upload = mocker.patch("services.file_service.FileService.upload_file")
    with pytest.raises(expected):
        validate_attachments("tenant", "account", [{"path": "shared/photo.png", "version": "v1"}])
    upload.assert_not_called()
