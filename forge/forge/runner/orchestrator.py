"""
forge/forge/runner/orchestrator.py
====================================
ProcessOrchestrator — manages multiple named long-running processes per project.

Replaces the single-process sandbox.py for full-stack projects. Allows the
frontend, backend, and database to run simultaneously under different names,
each with their own port and log buffer.

Used by:
  - Module 3 agent tools (start_process / stop_process import from here)
  - Module 4 API endpoints (/api/processes/...)
  - Module 7 AgentTerminalPanel (shows per-process logs + status)
"""

from __future__ import annotations

import asyncio
import os
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from forge.runner.workspace import workspace_manager


# ─────────────────────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ProcessInfo:
    """Snapshot of a managed process (safe to serialise to JSON)."""
    name:       str
    command:    str
    port:       int
    pid:        int | None  = None
    status:     str         = "stopped"   # stopped | starting | running | crashed | stopping
    exit_code:  int | None  = None
    started_at: float | None = None
    log_tail:   list[str]   = field(default_factory=list)  # last N lines
    error_msg:  str | None  = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name":       self.name,
            "command":    self.command,
            "port":       self.port,
            "pid":        self.pid,
            "status":     self.status,
            "exit_code":  self.exit_code,
            "started_at": self.started_at,
            "log_tail":   self.log_tail[-50:],   # cap at 50 lines for API response
            "error_msg":  self.error_msg,
        }


@dataclass
class _ManagedProcess:
    """Internal: wraps asyncio.Process + metadata."""
    info:    ProcessInfo
    process: asyncio.subprocess.Process | None = None
    _log_task: asyncio.Task | None = None
    _log_lines: list[str] = field(default_factory=list)
    MAX_LOG = 500   # keep last 500 lines in memory


# ─────────────────────────────────────────────────────────────────────────────
# Orchestrator
# ─────────────────────────────────────────────────────────────────────────────

