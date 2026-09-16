"""Ephemeral, offline inspection containers with read-only owner resources."""

import hashlib
import json
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import subprocess
import tempfile
import uuid


@dataclass
class InspectionAdmission:
    """Durable binding generation; startup and stop share the manager owner lock.

    The API obtains a ticket before rechecking its execution lease. Stopping
    rotates it, so a delayed HTTP request cannot reopen an already fenced turn.
    Only one record per binding is retained, including across manager restarts.
    """

    state: Path
    lock: object

    def _path(self, key, binding):
        digest = hashlib.sha256(
            (key + ":" + str(uuid.UUID(binding))).encode()
        ).hexdigest()
        return self.state / ("plan-admission-" + digest)

    def ticket(self, key, binding):
        with self.lock(key):
            path = self._path(key, binding)
            if not path.exists():
                self.revoke(key, binding)
            return path.read_text(encoding="utf-8")

    def revoke(self, key, binding):
        with self.lock(key):
            path = self._path(key, binding)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(uuid.uuid4().hex, encoding="utf-8")
            temporary.replace(path)

    @contextmanager
    def guard(self, key, binding, ticket):
        with self.lock(key):
            if (
                not isinstance(ticket, str)
                or not ticket
                or ticket != self.ticket(key, binding)
            ):
                raise ValueError("调查执行已停止，请核对当前任务后重试")
            yield


def remove_container(name, *, docker):
    result = docker("rm", "-f", name, check=False)
    if result.returncode and not docker("inspect", name, check=False).returncode:
        raise RuntimeError("无法确认调查容器已停止")


def copy_global_resources(source, destination):
    """Stream the trusted global tree; never buffer a resource archive in RAM."""
    with tempfile.TemporaryFile() as errors:
        export = subprocess.Popen(
            ["docker", "cp", source + ":/opt/workbench-global/.", "-"],
            stdout=subprocess.PIPE,
            stderr=errors,
        )
        try:
            copied = subprocess.run(
                [
                    "docker",
                    "exec",
                    "--user",
                    "0",
                    "-i",
                    destination,
                    "/bin/tar",
                    "--no-same-owner",
                    "-xf",
                    "-",
                    "-C",
                    "/opt/workbench-global",
                ],
                stdin=export.stdout,
                capture_output=True,
                timeout=30,
            )
            export.stdout.close()
            if export.wait(timeout=10) or copied.returncode:
                raise RuntimeError("无法准备计划调查所需的只读全局资源")
        finally:
            if export.poll() is None:
                export.kill()
                export.wait()


def scratch_name(owner, binding):
    return owner + "-plan-" + str(uuid.UUID(binding))


