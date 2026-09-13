"""Private container control plane; exposes no arbitrary Docker or shell operation."""
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import uuid

TOKEN = os.environ["WORKBENCH_SANDBOX_MANAGER_TOKEN"]
IMAGE = os.environ.get("WORKBENCH_SANDBOX_IMAGE", "langgenius/dify-agent-local-sandbox:1.17.1")
NETWORK = os.environ.get("WORKBENCH_SANDBOX_NETWORK", "dify-workbench-sandboxes")
PREFIX = os.environ.get("WORKBENCH_SANDBOX_PREFIX", "dify-wb-dev")
STATE = Path(os.environ.get("WORKBENCH_MANAGER_STATE", "/state"))
STATE.mkdir(parents=True, exist_ok=True)
LOCKS = {}
LOCKS_GUARD = threading.Lock()
MAX_BODY = 30 * 1024 * 1024


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
            if existing["Config"]["Image"] != IMAGE and not existing["State"]["Running"]:
                # Keep the stopped container as a rollback reference and reuse all owner volumes.
                docker("rename", name, name + "-previous-" + existing["Id"][:12])
                info = docker("inspect", name, check=False)
        if info.returncode:
            for suffix in ("home", "files", "env"):
                docker("volume", "create", "--label", "workbench=" + PREFIX, name + "-" + suffix)
            docker("run", "--rm", "--user", "0", "--network", "none", "--entrypoint", "python",
                   "-v", name + "-home:/home/dify", "-v", name + "-files:/workspace", "-v", name + "-env:/opt/user-env",
                   IMAGE, "-c", "import os; paths=['/home/dify','/workspace','/workspace/conversations','/workspace/" + key + "','/opt/user-env']; "
                   "[(os.makedirs(p,exist_ok=True),os.chown(p,1000,1000)) for p in paths]")
            # Existing personal venvs gain the immutable office fallback without replacing their packages.
            docker("run", "--rm", "--user", "1000", "--network", "none", "--entrypoint", "/usr/local/bin/python",
                   "-v", name + "-env:/opt/user-env", IMAGE, "-c",
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
        touch(key)
        return {"endpoint": "http://" + name + ":5004", "auth_token": token}


def runtime_epoch():
    # One fixed, single-process Agent backend is the deployment authority.
    name = os.environ["WORKBENCH_RUNTIME_CONTAINER"]
    info = json.loads(docker("inspect", name).stdout)[0]
    epoch = hashlib.sha256((info["Id"] + ":" + info["State"]["StartedAt"]).encode()).hexdigest()
    return {"epoch": epoch, "running": info["State"]["Running"]}


def stop_binding(key, binding):
    binding = str(uuid.UUID(binding))
    name, _ = identity(key)
    info = docker("inspect", name, check=False)
    if info.returncode or not json.loads(info.stdout)[0]["State"]["Running"]:
        return {"stopped": 0}
    script = Path(__file__).with_name("stop_jobs.py").read_text()
    result = docker("exec", "--user", "1000", "-i", name, "python", "-c", script,
                    stdin=json.dumps({"binding_id": binding}))
    return json.loads(result.stdout)


def operation(key, action, payload):
    name, _ = identity(key)
    if action == "runtime-epoch":
        return runtime_epoch()
    if action in ("stop-binding", "fence-binding"):
        with lock(key):
            if action == "fence-binding":
                previous = payload.get("epoch", "")
                if len(previous) != 64 or any(ch not in "0123456789abcdef" for ch in previous):
                    raise ValueError("Invalid runtime epoch")
                current = runtime_epoch()
                if current["running"] and current["epoch"] == previous:
                    return {"fenced": False}
            result = stop_binding(key, payload["binding_id"])
            return {**result, "fenced": True}
    if action == "ensure":
        return ensure(key)
    if action == "touch":
        touch(key)
        return {"ok": True}
    if action == "files":
        with lock(key):
            ensure(key)
            script = Path(__file__).with_name("file_ops.py").read_text()
            result = docker("exec", "--user", "1000", "-i", name, "python", "-c", script,
                            stdin=json.dumps(payload), check=False)
            output = json.loads(result.stdout or "{}")
            if result.returncode and not output.get("conflict"):
                raise ValueError(output.get("error", "File operation failed"))
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
                    "--memory", "2g", "--cpus", "2", "--pids-limit", "256", "--entrypoint", "python",
                    "-v", name + "-env:/opt/user-env", "-i", IMAGE, "-c", script,
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
        current = docker("exec", "--user", "1000", name, "python", "-c",
                         "from pathlib import Path; p=Path('/opt/user-env/current/request.json'); print(p.read_text() if p.exists() else '{}')", check=False)
        try:
            applied = json.loads(current.stdout).get("request_id") == payload.get("request_id")
        except ValueError:
            applied = False
        value = {"status": "ready" if applied else "failed", "message": "环境已更新。" if applied else "安装中断，原环境已保留。"}
        save_operation(record, value)
        return value
    if action == "clean-binding":
        binding = str(uuid.UUID(payload["binding_id"]))
        with lock(key):
            ensure(key)
            docker("exec", "--user", "1000", name, "python", "-c",
                   "import shutil; shutil.rmtree('/home/dify/" + binding + "',ignore_errors=True); "
                   "shutil.rmtree('/workspace/conversations/" + binding + "',ignore_errors=True)")
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
        for item in STATE.iterdir():
            try:
                key = str(uuid.UUID(item.name))
                with lock(key):
                    if time.time() - item.stat().st_mtime > 1800:
                        name, _ = identity(key)
                        docker("stop", "--time", "10", name, check=False)
            except (ValueError, OSError):
                continue


if __name__ == "__main__":
    if len(TOKEN) < 32:
        raise ValueError("Manager token must contain at least 32 characters")
    threading.Thread(target=reap, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 5010), Handler).serve_forever()
