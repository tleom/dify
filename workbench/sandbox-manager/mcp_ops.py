"""File-backed personal MCP configuration, executed as the owning sandbox user.

Listing never connects to a server or returns credentials. Configurations are the
source of truth; cached tool declarations are valid only for their config hash.
All file traversal follows the same no-symlink contract as personal skills.
"""

import json
import os
import re
import uuid
from urllib.parse import urlsplit

from file_ops import read_file, version
from resource_ops import atomic_write, directory

SETTINGS = ".mcp-settings.json"
PINS = ".mcp-pins.json"
MAX_CONFIG = 65536
MAX_MANIFEST = 1024 * 1024
MAX_TOOLS = 200


def server_name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?", value):
        raise ValueError("MCP 标识需为 1–64 位小写字母、数字或连字符")
    return value


def string_map(value, label):
    if not isinstance(value, dict) or len(value) > 100:
        raise ValueError(label + " 必须是字符串键值对象，最多 100 项")
    if any(not isinstance(k, str) or not isinstance(v, str) or "\x00" in k + v for k, v in value.items()):
        raise ValueError(label + " 必须是字符串键值对象")
    return value


def configuration(raw):
    if not raw or len(raw) > MAX_CONFIG:
        raise ValueError("mcp.json 必须是非空且不超过 64 KiB 的 UTF-8 JSON")
    value = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("MCP 配置必须是 JSON 对象")
    allowed = {"name", "description", "transport", "url", "headers", "command", "args", "env", "cwd", "timeout"}
    if set(value) - allowed:
        raise ValueError("MCP 配置包含不支持的字段")
    for key, maximum in (("name", 120), ("description", 2000)):
        if key in value and (not isinstance(value[key], str) or len(value[key]) > maximum):
            raise ValueError("MCP 名称或说明格式无效")
    transport = value.get("transport") or ("stdio" if value.get("command") else "streamable-http")
    if transport == "http":
        transport = "streamable-http"
    if transport not in {"stdio", "streamable-http", "sse"}:
        raise ValueError("transport 需为 stdio、streamable-http 或 sse")
    timeout = value.get("timeout", 60)
    if type(timeout) not in (int, float) or not 5 <= timeout <= 300:
        raise ValueError("MCP 超时需为 5–300 秒")
    value = {**value, "transport": transport, "timeout": timeout}
    if transport == "stdio":
        if any(key in value for key in ("url", "headers")):
            raise ValueError("stdio 配置使用 command、args 和 env")
        command, args = value.get("command"), value.get("args", [])
        if not isinstance(command, str) or not command.strip() or "\x00" in command:
            raise ValueError("缺少有效的 MCP 启动命令")
        if not isinstance(args, list) or len(args) > 100 or any(not isinstance(v, str) or "\x00" in v for v in args):
            raise ValueError("args 必须是字符串数组，最多 100 项")
        value["args"] = args
        value["env"] = string_map(value.get("env", {}), "env")
        cwd = value.get("cwd", "/workspace")
        if not isinstance(cwd, str) or not cwd.startswith("/workspace") or "\x00" in cwd:
            raise ValueError("工作目录需位于 /workspace 下")
        if os.path.commonpath(["/workspace", os.path.normpath(cwd)]) != "/workspace":
            raise ValueError("工作目录需位于 /workspace 下")
        value["cwd"] = cwd
    else:
        if any(key in value for key in ("command", "args", "env", "cwd")):
            raise ValueError("远程 MCP 配置使用 url 和 headers")
        url = value.get("url")
        if not isinstance(url, str) or len(url) > 4096 or any(c in url for c in "\r\n\x00"):
            raise ValueError("缺少有效的 MCP URL")
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            raise ValueError("MCP URL 需为 HTTP(S) 地址；认证信息请填写 headers")
        value["headers"] = string_map(value.get("headers", {}), "headers")
        if any("\r" in k + v or "\n" in k + v for k, v in value["headers"].items()):
            raise ValueError("MCP headers 不能包含换行")
    return value


