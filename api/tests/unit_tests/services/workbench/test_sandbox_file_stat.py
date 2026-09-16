"""Exercise the manager's real descriptor-based filesystem operations on Linux CI."""

import hashlib
import os
import runpy
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Requires POSIX descriptor filesystem operations")

type FileOperator = Callable[[dict[str, str], str], dict[str, object]]


@pytest.fixture
def operate() -> FileOperator:
    source = Path(__file__).resolve().parents[5] / "workbench" / "sandbox-manager" / "file_ops.py"
    return cast(FileOperator, runpy.run_path(str(source))["operate"])


def test_stat_finds_exact_file_beyond_bounded_listing(operate: FileOperator, tmp_path: Path) -> None:
    folder = tmp_path / "conversations" / "chat"
    folder.mkdir(parents=True)
    for index in range(2000):
        (folder / f"a-{index:04}.txt").write_bytes(b"entry")
    content = b"\x89PNG\r\n\x1a\n"
    (folder / "z-last.png").write_bytes(content)
    root = str(tmp_path)
    listing = operate({"operation": "list", "path": "conversations/chat"}, root)
    entries = cast(list[dict[str, object]], listing["entries"])
    assert len(entries) == 2000
    assert all(entry["name"] != "z-last.png" for entry in entries)
    item = operate({"operation": "stat", "path": "conversations/chat/z-last.png"}, root)
    assert item["kind"] == "file"
    assert item["size"] == len(content)
    assert item["version"] == hashlib.sha256(content).hexdigest()
    assert item["downloadable"] is True
    assert "data" not in item
    first = operate({"operation": "stat", "path": str(entries[0]["path"])}, root)
    assert entries[0]["version"] is None
    assert first == {**entries[0], "version": hashlib.sha256(b"entry").hexdigest()}


def test_stat_preserves_directory_version_and_blocks_links(operate: FileOperator, tmp_path: Path) -> None:
    folder = tmp_path / "conversations" / "chat"
    folder.mkdir(parents=True)
    nested = folder / "nested"
    nested.mkdir()
    (nested / "report.txt").write_text("report", encoding="utf-8")
    (folder / "link").symlink_to(nested, target_is_directory=True)
    os.mkfifo(folder / "fifo")
    root = str(tmp_path)
    entries = cast(
        list[dict[str, object]], operate({"operation": "list", "path": "conversations/chat"}, root)["entries"]
    )
    for entry in entries:
        item = operate({"operation": "stat", "path": str(entry["path"])}, root)
        assert entry["version"] is None
        assert {**item, "version": None} == entry
        if entry["kind"] == "directory":
            assert isinstance(item["version"], str)
    assert {entry["kind"] for entry in entries if entry["name"] in {"link", "fifo"}} == {"blocked"}
    with pytest.raises((OSError, ValueError)):
        operate({"operation": "stat", "path": "conversations/chat/link/report.txt"}, root)
    with pytest.raises(ValueError, match="Invalid file path"):
        operate({"operation": "stat", "path": "conversations/chat/../private.txt"}, root)
    assert operate({"operation": "stat", "path": "conversations/chat/missing.txt"}, root)["kind"] == "missing"


def test_stat_resolves_download_size_limits_after_shallow_listing(operate: FileOperator, tmp_path: Path) -> None:
    folder = tmp_path / "conversations" / "chat"
    folder.mkdir(parents=True)
    oversized = folder / "large.bin"
    with oversized.open("wb") as stream:
        stream.truncate(20 * 1024 * 1024 + 1)
    root = str(tmp_path)
    item = operate({"operation": "stat", "path": "conversations/chat/large.bin"}, root)
    assert item["kind"] == "file"
    assert item["downloadable"] is False
    with pytest.raises(ValueError, match="20 MiB"):
        operate({"operation": "get", "path": "conversations/chat/large.bin"}, root)
    assert operate({"operation": "stat", "path": "conversations/chat"}, root)["downloadable"] is False
    oversized.unlink()
    for index in range(3):
        with (folder / f"part-{index}.bin").open("wb") as stream:
            stream.truncate(18 * 1024 * 1024)
    directory = operate({"operation": "stat", "path": "conversations/chat"}, root)
    assert directory["downloadable"] is False
    with pytest.raises(ValueError, match="50 MiB"):
        operate({"operation": "get", "path": "conversations/chat"}, root)
    (folder / "part-2.bin").unlink()
    assert operate({"operation": "stat", "path": "conversations/chat"}, root)["downloadable"] is True
    (folder / "link").symlink_to(folder / "part-0.bin")
    assert operate({"operation": "stat", "path": "conversations/chat"}, root)["downloadable"] is False
    listing = cast(list[dict[str, object]], operate({"operation": "list", "path": "conversations"}, root)["entries"])
    # Listing advertises the folder; stat/get enforce its recursive limits on demand.
    assert listing[0]["version"] is None
    assert listing[0]["downloadable"] is True
    with pytest.raises(ValueError, match="unsupported link"):
        operate({"operation": "get", "path": "conversations/chat"}, root)
