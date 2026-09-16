"""Personal memory and skill packages, executed inside the owning sandbox.

No package script is executed during import. All archive members are validated
before writes; user files use no-follow directory descriptors and atomic swaps.
Global packages use a separate root-owned, content-addressed directory.
"""

import base64
import hashlib
import io
import json
import os
import re
import stat
import sys
import uuid
import zipfile

from file_ops import parent_fd, read_file, remove_contents, tree_version, version

MAX_PACKAGE = 20 * 1024 * 1024
MAX_SKILL_TEXT = 64 * 1024
MAX_FILES = 200
SETTINGS = ".skills-settings.json"
PINS = ".skills-pins.json"


def safe_name(name):
    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?", name):
        raise ValueError("技能名称需为 1–64 位小写字母、数字或连字符")
    return name


def members(payload, *, require_skill=True):
    """Normalize a SKILL.md package, accepting one optional enclosing folder."""
    entries = {}
    if payload.get("archive"):
        raw = base64.b64decode(payload["archive"], validate=True)
        if len(raw) > MAX_PACKAGE:
            raise ValueError("技能包超过 20 MiB")
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            infos = [info for info in archive.infolist() if not info.is_dir()]
            if len(infos) > MAX_FILES or sum(info.file_size for info in infos) > MAX_PACKAGE:
                raise ValueError("技能包最多包含 200 个文件，总大小不超过 20 MiB")
            for info in infos:
                mode = info.external_attr >> 16
                if (stat.S_IFMT(mode) and not stat.S_ISREG(mode)) or info.flag_bits & 1:
                    raise ValueError("技能包不能包含链接、特殊文件或加密文件")
                if info.filename in entries:
                    raise ValueError("技能包包含重复路径")
                entries[info.filename] = archive.read(info)
    else:
        for item in payload.get("files", []):
            if item["path"] in entries:
                raise ValueError("技能包包含重复路径")
            entries[item["path"]] = base64.b64decode(item["data"], validate=True)
    if not entries or len(entries) > MAX_FILES or sum(map(len, entries.values())) > MAX_PACKAGE:
        raise ValueError("技能包文件数量或大小超限")
    for path in entries:
        if any(part in ("", ".", "..") for part in path.split("/")) or any(char in path for char in "\\\x00:"):
            raise ValueError("技能包包含无效路径")
    if require_skill and "SKILL.md" not in entries:
        first = next(iter(entries)).split("/")[0]
        if not all(path.startswith(first + "/") for path in entries) or first + "/SKILL.md" not in entries:
            raise ValueError("技能包需要包含一个 SKILL.md")
        entries = {path[len(first) + 1:]: data for path, data in entries.items()}
    folded = [path.casefold() for path in entries]
    if len(set(folded)) != len(folded):
        raise ValueError("技能包包含大小写冲突的文件名")
    if require_skill:
        if len(entries["SKILL.md"]) > MAX_SKILL_TEXT:
            raise ValueError("SKILL.md 不能超过 64 KiB")
        entries["SKILL.md"].decode("utf-8-sig")
    return entries


def directory(fd, name, mode=0o700):
    try:
        os.mkdir(name, mode, dir_fd=fd)
    except FileExistsError:
        pass
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)


def atomic_write(fd, name, data):
    temporary = ".resource-" + uuid.uuid4().hex
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


