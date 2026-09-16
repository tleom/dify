"""Manager-side lifecycle of sandbox-local MCP workers; no MCP code runs here."""

import json
import queue
import subprocess
import threading
import time
from pathlib import Path
from uuid import UUID

MAX_LINE = 8 * 1024 * 1024 + 1
WORKERS = {}
GUARD = threading.RLock()
CALL_LOCKS = {}
ACTIVE = set()
PATH = "/opt/user-env/current/python/bin:/opt/office/python/bin:/opt/user-env/current/node/node_modules/.bin:/opt/office/node/node_modules/.bin:/usr/local/bin:/usr/bin:/bin"


def module_source(*names):
    base = Path(__file__).parent
    source = "import types,sys; "
    for name in names:
        source += f"module=types.ModuleType({name!r}); sys.modules[{name!r}]=module; exec({(base / (name + '.py')).read_text()!r},module.__dict__); "
    return source


class Worker:
    def __init__(self, container, name, version, timeout):
        source = module_source("file_ops", "resource_ops", "mcp_ops")
        source += (Path(__file__).with_name("mcp_worker.py")).read_text()
        self.version, self.timeout = version, timeout
        self.responses = queue.Queue(maxsize=2)
        self.close_guard = threading.Lock()
        self.closed = False
        self.process = subprocess.Popen(
            [
                "docker",
                "exec",
                "--user",
                "1000",
                "-i",
                container,
                "/usr/bin/env",
                "-i",
                "HOME=/home/dify",
                "LANG=C.UTF-8",
                "PATH=" + PATH,
                "NODE_PATH=/opt/user-env/current/node/node_modules:/opt/office/node/node_modules",
                "/opt/workbench-mcp/bin/python",
                "-I",
                "-u",
                "-c",
                source,
                name,
                version,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        threading.Thread(target=self._read, daemon=True).start()
        self.ready = None

    def _read(self):
        try:
            while line := self.process.stdout.readline(MAX_LINE):
                if len(line) >= MAX_LINE or not line.endswith(b"\n"):
                    break
                self.responses.put(json.loads(line), timeout=1)
        except (ValueError, OSError, queue.Full):
            pass
        finally:
            try:
                self.responses.put(
                    {
                        "error": "MCP 连接已关闭，执行结果未知；请先核实外部状态",
                        "uncertain": True,
                    },
                    timeout=1,
                )
            except queue.Full:
                pass

    def response(self, cancelled=None, timeout=None):
        deadline = time.monotonic() + (
            self.timeout + 15 if timeout is None else timeout
        )
        while time.monotonic() < deadline:
            if cancelled is not None and cancelled.is_set():
                return {
                    "error": "MCP 调用已停止，执行结果未知；请先核实外部状态",
                    "uncertain": True,
                }
            try:
                return self.responses.get(
                    timeout=min(0.1, max(0.001, deadline - time.monotonic()))
                )
            except queue.Empty:
                continue
        return {
            "error": "MCP 响应超时，执行结果未知；请先核实外部状态",
            "uncertain": True,
        }

    def send(self, payload):
        try:
            self.process.stdin.write(json.dumps(payload).encode() + b"\n")
            self.process.stdin.flush()
        except (BrokenPipeError, OSError):
            return {
                "error": "MCP 连接中断，执行结果未知；请先核实外部状态",
                "uncertain": True,
            }
        return None

    def close(self):
        with self.close_guard:
            if not self.closed:
                self.closed = True
                try:
                    self.process.stdin.close()
                except OSError:
                    pass
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            # stdin EOF is observed concurrently by the sandbox worker, including
            # during a business call; the SDK then tears down its child process.
            self.process.terminate()


class Invocation:
    def __init__(self, key, payload):
        self.key, self.name = key, payload["name"]
        self.cancelled, self.worker = threading.Event(), None
        authorization = payload.get("authorization") or {}
        self.binding = authorization.get("binding_id")
        if payload["operation"] == "mcp_call":
            if authorization.get("workspace_id") != key or not self.binding:
                raise ValueError("MCP invocation requires an owned execution binding")
            self.binding = str(UUID(self.binding))


def close_workers(workers):
    threads = [
        threading.Thread(target=worker.close, daemon=True) for worker in set(workers)
    ]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + 6
    for thread in threads:
        thread.join(max(0, deadline - time.monotonic()))


def invalidate(key, name=None, *, binding=None):
    """Cancel matching queued/in-flight calls without acquiring their call lock."""
    workers = []
    with GUARD:
        for call in list(ACTIVE):
            if (
                call.key == key
                and (name is None or call.name == name)
                and (binding is None or call.binding == binding)
            ):
                call.cancelled.set()
                if call.worker is not None:
                    workers.append(call.worker)
        for identity, worker in list(WORKERS.items()):
            if (
                identity[0] == key
                and (name is None or identity[1] == name)
                and (binding is None or worker in workers)
            ):
                workers.append(WORKERS.pop(identity))
    close_workers(workers)


def invoke(key, container, payload, state, *, authorize=None, journal=None):
    name = payload["name"]
    if payload.get("version") != state["version"]:
        return {"conflict": True}
    if payload["operation"] == "mcp_call" and not state["enabled"]:
        return {"error": "个人 MCP 已停用"}
    if payload["operation"] == "mcp_test":
        invalidate(key, name)
    call = Invocation(key, payload)
    identity = key, name
    with GUARD:
        mutex = CALL_LOCKS.setdefault(identity, threading.Lock())
        ACTIVE.add(call)
    acquired, worker, digest = False, None, None
    try:
        deadline = time.monotonic() + 20
        while not call.cancelled.is_set() and time.monotonic() < deadline:
            if mutex.acquire(timeout=0.1):
                acquired = True
                break
        if call.cancelled.is_set():
            return {"error": "MCP 执行已失效，本次操作未执行"}
        if not acquired:
            return {"error": "此 MCP 正忙，本次操作未执行，请稍后再试"}
        if payload["operation"] == "mcp_call" and (
            authorize is None or not authorize(payload["authorization"])
        ):
            return {"error": "MCP 当前任务或执行已失效，本次操作未执行"}
        with GUARD:
            worker = WORKERS.get(identity)
        if worker and (
            worker.version != state["version"] or worker.process.poll() is not None
        ):
            with GUARD:
                WORKERS.pop(identity, None)
            worker.close()
            worker = None
        if worker is None:
            worker = Worker(container, name, state["version"], state["timeout"])
            with GUARD:
                call.worker = worker
            worker.ready = worker.response(call.cancelled, timeout=25)
            if not worker.ready.get("ready"):
                worker.close()
                return worker.ready
            with GUARD:
                if not call.cancelled.is_set():
                    WORKERS[identity] = worker
        with GUARD:
            call.worker = worker
        if call.cancelled.is_set():
            worker.close()
            return {"error": "MCP 执行已失效，本次操作未执行"}
        if payload["operation"] == "mcp_call":
            if (
                authorize is None
                or journal is None
                or not authorize(payload["authorization"])
            ):
                return {"error": "MCP 当前任务或执行已失效，本次操作未执行"}
            digest, previous = journal.begin(key, payload)
            if previous is not None:
                return previous
        # This short critical section orders dispatch against stop/config changes.
        # External connection, authorization and response waits never hold it.
        with GUARD:
            if call.cancelled.is_set():
                result = {"error": "MCP 执行已失效，本次操作未执行"}
            else:
                result = worker.send(payload)
        if result is None:
            result = worker.response(call.cancelled)
        if digest is not None:
            journal.finish(digest, result)
        if result.get("uncertain") or result.get("conflict") or call.cancelled.is_set():
            with GUARD:
                if WORKERS.get(identity) is worker:
                    WORKERS.pop(identity)
            worker.close()
        return result
    finally:
        with GUARD:
            ACTIVE.discard(call)
        if acquired:
            mutex.release()