def inspect_plan(
    key, payload, *, docker, ensure, identity, image, prefix, manager_python, admission
):
    binding = str(uuid.UUID(payload["binding_id"]))
    execution = str(uuid.UUID(payload["execution_id"]))
    script = payload.get("script")
    timeout = payload.get("timeout", 30)
    previews = payload.get("preview_paths", [])
    request_key = payload.get("request_key", "")
    if not isinstance(script, str) or not script.strip() or len(script) > 100000:
        raise ValueError("调查命令不能为空或超过 100000 字符")
    if type(timeout) is not int or not 1 <= timeout <= 60:
        raise ValueError("调查命令超时必须为 1 至 60 秒")
    if (
        not isinstance(previews, list)
        or len(previews) > 4
        or any(not isinstance(p, str) for p in previews)
    ):
        raise ValueError("最多读取四张临时预览图片")
    if not isinstance(request_key, str) or not request_key or len(request_key) > 128:
        raise ValueError("调查命令缺少有效请求标识")
    owner, _ = identity(key)
    scratch = scratch_name(owner, binding)
    digest = hashlib.sha256(
        (key + ":" + execution + ":" + request_key).encode()
    ).hexdigest()
    name = prefix + "-plan-" + digest[:24]
    ticket = payload.get("admission_ticket")
    labels = (
        "--label",
        "workbench=" + prefix,
        "--label",
        "workbench.plan.owner=" + key,
        "--label",
        "workbench.plan.binding=" + binding,
    )
    initializer = name + "-init"
    created = initialized = False
    try:
        with admission.guard(key, binding, ticket):
            ensure(key)
            docker(
                "volume",
                "create",
                "--label",
                "workbench=" + prefix,
                "--label",
                "workbench.plan.binding=" + binding,
                scratch,
            )
            # Initialize only the scratch root in a short trusted process. The actual
            # inspection container never receives CHOWN/FOWNER capabilities.
            docker(
                "create",
                "--name",
                initializer,
                *labels,
                "--network",
                "none",
                "--user",
                "0",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--cap-add",
                "CHOWN",
                "--cap-add",
                "FOWNER",
                "--security-opt",
                "no-new-privileges:true",
                "-v",
                scratch + ":/tmp",
                "--entrypoint",
                manager_python[0],
                image,
                *manager_python[1:],
                "-c",
                "import os; os.chown('/tmp',1000,1000); os.chmod('/tmp',0o700)",
            )
            initialized = True
            docker("start", initializer)
        # Waiting does not hold the owner lock. Stop can remove this named,
        # labelled initializer even before the inspection container exists.
        initialized_result = docker("wait", initializer, timeout=20)
        if initialized_result.stdout.strip() != "0":
            raise ValueError("调查准备未完成或已停止")
        with admission.lock(key):
            remove_container(initializer, docker=docker)
        initialized = False
        with admission.guard(key, binding, ticket):
            docker(
                "create",
                "--name",
                name,
                "--label",
                "workbench=" + prefix,
                "--label",
                "workbench.plan.owner=" + key,
                "--label",
                "workbench.plan.binding=" + binding,
                "--network",
                "none",
                "--read-only",
                "--user",
                "1000",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                "--memory",
                "1g",
                "--cpus",
                "1",
                "--pids-limit",
                "128",
                "--volumes-from",
                owner + ":ro",
                "-v",
                scratch + ":/tmp",
                "--tmpfs",
                "/opt/workbench-global:rw,nosuid,nodev,noexec,size=256m,mode=0755",
                "-e",
                "HOME=/tmp/home",
                "-e",
                "TMPDIR=/tmp",
                "-e",
                "TMP=/tmp",
                "-e",
                "TEMP=/tmp",
                "-e",
                "XDG_CACHE_HOME=/tmp/cache",
                "-e",
                "PYTHONDONTWRITEBYTECODE=1",
                "-e",
                "PATH=/opt/user-env/current/python/bin:/opt/office/python/bin:/opt/user-env/current/node/node_modules/.bin:/opt/office/node/node_modules/.bin:/usr/local/bin:/usr/bin:/bin",
                "-e",
                "NODE_PATH=/opt/user-env/current/node/node_modules:/opt/office/node/node_modules",
                "--entrypoint",
                "/bin/sleep",
                image,
                "120",
            )
            created = True
            docker("start", name)
        copy_global_resources(owner, name)
        helper = (
            Path(__file__).with_name("planning_exec.py").read_text(encoding="utf-8")
        )
        result = docker(
            "exec",
            "--user",
            "1000",
            "--workdir",
            "/workspace/conversations/" + binding,
            "-i",
            name,
            *manager_python,
            "-c",
            helper,
            stdin=json.dumps(
                {"script": script, "timeout": timeout, "preview_paths": previews}
            ),
            timeout=timeout + 10,
            check=False,
        )
        if result.returncode:
            raise ValueError("计划调查未完成或已停止，请核对会话状态后重试")
        return json.loads(result.stdout)
    finally:
        # All child/background processes die with this disposable container.
        # Owner volumes were read-only throughout; investigation scratch remains.
        # Stop and request cleanup can run at the same time. Serialize Docker
        # removals so a second rm does not mistake "removal in progress" for a
        # failed fence, or return while the first removal is still incomplete.
        with admission.lock(key):
            if created:
                remove_container(name, docker=docker)
            if initialized:
                remove_container(initializer, docker=docker)


def stop_inspections(key, binding, *, docker, prefix, admission):
    binding = str(uuid.UUID(binding))
    with admission.lock(key):
        admission.revoke(key, binding)
        result = docker(
            "ps",
            "-aq",
            "--filter",
            "label=workbench=" + prefix,
            "--filter",
            "label=workbench.plan.owner=" + key,
            "--filter",
            "label=workbench.plan.binding=" + binding,
        )
        names = result.stdout.split()
        for name in names:
            remove_container(name, docker=docker)
        return len(names)