def tool_manifest(tools):
    if not isinstance(tools, list) or len(tools) > MAX_TOOLS:
        raise ValueError("单个 MCP 最多提供 200 个工具")
    result, seen = [], set()
    for tool in tools:
        if not isinstance(tool, dict):
            raise ValueError("MCP 工具声明无效")
        name, schema = tool.get("name"), tool.get("inputSchema")
        if not isinstance(name, str) or not name or len(name) > 128 or name in seen:
            raise ValueError("MCP 工具名称无效或重复")
        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise ValueError("MCP 工具参数必须是 object JSON Schema")
        pending = [schema]
        while pending:
            part = pending.pop()
            if isinstance(part, dict):
                for key, value in part.items():
                    if key in {"$ref", "$dynamicRef"} and (not isinstance(value, str) or not value.startswith("#")):
                        raise ValueError("MCP 参数声明不能引用外部 JSON Schema")
                    pending.append(value)
            elif isinstance(part, list):
                pending.extend(part)
        description = tool.get("description") or name
        if not isinstance(description, str) or len(description) > 16000:
            raise ValueError("MCP 工具说明过长或无效")
        result.append({"name": name, "description": description, "inputSchema": schema})
        seen.add(name)
    if len(json.dumps(result).encode()) > MAX_MANIFEST:
        raise ValueError("MCP 工具目录超过 1 MiB")
    return result


def manifest_version(tools):
    return version(json.dumps(tools, sort_keys=True, separators=(",", ":")).encode())


def mapping(rootfd, name):
    value = json.loads(read_file(rootfd, name) or b"{}")
    if not isinstance(value, dict) or any(not isinstance(v, bool) for v in value.values()):
        raise ValueError("MCP 开关或置顶设置格式无效")
    return value


def raw_configuration(rootfd, name):
    mcpfd = directory(rootfd, "mcp")
    try:
        fd = os.open(server_name(name), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=mcpfd)
    finally:
        os.close(mcpfd)
    try:
        return read_file(fd, "mcp.json")
    finally:
        os.close(fd)


def load(rootfd, name):
    raw = raw_configuration(rootfd, name)
    return configuration(raw), version(raw)


def snapshot(rootfd):
    warnings = []
    try:
        settings = mapping(rootfd, SETTINGS)
        settings_valid = True
    except (ValueError, OSError):
        settings, settings_valid = {}, False
        warnings.append("MCP 开关文件无效，个人 MCP 暂时停用")
    try:
        pins = mapping(rootfd, PINS)
    except (ValueError, OSError):
        pins = {}
        warnings.append("MCP 置顶文件无效")
    mcpfd, cachefd = directory(rootfd, "mcp"), directory(rootfd, ".mcp-cache")
    items = []
    try:
        for name in sorted(os.listdir(mcpfd)):
            item = {
                "id": name,
                "name": name,
                "description": "",
                "scope": "personal",
                "readonly": False,
                "path": "/workspace/mcp/" + name,
                "enabled": settings_valid and settings.get(name, True),
                "pinned": pins.get(name, False),
                "version": None,
                "tools": [],
                "status": "unverified",
            }
            try:
                raw = raw_configuration(rootfd, name)
                revision = version(raw)
                item["version"] = revision
                config = configuration(raw)
                item.update(
                    name=config.get("name") or name,
                    description=config.get("description", ""),
                    transport=config["transport"],
                    version=revision,
                )
                cached = json.loads(read_file(cachefd, name + ".json") or b"{}")
                if isinstance(cached, dict) and cached.get("version") == revision:
                    tools = tool_manifest(cached.get("tools", []))
                    item.update(tools=tools, manifest_version=manifest_version(tools), status="ready")
            except (ValueError, OSError, RecursionError):
                item.update(enabled=False, status="invalid", error="配置或工具目录无效，请编辑 mcp.json 后重新测试连接")
            items.append(item)
        return {"mcp": items, "warnings": warnings}
    finally:
        os.close(mcpfd)
        os.close(cachefd)


