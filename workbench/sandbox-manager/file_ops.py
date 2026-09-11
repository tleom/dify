"""Executed as the sandbox user. All traversal uses directory descriptors, never symlinks."""
import base64
import hashlib
import json
import os
import stat
import sys
import uuid

MAX_BYTES = 20 * 1024 * 1024


def parent_fd(root, path):
    parts = path.split("/")
    if not path or any(part in ("", ".", "..") or "\\" in part or "\x00" in part for part in parts):
        raise ValueError("Invalid file path")
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd, parts[-1]
    except BaseException:
        os.close(fd)
        raise


def read_file(fd, name):
    try:
        item = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    except FileNotFoundError:
        return None
    with os.fdopen(item, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            raise ValueError("Only regular files up to 20 MiB are supported")
        data = stream.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ValueError("File exceeds 20 MiB")
        return data


def version(data):
    return hashlib.sha256(data).hexdigest() if data is not None else None


def operate(payload, root="/workspace"):
    operation = payload["operation"]
    path = payload.get("path", "shared")
    if path.split("/")[0] not in ("shared", "conversations"):
        raise ValueError("Path must be in shared or conversations")
    fd, name = parent_fd(root, path)
    try:
        if operation == "list":
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                result = []
                for entry in sorted(os.listdir(child))[:2000]:
                    info = os.stat(entry, dir_fd=child, follow_symlinks=False)
                    kind = "directory" if stat.S_ISDIR(info.st_mode) else "file" if stat.S_ISREG(info.st_mode) else "blocked"
                    data = read_file(child, entry) if kind == "file" and info.st_size <= MAX_BYTES else None
                    result.append({"name": entry, "path": path + "/" + entry, "kind": kind,
                                   "size": info.st_size, "modified": info.st_mtime, "version": version(data)})
                return {"path": path, "entries": result}
            finally:
                os.close(child)
        old = read_file(fd, name)
        if operation == "get":
            if old is None:
                raise FileNotFoundError(path)
            return {"data": base64.b64encode(old).decode(), "version": version(old), "name": name}
        if operation not in ("upload", "delete"):
            raise ValueError("Unknown operation")
        if "version" not in payload or payload["version"] != version(old):
            return {"conflict": True, "message": "文件已改变，请刷新后重试"}
        if operation == "delete":
            if old is not None:
                os.unlink(name, dir_fd=fd)
            return {"deleted": True}
        data = base64.b64decode(payload["data"], validate=True)
        if len(data) > MAX_BYTES:
            raise ValueError("Upload exceeds 20 MiB")
        temporary = ".upload-" + uuid.uuid4().hex
        try:
            target = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
            with os.fdopen(target, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.rename(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
            os.fsync(fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=fd)
            except FileNotFoundError:
                pass
        return {"path": path, "version": version(data)}
    finally:
        os.close(fd)


if __name__ == "__main__":
    try:
        print(json.dumps(operate(json.load(sys.stdin))))
    except (ValueError, OSError) as error:
        print(json.dumps({"error": str(error)}))
        sys.exit(2)