class ProcessOrchestrator:
    """
    Manages named processes per project.

    Usage:
        orch = ProcessOrchestrator()

        # Start frontend dev server
        info = await orch.start(
            project_id="abc123",
            name="frontend",
            command="npm run dev -- --port 5173",
            port=5173,
        )

        # Get all running processes for a project
        procs = orch.list_processes("abc123")

        # Stream logs from a process
        async for line in orch.stream_logs("abc123", "frontend"):
            print(line)

        # Stop a process
        await orch.stop("abc123", "frontend")

        # Stop all processes for a project (e.g. on project close)
        await orch.stop_all("abc123")
    """

    def __init__(self) -> None:
        # {project_id: {name: _ManagedProcess}}
        self._registry: dict[str, dict[str, _ManagedProcess]] = {}

    # ── Start ──────────────────────────────────────────────────────────────────

    async def start(
        self,
        project_id: str,
        name: str,
        command: str,
        port: int,
        env: dict[str, str] | None = None,
        on_log: Callable[[str, str, str], None] | None = None,
    ) -> ProcessInfo:
        """
        Start a named process for a project.

        Args:
            project_id: project UUID
            name:       logical name ("frontend", "backend", "db")
            command:    shell command to run
            port:       port the process listens on (metadata only — not enforced here)
            env:        extra env vars (merged with inherited)
            on_log:     optional callback(project_id, name, line) for real-time log streaming

        Returns:
            ProcessInfo snapshot.
        """
        workspace = workspace_manager.path(project_id)

        # Kill any existing process with this name
        await self._kill_existing(project_id, name)

        # ── Ensure package.json has a "dev" script before running ────────────
        # The agent sometimes generates a package.json without one, causing
        # `npm run dev` to fail with "Missing script: dev". Patch it here so
        # start_process never fails for this reason.
        if "npm run dev" in command or "npm run start" in command:
            import json as _json, re as _re
            pkg_path = workspace / "package.json"
            if pkg_path.exists():
                try:
                    pkg = _json.loads(pkg_path.read_text())
                    scripts = pkg.get("scripts", {})
                    changed = False
                    # Strip hardcoded port flags so PORT env var wins
                    for key in ("dev", "start", "serve", "preview"):
                        if key in scripts:
                            orig = scripts[key]
                            patched = _re.sub(r'\s+(?:--port=\d+|-p\s+\d+|--port\s+\d+)', '', orig).strip()
                            if patched != orig:
                                scripts[key] = patched
                                changed = True
                    # Inject missing "dev" script based on installed framework
                    if "dev" not in scripts:
                        deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
                        if "next" in deps:
                            scripts["dev"] = "next dev"
                        elif "vite" in deps:
                            scripts["dev"] = "vite"
                        elif "react-scripts" in deps:
                            scripts["dev"] = "react-scripts start"
                        elif (workspace / "vite.config.ts").exists() or (workspace / "vite.config.js").exists():
                            scripts["dev"] = "vite"
                        elif (workspace / "next.config.js").exists() or (workspace / "next.config.ts").exists():
                            scripts["dev"] = "next dev"
                        elif "start" in scripts:
                            scripts["dev"] = scripts["start"]
                        changed = True
                        print(f"[orchestrator] injected missing 'dev' script: {scripts.get('dev')!r}", flush=True)
                    if changed:
                        pkg["scripts"] = scripts
                        pkg_path.write_text(_json.dumps(pkg, indent=2))
                except Exception as _e:
                    print(f"[orchestrator] could not patch package.json: {_e}", flush=True)

        merged_env = {**os.environ, "HOME": str(Path.home()), **(env or {})}

        info = ProcessInfo(
            name=name, command=command, port=port,
            status="starting", started_at=time.time(),
        )
        managed = _ManagedProcess(info=info)
        self._registry.setdefault(project_id, {})[name] = managed

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=str(workspace),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=merged_env,
            )
            managed.process = proc
            info.pid    = proc.pid
            info.status = "running"

            # Start background log reader
            managed._log_task = asyncio.create_task(
                self._read_logs(project_id, name, managed, proc, on_log)
            )

        except Exception as e:
            info.status    = "crashed"
            info.error_msg = str(e)

        return info

    # ── Stop ───────────────────────────────────────────────────────────────────

    async def stop(
        self,
        project_id: str,
        name: str,
        timeout: float = 10.0,
    ) -> ProcessInfo | None:
        """Stop a named process. Returns final ProcessInfo or None if not found."""
        managed = self._registry.get(project_id, {}).get(name)
        if not managed:
            return None

        info = managed.info
        info.status = "stopping"

        proc = managed.process
        if proc and proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
            info.exit_code = proc.returncode

        if managed._log_task and not managed._log_task.done():
            managed._log_task.cancel()
            try:
                await managed._log_task
            except asyncio.CancelledError:
                pass

        info.status = "stopped"
        self._registry.get(project_id, {}).pop(name, None)
        return info

    async def stop_all(self, project_id: str) -> list[ProcessInfo]:
        """Stop all processes for a project."""
        names = list(self._registry.get(project_id, {}).keys())
        results = []
        for name in names:
            info = await self.stop(project_id, name)
            if info:
                results.append(info)
        self._registry.pop(project_id, None)
        return results

    # ── Query ──────────────────────────────────────────────────────────────────

    def list_processes(self, project_id: str) -> list[ProcessInfo]:
        """Return current ProcessInfo for all processes in a project."""
        project_procs = self._registry.get(project_id, {})
        result = []
        for name, managed in project_procs.items():
            info = managed.info
            # Refresh status from underlying process
            if managed.process and managed.process.returncode is not None:
                if info.status == "running":
                    info.status    = "crashed" if managed.process.returncode != 0 else "stopped"
                    info.exit_code = managed.process.returncode
            # Attach recent log tail
            info.log_tail = managed._log_lines[-50:]
            result.append(info)
        return result

    def get_process(self, project_id: str, name: str) -> ProcessInfo | None:
        managed = self._registry.get(project_id, {}).get(name)
        if not managed:
            return None
        managed.info.log_tail = managed._log_lines[-50:]
        return managed.info

    def get_logs(
        self,
        project_id: str,
        name: str,
        last_n: int = 100,
    ) -> list[str]:
        """Return the last N log lines for a process."""
        managed = self._registry.get(project_id, {}).get(name)
        if not managed:
            return []
        return managed._log_lines[-last_n:]

    # ── Internal ───────────────────────────────────────────────────────────────

    async def _kill_existing(self, project_id: str, name: str) -> None:
        """Kill and deregister any existing process with this name."""
        existing = self._registry.get(project_id, {}).get(name)
        if not existing:
            return
        proc = existing.process
        if proc and proc.returncode is None:
            try:
                proc.kill()
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:
                pass
        if existing._log_task and not existing._log_task.done():
            existing._log_task.cancel()
        self._registry.get(project_id, {}).pop(name, None)

    async def _read_logs(
        self,
        project_id: str,
        name: str,
        managed: _ManagedProcess,
        proc: asyncio.subprocess.Process,
        on_log: Callable[[str, str, str], None] | None,
    ) -> None:
        """Background task: read lines from process stdout and store in _log_lines."""
        if proc.stdout is None:
            return
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                decoded = line.decode(errors="replace").rstrip("\n")
                managed._log_lines.append(decoded)
                if len(managed._log_lines) > managed.MAX_LOG:
                    managed._log_lines = managed._log_lines[-managed.MAX_LOG:]
                if on_log:
                    try:
                        on_log(project_id, name, decoded)
                    except Exception:
                        pass
        except asyncio.CancelledError:
            pass
        except Exception:
            pass


# ── Singleton ─────────────────────────────────────────────────────────────────
orchestrator = ProcessOrchestrator()