def personal_mcp(payload, root="/workspace"):
    operation = payload["operation"]
    rootfd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if operation == "mcp_list":
            return snapshot(rootfd)
        name = server_name(payload.get("name"))
        if operation == "mcp_save":
            content = payload.get("content")
            if not isinstance(content, str):
                raise ValueError("缺少 MCP 配置")
            configuration(content.encode("utf-8"))
        if operation == "mcp_probe":
            config, revision = load(rootfd, name)
            return {
                "id": name,
                "version": revision,
                "timeout": config["timeout"],
                "enabled": mapping(rootfd, SETTINGS).get(name, True),
            }
        if operation in {"mcp_toggle", "mcp_pin"}:
            load(rootfd, name)
            field, filename = ("enabled", SETTINGS) if operation == "mcp_toggle" else ("pinned", PINS)
            if not isinstance(payload.get(field), bool):
                raise ValueError("MCP 设置需为布尔值")
            values = mapping(rootfd, filename)
            values[name] = payload[field]
            atomic_write(rootfd, filename, json.dumps(values).encode())
            return {"id": name, field: values[name]}
        mcpfd = directory(rootfd, "mcp")
        try:
            fd = (
                directory(mcpfd, name)
                if operation == "mcp_save"
                else os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=mcpfd)
            )
            try:
                raw = read_file(fd, "mcp.json")
                revision = version(raw)
                if operation == "mcp_read":
                    return {"id": name, "content": (raw or b"").decode("utf-8-sig"), "version": revision}
                if payload.get("version") != revision:
                    return {"conflict": True}
                if operation == "mcp_save":
                    content = payload.get("content")
                    if not isinstance(content, str):
                        raise ValueError("缺少 MCP 配置")
                    data = content.encode("utf-8")
                    configuration(data)
                    if raw is not None:
                        backups = directory(rootfd, ".mcp-backups")
                        try:
                            atomic_write(backups, name + "-" + uuid.uuid4().hex + ".json", raw)
                        finally:
                            os.close(backups)
                    atomic_write(fd, "mcp.json", data)
                    return {"id": name, "version": version(data)}
                if operation == "mcp_delete":
                    backups = directory(rootfd, ".mcp-backups")
                    try:
                        os.rename(name, name + "-" + uuid.uuid4().hex, src_dir_fd=mcpfd, dst_dir_fd=backups)
                    finally:
                        os.close(backups)
                    cachefd = directory(rootfd, ".mcp-cache")
                    try:
                        try:
                            os.unlink(name + ".json", dir_fd=cachefd)
                        except FileNotFoundError:
                            pass
                    finally:
                        os.close(cachefd)
                    for setting in (SETTINGS, PINS):
                        try:
                            values = mapping(rootfd, setting)
                        except (ValueError, OSError):
                            continue
                        values.pop(name, None)
                        atomic_write(rootfd, setting, json.dumps(values).encode())
                    return {"id": name, "removed": True}
                if operation == "mcp_cache":
                    tools = tool_manifest(payload.get("tools"))
                    cachefd = directory(rootfd, ".mcp-cache")
                    try:
                        atomic_write(
                            cachefd, name + ".json", json.dumps({"version": revision, "tools": tools}).encode()
                        )
                    finally:
                        os.close(cachefd)
                    return {"id": name, "version": revision, "tools": tools}
                raise ValueError("不支持的 MCP 配置操作")
            finally:
                os.close(fd)
        finally:
            os.close(mcpfd)
    finally:
        os.close(rootfd)


if __name__ == "__main__":
    import sys

    try:
        print(json.dumps(personal_mcp(json.load(sys.stdin))))
    except (ValueError, OSError):
        print(json.dumps({"error": "MCP 配置操作失败，请检查格式、路径和版本后重试"}))
        sys.exit(1)