def write_package(fd, files, *, readonly=False):
    for path, data in files.items():
        current = os.dup(fd)
        try:
            for part in path.split("/")[:-1]:
                child = directory(current, part)
                os.close(current)
                current = child
            name = path.split("/")[-1]
            atomic_write(current, name, data)
            os.chmod(name, 0o444 if readonly else 0o600, dir_fd=current, follow_symlinks=False)
        finally:
            os.close(current)
    if readonly:
        def protect(current):
            for name in os.listdir(current):
                info = os.stat(name, dir_fd=current, follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
                    try:
                        protect(child)
                    finally:
                        os.close(child)
            os.fchmod(current, 0o555)
        protect(fd)


def personal(payload, root="/workspace"):
    operation = payload["operation"]
    rootfd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        warnings = []
        memory = None
        if operation in {"list", "memory_update"}:
            try:
                memory = read_file(rootfd, "memory.md")
            except (ValueError, OSError) as error:
                if operation != "list":
                    raise
                warnings.append("memory.md 无法读取：" + str(error))
        if memory is not None and len(memory) > MAX_SKILL_TEXT:
            warnings.append("memory.md 超过 64 KiB，未载入模型")
        try:
            memory_text = (memory or b"").decode("utf-8-sig") if len(memory or b"") <= MAX_SKILL_TEXT else ""
        except UnicodeDecodeError:
            memory_text = ""
            warnings.append("memory.md 不是有效 UTF-8，未载入模型")
        if operation == "memory_update":
            text = payload["content"].encode("utf-8")
            if len(text) > MAX_SKILL_TEXT:
                raise ValueError("memory.md 不能超过 64 KiB")
            if payload.get("version") != version(memory):
                return {"conflict": True}
            atomic_write(rootfd, "memory.md", text)
            return {"content": payload["content"], "version": version(text)}
        try:
            raw_settings = read_file(rootfd, SETTINGS)
            settings = json.loads(raw_settings or b"{}")
        except (ValueError, OSError) as error:
            if operation != "list":
                raise
            warnings.append("技能开关文件无效，个人技能暂时停用：" + str(error))
            settings = {}
            invalid_settings = True
        else:
            invalid_settings = not isinstance(settings, dict)
        if not isinstance(settings, dict):
            if operation != "list":
                raise ValueError("个人技能开关文件格式无效")
            settings = {}
            warnings.append("技能开关文件无效，个人技能暂时停用")
        try:
            pins = json.loads(read_file(rootfd, PINS) or b"{}")
            if not isinstance(pins, dict):
                raise ValueError("置顶设置格式无效")
        except (ValueError, OSError) as error:
            if operation == "skill_pin":
                raise
            pins = {}
            warnings.append("技能置顶设置无法读取：" + str(error))
        try:
            skillsfd = directory(rootfd, "skills")
        except OSError as error:
            if operation != "list":
                raise
            return {"memory": {"content": memory_text, "version": version(memory)}, "skills": [], "warnings": [*warnings, "skills 目录无法读取：" + str(error)]}
        try:
            if operation == "list":
                skills = []
                for name in sorted(os.listdir(skillsfd)):
                    info = os.stat(name, dir_fd=skillsfd, follow_symlinks=False)
                    if not stat.S_ISDIR(info.st_mode):
                        continue
                    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=skillsfd)
                    try:
                        source = read_file(fd, "SKILL.md")
                        if source is None or len(source) > MAX_SKILL_TEXT:
                            continue
                        skills.append({
                            "id": name, "content": source.decode("utf-8-sig"),
                            "enabled": not invalid_settings and settings.get(name, True) is not False,
                            "pinned": pins.get(name) is True,
                            "path": "/workspace/skills/" + name, "version": tree_version(fd),
                        })
                    except (ValueError, OSError) as error:
                        warnings.append("技能 " + name + " 未载入：" + str(error))
                    finally:
                        os.close(fd)
                return {"memory": {"content": memory_text, "version": version(memory)}, "skills": skills, "warnings": warnings}
            name = safe_name(payload["name"])
            if operation == "skill_pin":
                fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=skillsfd)
                os.close(fd)
                if not isinstance(payload.get("pinned"), bool):
                    raise ValueError("技能置顶值无效")
                pins[name] = payload["pinned"]
                atomic_write(rootfd, PINS, json.dumps(pins).encode())
                return {"id": name, "pinned": pins[name]}
            if operation in {"skill_update", "skill_uninstall"}:
                fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=skillsfd)
                try:
                    if not payload.get("version") or payload["version"] != tree_version(fd):
                        return {"conflict": True}
                    text = None
                    if operation == "skill_update":
                        if not isinstance(payload.get("content"), str):
                            raise ValueError("缺少技能内容")
                        text = payload["content"].encode("utf-8")
                        if len(text) > MAX_SKILL_TEXT:
                            raise ValueError("SKILL.md 不能超过 64 KiB")
                    backups = directory(rootfd, ".skill-backups")
                    backup_name = name + "-" + uuid.uuid4().hex
                    try:
                        if operation == "skill_uninstall":
                            # Keep the complete package recoverable outside the active catalog.
                            os.rename(name, backup_name, src_dir_fd=skillsfd, dst_dir_fd=backups)
                            os.fsync(skillsfd)
                            os.fsync(backups)
                            return {"id": name, "uninstalled": True}
                        backup = directory(backups, backup_name)
                        try:
                            previous = read_file(fd, "SKILL.md")
                            if previous is not None:
                                atomic_write(backup, "SKILL.md", previous)
                        finally:
                            os.close(backup)
                        atomic_write(fd, "SKILL.md", text)
                        return {"id": name, "version": tree_version(fd)}
                    finally:
                        os.close(backups)
                finally:
                    os.close(fd)
            if operation == "skill_toggle":
                fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=skillsfd)
                os.close(fd)
                if not isinstance(payload["enabled"], bool):
                    raise ValueError("技能开关值无效")
                settings[name] = payload["enabled"]
                atomic_write(rootfd, SETTINGS, json.dumps(settings).encode())
                return {"id": name, "enabled": settings[name]}
            if operation != "skill_import":
                raise ValueError("未知个人资源操作")
            files = members(payload)
            try:
                oldfd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=skillsfd)
            except FileNotFoundError:
                oldfd = None
            previous = None
            if oldfd is not None:
                try:
                    previous = tree_version(oldfd)
                finally:
                    os.close(oldfd)
            if payload.get("version") != previous:
                return {"conflict": True}
            staging_name = ".skill-import-" + uuid.uuid4().hex
            staging = directory(rootfd, staging_name)
            try:
                write_package(staging, files)
                fingerprint = tree_version(staging)
            finally:
                os.close(staging)
            backups, backup_name = None, None
            if previous is not None:
                backups = directory(rootfd, ".skill-backups")
                backup_name = name + "-" + uuid.uuid4().hex
                os.rename(name, backup_name, src_dir_fd=skillsfd, dst_dir_fd=backups)
            try:
                os.rename(staging_name, name, src_dir_fd=rootfd, dst_dir_fd=skillsfd)
                os.fsync(skillsfd)
            except BaseException:
                if backups is not None:
                    os.rename(backup_name, name, src_dir_fd=backups, dst_dir_fd=skillsfd)
                raise
            finally:
                if backups is not None:
                    os.close(backups)
            return {"id": name, "version": fingerprint, "path": "/workspace/skills/" + name}
        finally:
            os.close(skillsfd)
    finally:
        os.close(rootfd)


