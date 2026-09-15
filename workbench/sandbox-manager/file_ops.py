"""Executed as the sandbox user. All traversal uses directory descriptors, never symlinks."""
import base64
import hashlib
import io
import json
import os
import stat
import sys
import uuid
import zipfile

MAX_BYTES = 20 * 1024 * 1024
MAX_TREE_BYTES = 50 * 1024 * 1024
MAX_TREE_ITEMS = 10000


def parent_fd(root, path):
    parts = path.split("/")
    if not path or any(part in ("", ".", "..") or "\\" in part or "\x00" in part for part in parts):
        raise ValueError("Invalid file path")
    fd = os.dup(root) if isinstance(root, int) else os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
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


def tree_entries(fd, prefix="", result=None):
    """Fingerprint metadata without following links or reading every file on listing."""
    result = [] if result is None else result
    for name in sorted(os.listdir(fd)):
        if len(result) >= MAX_TREE_ITEMS:
            raise ValueError("Folder contains too many entries")
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        path = prefix + name
        result.append((path, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ino))
        if stat.S_ISDIR(info.st_mode):
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                tree_entries(child, path + "/", result)
            finally:
                os.close(child)
    return result


def tree_version(fd):
    return version(json.dumps(tree_entries(fd), separators=(",", ":")).encode())


def archive(fd):
    entries = tree_entries(fd)
    total = sum(size for _, mode, size, _, _ in entries if stat.S_ISREG(mode))
    if total > MAX_TREE_BYTES:
        raise ValueError("Folder download exceeds 50 MiB; download its files separately")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
        for path, mode, _, _, _ in entries:
            if stat.S_ISDIR(mode):
                zipped.writestr(path + "/", b"")
            elif stat.S_ISREG(mode):
                parent, name = parent_fd(fd, path)
                try:
                    zipped.writestr(path, read_file(parent, name))
                finally:
                    os.close(parent)
            else:
                raise ValueError("Folder contains an unsupported link or special file")
    return output.getvalue()


def remove_contents(fd):
    for name in os.listdir(fd):
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                remove_contents(child)
            finally:
                os.close(child)
            os.rmdir(name, dir_fd=fd)
        else:
            os.unlink(name, dir_fd=fd)


def operate(payload, root="/workspace"):
    operation = payload["operation"]
    path = payload.get("path", "conversations")
    if path.split("/")[0] != "conversations":
        raise ValueError("Path must be in conversations")
    fd, name = parent_fd(root, path)
    try:
        if operation == "mkdir":
            if len(path.split("/")) != 2 or not path.startswith("conversations/"):
                raise ValueError("Only a conversation folder can be created")
            uuid.UUID(name)
            try:
                os.mkdir(name, mode=0o700, dir_fd=fd)
            except FileExistsError:
                pass
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(child)
            return {"path": path}
        if operation == "list":
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                result = []
                for entry in sorted(os.listdir(child))[:2000]:
                    info = os.stat(entry, dir_fd=child, follow_symlinks=False)
                    kind = "directory" if stat.S_ISDIR(info.st_mode) else "file" if stat.S_ISREG(info.st_mode) else "blocked"
                    data = read_file(child, entry) if kind == "file" and info.st_size <= MAX_BYTES else None
                    fingerprint = version(data)
                    if kind == "directory":
                        nested = os.open(entry, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=child)
                        try:
                            fingerprint = tree_version(nested)
                        finally:
                            os.close(nested)
                    result.append({"name": entry, "path": path + "/" + entry, "kind": kind,
                                   "size": info.st_size, "modified": info.st_mtime, "version": fingerprint})
                return {"path": path, "entries": result}
            finally:
                os.close(child)
        try:
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            info = None
        if operation == "stat":
            if info is None:
                return {"path": path, "kind": "missing"}
            kind = "directory" if stat.S_ISDIR(info.st_mode) else "file" if stat.S_ISREG(info.st_mode) else "blocked"
            data = read_file(fd, name) if kind == "file" and info.st_size <= MAX_BYTES else None
            fingerprint = version(data)
            if kind == "directory":
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    fingerprint = tree_version(child)
                finally:
                    os.close(child)
            return {"name": name, "path": path, "kind": kind,
                    "size": info.st_size, "modified": info.st_mtime, "version": fingerprint}
        if info is not None and stat.S_ISDIR(info.st_mode):
            if len(path.split("/")) < 2:
                raise ValueError("The conversation root cannot be downloaded or deleted")
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                fingerprint = tree_version(child)
                if operation == "get":
                    data = archive(child)
                    if fingerprint != tree_version(child):
                        return {"conflict": True}
                    return {"data": base64.b64encode(data).decode(), "version": fingerprint,
                            "kind": "directory", "name": name + ".zip"}
                if operation != "delete":
                    raise ValueError("A folder cannot be overwritten by an uploaded file")
                if payload.get("version") != fingerprint:
                    return {"conflict": True}
                remove_contents(child)
            finally:
                os.close(child)
            os.rmdir(name, dir_fd=fd)
            return {"deleted": True}
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
