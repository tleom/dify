"""Private container control plane; exposes no arbitrary Docker or shell operation."""
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import sqlite3
from pathlib import Path
import subprocess
import threading
import time
import uuid
from urllib.request import ProxyHandler, Request, build_opener

from mcp_journal import Journal

TOKEN = os.environ["WORKBENCH_SANDBOX_MANAGER_TOKEN"]
IMAGE = os.environ.get("WORKBENCH_SANDBOX_IMAGE", "langgenius/dify-agent-local-sandbox:1.17.1")
NETWORK = os.environ.get("WORKBENCH_SANDBOX_NETWORK", "dify-workbench-sandboxes")
PREFIX = os.environ.get("WORKBENCH_SANDBOX_PREFIX", "dify-wb-dev")
STATE = Path(os.environ.get("WORKBENCH_MANAGER_STATE", "/state"))
STATE.mkdir(parents=True, exist_ok=True)
LOCKS = {}
LOCKS_GUARD = threading.Lock()
MAX_BODY = 30 * 1024 * 1024
MCP_JOURNAL = Journal(STATE)
MCP_API_URL = os.environ.get("WORKBENCH_MCP_API_URL", "http://wb-api:5001").rstrip("/")
# Manager-owned scripts never inherit a user's interpreter, import path or
# site initialization. User tasks keep their personal-environment-first PATH.
MANAGER_PYTHON = ("/usr/local/bin/python", "-I", "-S")


def docker(*args, stdin=None, timeout=90, check=True):
    result = subprocess.run(["docker", *args], input=stdin, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        # Never echo command arguments (which may contain the sandbox auth token).
        raise RuntimeError(result.stderr[-2000:] or "Docker operation failed")
    return result


def lock(key):
    with LOCKS_GUARD:
        return LOCKS.setdefault(key, threading.RLock())


def identity(key):
    key = str(uuid.UUID(key))
    name = PREFIX + "-" + key
    token = hmac.new(TOKEN.encode(), key.encode(), hashlib.sha256).hexdigest()
    return name, token


def touch(key):
    (STATE / key).touch()


def operation_file(key, request_id):
    digest = hashlib.sha256((key + ":" + request_id).encode()).hexdigest()
    return STATE / ("op-" + digest), PREFIX + "-install-" + digest[:24]


def save_operation(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value))
    os.replace(temporary, path)


def ensure(key):
    name, token = identity(key)
    with lock(key):
        info = docker("inspect", name, check=False)
        if not info.returncode:
            existing = json.loads(info.stdout)[0]
            if existing["Config"]["Image"] != IMAGE:
                if existing["State"]["Running"]:
                    # Do not execute privileged helpers in a previous image.
                    # Deployment drains its runs before stopping/recreating it.
                    raise RuntimeError("个人沙箱需要更新，请等待当前任务结束后由管理员重建运行容器")
                # Keep the stopped container as a rollback reference and reuse all owner volumes.
                docker("rename", name, name + "-previous-" + existing["Id"][:12])
                info = docker("inspect", name, check=False)
        if info.returncode:
            for suffix in ("home", "files", "env"):
                docker("volume", "create", "--label", "workbench=" + PREFIX, name + "-" + suffix)
            docker("run", "--rm", "--user", "0", "--network", "none", "--entrypoint", MANAGER_PYTHON[0],
                   "-v", name + "-home:/home/dify", "-v", name + "-files:/workspace", "-v", name + "-env:/opt/user-env",
                   IMAGE, *MANAGER_PYTHON[1:], "-c", "import os; paths=['/home/dify','/workspace','/workspace/conversations','/workspace/" + key + "','/opt/user-env']; "
                   "[(os.makedirs(p,exist_ok=True),os.chown(p,1000,1000)) for p in paths]")
            # Existing personal venvs gain the immutable office fallback without replacing their packages.
            docker("run", "--rm", "--user", "1000", "--network", "none", "--entrypoint", MANAGER_PYTHON[0],
                   "-v", name + "-env:/opt/user-env", IMAGE, *MANAGER_PYTHON[1:], "-c",
                   "from pathlib import Path; site=Path('/opt/user-env/current/python/lib/python3.12/site-packages'); "
                   "base=Path('/opt/office/python/lib/python3.12/site-packages'); "
                   "(site/'workbench_office.pth').write_text(str(base)+'\\n') if site.exists() and base.exists() else None")
            docker("create", "--name", name, "--label", "workbench=" + PREFIX, "--network", NETWORK,
                   "--cpus", os.environ.get("WORKBENCH_SANDBOX_CPUS", "2"),
                   "--memory", os.environ.get("WORKBENCH_SANDBOX_MEMORY", "4g"), "--pids-limit", "512",
                   "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
                   "-v", name + "-home:/home/dify", "-v", name + "-files:/workspace",
                   "-v", name + "-env:/opt/user-env:ro",
                   "-e", "SHELLCTL_AUTH_TOKEN=" + token,
                   "-e", "PATH=/opt/user-env/current/python/bin:/opt/office/python/bin:/opt/user-env/current/node/node_modules/.bin:/opt/office/node/node_modules/.bin:/usr/local/bin:/usr/bin:/bin",
                   "-e", "NODE_PATH=/opt/user-env/current/node/node_modules:/opt/office/node/node_modules", IMAGE)
        docker("start", name)
        docker("exec", "--user", "0", name, *MANAGER_PYTHON, "-c",
               "import os; os.makedirs('/opt/workbench-global', mode=0o755, exist_ok=True)")
        touch(key)
        return {"endpoint": "http://" + name + ":5004", "auth_token": token}