def global_install(payload, root="/opt/workbench-global"):
    """Only the trusted manager invokes this as root, with server-loaded archives."""
    os.makedirs(root, mode=0o755, exist_ok=True)
    rootfd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    result = []
    try:
        for package in payload["packages"]:
            is_skill = package.get("kind", "skill") == "skill"
            files = members(package, require_skill=is_skill)
            digest = hashlib.sha256()
            for path, data in sorted(files.items()):
                digest.update(path.encode() + b"\0" + hashlib.sha256(data).digest())
            key = digest.hexdigest()
            try:
                fd = os.open(key, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=rootfd)
            except FileNotFoundError:
                temporary = ".global-" + uuid.uuid4().hex
                fd = directory(rootfd, temporary)
                try:
                    write_package(fd, files, readonly=True)
                finally:
                    os.close(fd)
                os.rename(temporary, key, src_dir_fd=rootfd, dst_dir_fd=rootfd)
            else:
                os.close(fd)
            result.append({"name": package["name"], "path": root + "/" + key + ("" if is_skill else "/" + next(iter(files))), "content": files["SKILL.md"].decode("utf-8-sig") if is_skill else ""})
        os.fsync(rootfd)
        return {"skills": result}
    finally:
        os.close(rootfd)


if __name__ == "__main__":
    try:
        request = json.load(sys.stdin)
        print(json.dumps(global_install(request) if request.get("operation") == "global_install" else personal(request)))
    except (ValueError, OSError, zipfile.BadZipFile) as error:
        print(json.dumps({"error": str(error)}))
        sys.exit(2)
