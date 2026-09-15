"""Route only API-owned workbench bindings to per-user sandbox containers."""

from __future__ import annotations

import asyncio
import logging
import shlex
import time
from dataclasses import dataclass
from uuid import UUID

import httpx

from dify_agent.adapters.shell.shellctl import ShellctlCommands
from dify_agent.runtime_backend.local import LocalExecutionBindingBackend, _parse_local_binding_ref
from dify_agent.runtime_backend.protocols import (
    ExecutionBindingAllocation,
    ExecutionBindingCreateSpec,
    ExecutionBindingDestroySpec,
    RuntimeLayout,
    RuntimeLease,
)
from dify_agent.runtime_backend.shellctl import ShellctlRuntimeLease, run_shellctl_control_command

logger = logging.getLogger(__name__)


@dataclass
class WorkbenchRuntimeLease:
    lease: ShellctlRuntimeLease
    pulse: asyncio.Task[None]
    workspace: str
    binding: str

    @property
    def layout(self):
        return self.lease.layout

    @property
    def commands(self):
        return self.lease.commands


@dataclass
class WorkbenchExecutionBindingBackend:
    fallback: LocalExecutionBindingBackend
    manager_endpoint: str
    manager_token: str

    async def _manager(self, workspace: str, operation: str, payload: dict | None = None):
        workspace = str(UUID(workspace))
        async with httpx.AsyncClient(timeout=90, trust_env=False) as client:
            response = await client.post(
                f"{self.manager_endpoint.rstrip('/')}/sandboxes/{workspace}/{operation}",
                headers={"Authorization": "Bearer " + self.manager_token},
                json=payload or {},
            )
            response.raise_for_status()
            return response.json()

    async def _backend(self, workspace: str):
        target = await self._manager(workspace, "ensure")
        # Docker start acknowledges the process before shellctl begins listening.
        # Only retry this read-only readiness probe, never a submitted Shell job.
        deadline = time.monotonic() + 30
        async with httpx.AsyncClient(timeout=2, trust_env=False) as client:
            while True:
                try:
                    response = await client.get(target["endpoint"].rstrip("/") + "/healthz")
                    response.raise_for_status()
                    break
                except (httpx.TransportError, httpx.HTTPStatusError):
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Personal sandbox did not become ready") from None
                    await asyncio.sleep(0.25)
        return LocalExecutionBindingBackend(endpoint=target["endpoint"], auth_token=target["auth_token"])

    async def create_binding(self, spec: ExecutionBindingCreateSpec) -> ExecutionBindingAllocation:
        if not spec.workbench:
            return await self.fallback.create_binding(spec)
        if spec.home_snapshot_ref is not None:
            raise ValueError("Workbench bindings require independent Homes")
        UUID(spec.binding_id)
        backend = await self._backend(spec.workspace_id)
        # A user Workspace is shared, while each Binding owns its materialized Home.
        allocation = await backend.create_binding(spec)
        control = backend._control_lease(allocation.binding_ref)
        try:
            result = await run_shellctl_control_command(
                control.commands, "mkdir -p /workspace/conversations/" + shlex.quote(spec.binding_id)
            )
            if result.exit_code:
                raise RuntimeError("Failed to create conversation work directory")
        finally:
            await control.close()
        return ExecutionBindingAllocation(
            binding_ref="wb:" + allocation.binding_ref, workspace_ref=allocation.workspace_ref
        )

    async def acquire(self, binding_ref: str) -> RuntimeLease:
        if not binding_ref.startswith("wb:"):
            return await self.fallback.acquire(binding_ref)
        raw_ref = binding_ref[3:]
        binding, workspace = _parse_local_binding_ref(raw_ref)
        backend = await self._backend(workspace)
        lease = await backend.acquire(raw_ref)
        lease.layout = RuntimeLayout(
            home_dir=lease.layout.home_dir, workspace_dir="/workspace/conversations/" + binding
        )
        # The container is user-owned; cwd remains conversation-local. Global
        # packages live outside /workspace under a root-owned read-only prefix.
        control = backend._control_lease(raw_ref)
        temporary = lease.layout.home_dir + "/tmp"
        try:
            result = await run_shellctl_control_command(
                control.commands, "mkdir -p " + shlex.quote(temporary) + " " + shlex.quote(lease.layout.workspace_dir)
            )
            if result.exit_code:
                raise RuntimeError("Failed to prepare the conversation directory")
        finally:
            await control.close()
        lease.commands = ShellctlCommands(
            client=lease.client,
            home_dir=lease.layout.home_dir,
            workspace_dir=lease.layout.workspace_dir,
            default_cwd=lease.layout.workspace_dir,
            default_env={
                "SHELLCTL_LANDLOCK_RW_PATHS": "/workspace," + temporary,
                "TMPDIR": temporary,
                "TMP": temporary,
                "TEMP": temporary,
                "SHELLCTL_LANDLOCK_RO_PATHS": "/usr,/bin,/sbin,/lib,/lib64,/etc,/proc,/opt/dify-agent-tools,/opt/homebrew,/snap,/opt/user-env,/opt/office,/opt/google/chrome,/opt/workbench-global",
            },
        )

        return WorkbenchRuntimeLease(lease, asyncio.create_task(self._keep_alive(workspace)), workspace, binding)

    async def _keep_alive(self, workspace: str) -> None:
        """Keep retrying safe touch requests after a transient manager outage."""
        while True:
            await asyncio.sleep(30)
            try:
                await self._manager(workspace, "touch")
            except Exception:
                logger.warning("Workbench sandbox keepalive failed; will retry: %s", workspace, exc_info=True)

    async def release(self, lease: RuntimeLease) -> None:
        if not isinstance(lease, WorkbenchRuntimeLease):
            return await self.fallback.release(lease)
        lease.pulse.cancel()
        try:
            await lease.pulse
        except asyncio.CancelledError:
            pass
        finally:
            try:
                # A cancelled shell_run may not have returned its job ID yet.
                await self._manager(lease.workspace, "stop-binding", {"binding_id": lease.binding})
            finally:
                await lease.lease.close()

    async def destroy_binding(self, spec: ExecutionBindingDestroySpec) -> None:
        if not spec.binding_ref.startswith("wb:"):
            return await self.fallback.destroy_binding(spec)
        binding, workspace = _parse_local_binding_ref(spec.binding_ref[3:])
        if spec.workspace_ref is not None and spec.workspace_ref != workspace:
            raise ValueError("Workspace ref does not match binding")
        # Shared read-only dependencies outlive every conversation.
        await self._manager(workspace, "clean-binding", {"binding_id": binding})