def runtime_epoch():
    # One fixed, single-process Agent backend is the deployment authority.
    name = os.environ["WORKBENCH_RUNTIME_CONTAINER"]
    info = json.loads(docker("inspect", name).stdout)[0]
    epoch = hashlib.sha256((info["Id"] + ":" + info["State"]["StartedAt"]).encode()).hexdigest()
    return {"epoch": epoch, "running": info["State"]["Running"]}


def stop_binding(key, binding):
    from planning import InspectionAdmission, stop_inspections

    binding = str(uuid.UUID(binding))
    inspection_count = stop_inspections(key, binding, docker=docker, prefix=PREFIX,
                                        admission=InspectionAdmission(STATE, lock))
    name, _ = identity(key)
    info = docker("inspect", name, check=False)
    if info.returncode or not json.loads(info.stdout)[0]["State"]["Running"]:
        return {"stopped": inspection_count}
    script = Path(__file__).with_name("stop_jobs.py").read_text()
    result = docker("exec", "--user", "1000", "-i", name, *MANAGER_PYTHON, "-c", script,
                    stdin=json.dumps({"binding_id": binding}))
    output = json.loads(result.stdout)
    output["stopped"] = output.get("stopped", 0) + inspection_count
    return output


def authorize_mcp(payload):
    """Only the configured API can confirm a currently owned execution."""
    request = Request(MCP_API_URL + "/inner/api/agent/workbench/mcp/authorize",
                      data=json.dumps(payload).encode(), method="POST",
                      headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"})
    try:
        with build_opener(ProxyHandler({})).open(request, timeout=5) as response:
            return json.loads(response.read(1024)).get("authorized") is True
    except Exception:
        return False


def operation(key, action, payload):
    name, _ = identity(key)
    if action == "runtime-epoch":
        return runtime_epoch()
    if action in ("stop-binding", "fence-binding"):
        # MCP cancellation never waits for a workspace operation or tool call.
        # The epoch check still owns permission to fence an apparently live run.
        if action == "fence-binding":
            previous = payload.get("epoch", "")
            if len(previous) != 64 or any(ch not in "0123456789abcdef" for ch in previous):
                raise ValueError("Invalid runtime epoch")
            current = runtime_epoch()
            if current["running"] and current["epoch"] == previous:
                return {"fenced": False}
        from mcp_runtime import invalidate

        invalidate(key, binding=str(uuid.UUID(payload["binding_id"])))
        with lock(key):
            result = stop_binding(key, payload["binding_id"])
            return {**result, "fenced": True}
    if action == "ensure":
        return ensure(key)
    if action == "touch":
        touch(key)
        return {"ok": True}
    if action in ("plan-admission", "plan-inspect"):
        from planning import InspectionAdmission, inspect_plan

        admission = InspectionAdmission(STATE, lock)
        if action == "plan-admission":
            return {"ticket": admission.ticket(key, payload["binding_id"])}
        return inspect_plan(
            key, payload, docker=docker, ensure=ensure, identity=identity,
            image=IMAGE, prefix=PREFIX, manager_python=MANAGER_PYTHON, admission=admission,
        )
    if action == "personal-mcp":
        from mcp_runtime import invalidate, invoke, module_source

        allowed = {"mcp_list", "mcp_read", "mcp_save", "mcp_delete", "mcp_toggle", "mcp_pin", "mcp_test", "mcp_call"}
        if payload.get("operation") not in allowed:
            raise ValueError("Unsupported MCP operation")
        with lock(key):
            ensure(key)
            request = payload
            if payload["operation"] in {"mcp_test", "mcp_call"}:
                request = {"operation": "mcp_probe", "name": payload.get("name")}
            source = module_source("file_ops", "resource_ops", "mcp_ops")
            source += "import json; print(json.dumps(sys.modules['mcp_ops'].personal_mcp(json.load(sys.stdin))))"
            result = docker("exec", "--user", "1000", "-i", name, *MANAGER_PYTHON, "-c", source,
                            stdin=json.dumps(request), check=False)
            if result.returncode:
                # Parse errors and tracebacks may include user-supplied secrets.
                raise ValueError("MCP 配置操作失败，请检查 JSON、路径和版本")
            output = json.loads(result.stdout)
            touch(key)
        if payload["operation"] in {"mcp_save", "mcp_delete", "mcp_toggle"} and not output.get("conflict"):
            invalidate(key, payload["name"])
        if payload["operation"] in {"mcp_test", "mcp_call"}:
            output = invoke(key, name, payload, output, authorize=authorize_mcp, journal=MCP_JOURNAL)
        touch(key)
        return output
    if action == "office-preview":
        # No owner volumes, credentials, network or writable image are exposed.
        preview_name = PREFIX + "-preview-" + uuid.uuid4().hex
        with lock(key):
            try:
                script = Path(__file__).with_name("office_preview.py").read_text()
                result = docker("run", "--rm", "--name", preview_name, "--network", "none",
                                "--read-only", "--tmpfs", "/tmp:rw,nosuid,noexec,size=256m",
                                "--user", "1000", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
                                "--memory", "1g", "--cpus", "1", "--pids-limit", "128", "-e", "HOME=/tmp",
                                "--entrypoint", MANAGER_PYTHON[0], "-i", IMAGE, *MANAGER_PYTHON[1:], "-c", script,
                                stdin=json.dumps(payload), timeout=55, check=False)
                output = json.loads(result.stdout or "{}")
                if result.returncode or not output.get("data"):
                    raise ValueError(output.get("error", "文档排版失败"))
                return output
            except subprocess.TimeoutExpired as error:
                raise ValueError("文档排版超时，请下载原文件查看") from error
            finally:
                docker("rm", "-f", preview_name, check=False)
    if action == "files":
        with lock(key):
            ensure(key)
            script = Path(__file__).with_name("file_ops.py").read_text()
            result = docker("exec", "--user", "1000", "-i", name, *MANAGER_PYTHON, "-c", script,
                            stdin=json.dumps(payload), check=False)
            output = json.loads(result.stdout or "{}")
            if result.returncode and not output.get("conflict"):
                raise ValueError(output.get("error", "File operation failed"))
            return output
    if action in ("personal-resources", "global-resources"):
        with lock(key):
            ensure(key)
            if action == "personal-resources" and payload.get("operation") == "global_install":
                raise ValueError("Global resources are read-only")
            if action == "global-resources" and payload.get("operation") != "global_install":
                raise ValueError("Unsupported global resource operation")
            helper = Path(__file__).with_name("file_ops.py").read_text()
            script = Path(__file__).with_name("resource_ops.py").read_text()
            source = (
                "import types,sys; helper=types.ModuleType('file_ops'); exec(" + repr(helper)
                + ",helper.__dict__); sys.modules['file_ops']=helper; exec(" + repr(script) + ")"
            )
            result = docker("exec", "--user", "0" if action == "global-resources" else "1000", "-i",
                            name, *MANAGER_PYTHON, "-c", source, stdin=json.dumps(payload), check=False)
            output = json.loads(result.stdout or "{}")
            if result.returncode and not output.get("conflict"):
                raise ValueError(output.get("error", "Resource operation failed"))
            return output
    if action == "environment":
        with lock(key):
            ensure(key)
            request_id = payload.get("request_id", "")
            if not request_id or len(request_id) > 128:
                raise ValueError("Environment update requires a stable request ID")
            record, installer = operation_file(key, request_id)
            if record.exists():
                return operation(key, "environment-status", {"request_id": request_id})
            save_operation(record, {"status": "installing"})
            script = Path(__file__).with_name("environment.py").read_text()
            # No Home, files, Docker socket or credentials are mounted into the installer.
            try:
                result = docker("run", "--rm", "--name", installer, "--user", "1000", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
                    "--memory", "2g", "--cpus", "2", "--pids-limit", "256", "--entrypoint", MANAGER_PYTHON[0],
                    "-v", name + "-env:/opt/user-env", "-i", IMAGE, *MANAGER_PYTHON[1:], "-c", script,
                    stdin=json.dumps(payload), timeout=900)
                output = json.loads(result.stdout)
            except subprocess.TimeoutExpired:
                # The installer may still be running. Keep the gate closed until its outcome is known.
                return {"status": "installing"}
            except Exception as error:
                # Installer stderr contains package diagnostics, never sandbox credentials.
                record.with_suffix(".error.log").write_text(str(error)[-12000:])
                output = {"status": "failed", "message": "安装失败，原共享环境已保留。"}
            save_operation(record, output)
            return output
    if action == "environment-status":
        record, installer = operation_file(key, payload.get("request_id", ""))
        if not record.exists():
            return {"status": "failed", "message": "安装请求未执行，原环境已保留。"}
        value = json.loads(record.read_text())
        if value.get("status") != "installing":
            return value
        result = docker("inspect", "--format", "{{.State.Running}}", installer, check=False)
        if result.returncode == 0 and result.stdout.strip() == "true":
            return value
        # Recover a switch that completed just before a manager restart.
        current = docker("exec", "--user", "1000", name, *MANAGER_PYTHON, "-c",
                         "from pathlib import Path; p=Path('/opt/user-env/current/request.json'); print(p.read_text() if p.exists() else '{}')", check=False)
        try:
            applied = json.loads(current.stdout).get("request_id") == payload.get("request_id")
        except ValueError:
            applied = False
        value = {"status": "ready" if applied else "failed", "message": "环境已更新。" if applied else "安装中断，原环境已保留。"}
        save_operation(record, value)
        return value
    if action == "clean-binding":
        from planning import scratch_name

        binding = str(uuid.UUID(payload["binding_id"]))
        with lock(key):
            stop_binding(key, binding)
            ensure(key)
            docker("exec", "--user", "1000", name, *MANAGER_PYTHON, "-c",
                   "import shutil; shutil.rmtree('/home/dify/" + binding + "',ignore_errors=True); "
                   "shutil.rmtree('/workspace/conversations/" + binding + "',ignore_errors=True)")
            docker("volume", "rm", scratch_name(name, binding), check=False)
            return {"ok": True}
    raise ValueError("Unsupported operation")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_POST(self):
        code = 200
        try:
            if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + TOKEN):
                self.send_error(403)
                return
            parts = self.path.strip("/").split("/")
            if len(parts) != 3 or parts[0] != "sandboxes":
                raise ValueError("Unsupported route")
            key = str(uuid.UUID(parts[1]))
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 <= size <= MAX_BODY:
                raise ValueError("Request too large")
            payload = json.loads(self.rfile.read(size) or "{}")
            output = operation(key, parts[2], payload)
            if output.get("conflict"):
                code = 409
        except (ValueError, OSError) as error:
            code, output = 400, {"error": str(error)}
        except Exception:
            code, output = 503, {"error": "Sandbox operation failed; current data and environment retained"}
        data = json.dumps(output).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def reap():
    while True:
        time.sleep(30)
        try:
            MCP_JOURNAL.prune()
        except (ValueError, OSError, sqlite3.Error):
            pass
        for item in STATE.iterdir():
            try:
                key = str(uuid.UUID(item.name))
                with lock(key):
                    if time.time() - item.stat().st_mtime > 1800:
                        from mcp_runtime import invalidate

                        invalidate(key)
                        name, _ = identity(key)
                        docker("stop", "--time", "10", name, check=False)
            except (ValueError, OSError):
                continue


if __name__ == "__main__":
    if len(TOKEN) < 32:
        raise ValueError("Manager token must contain at least 32 characters")
    threading.Thread(target=reap, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 5010), Handler).serve_forever()
