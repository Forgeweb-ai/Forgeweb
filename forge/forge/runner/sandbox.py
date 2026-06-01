"""
forge/runner/sandbox.py
========================
Sandboxed subprocess runner.
Writes project files to a workspace directory, then executes the detected run command.
Streams stdout/stderr line-by-line as an async generator.

v3: Per-project process isolation.
  - Each project_id gets its own _ProjectState (proc + tmpdir + asyncio.Lock).
  - stop(project_id) only kills THAT project's process — other projects keep running.
  - No more global port 5174 fallback; every run uses the port allocated in the
    workspace port registry (or asks the OS for a free port when no project_id).
  - Fully backward-compatible: passing project_id=None still works via the legacy
    "None" slot in the per-project dict.
"""

import asyncio
import os
import signal
import shlex
import shutil
import socket
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator

from forge.runner.workspace import workspace_manager


# ── Port-freeing utility ──────────────────────────────────────────────────────

def _kill_port(port: int) -> None:
    """
    Kill any process currently listening on `port` (macOS / Linux).
    Uses lsof which is available on both platforms.
    Best-effort — silently ignores errors.
    """
    try:
        result = subprocess.run(
            ["lsof", "-ti", f":{port}", "-sTCP:LISTEN"],
            capture_output=True, text=True, check=False,
        )
        for pid_str in result.stdout.strip().splitlines():
            pid_str = pid_str.strip()
            if pid_str:
                try:
                    os.kill(int(pid_str), signal.SIGKILL)
                except (ProcessLookupError, ValueError, PermissionError):
                    pass
    except Exception:
        pass


def _find_free_port() -> int:
    """Ask the OS for a free port. Used as last-resort fallback when no project_id."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ── Entry-point heuristics ────────────────────────────────────────────────────

# Ordered list: first match wins
ENTRY_HINTS: list[tuple[str, list[str]]] = [
    # Python
    ("main.py",   ["python", "-u", "main.py"]),
    ("app.py",    ["python", "-u", "app.py"]),
    ("server.py", ["python", "-u", "server.py"]),
    ("run.py",    ["python", "-u", "run.py"]),
    # Node
    ("index.js",  ["node", "index.js"]),
    ("app.js",    ["node", "app.js"]),
    ("server.js", ["node", "server.js"]),
    ("index.ts",  ["npx", "ts-node", "index.ts"]),
    # Go
    ("main.go",   ["go", "run", "."]),
    # Ruby
    ("main.rb",   ["ruby", "main.rb"]),
    # Shell
    ("run.sh",    ["bash", "run.sh"]),
    ("start.sh",  ["bash", "start.sh"]),
]


def detect_command(files: list[dict], run_command: str) -> list[str] | None:
    """
    Returns the shell argv to execute the project.
    Priority: 1) run_command from AI output  2) entry-point heuristic

    Commands that contain shell operators (&&, &, |, ;) or start with `cd`
    are run via `bash -lc "..."` so the full shell pipeline executes correctly.
    """
    # 1. Use the AI-supplied run_command, skip HTML "open in browser" hints
    rc = (run_command or "").strip()
    if rc and "open index.html" not in rc.lower() and "open index" not in rc.lower():
        _SHELL_OPS = ("&&", "||", " & ", " | ", ";", "$(")
        needs_shell = rc.startswith("cd ") or any(op in rc for op in _SHELL_OPS)
        if needs_shell:
            return ["bash", "-lc", rc]
        if rc.startswith(("python", "node", "go", "ruby", "bash", "npm", "npx", "uvicorn", "ng ")):
            return rc.split()

    # 2. Match against known entry points — only at root level (not inside subdirs)
    paths = {f["path"] for f in files}
    for filename, cmd in ENTRY_HINTS:
        # Only match root-level files (no slash in path means it's at root)
        if filename in paths:
            return list(cmd)

    # 3. package.json at root → npm start
    if "package.json" in paths:
        return ["npm", "start"]

    return None


# ── Per-project state ─────────────────────────────────────────────────────────

@dataclass
class _ProjectState:
    """Holds the running subprocess and working directory for one project slot."""
    proc:   asyncio.subprocess.Process | None = None
    tmpdir: str | None = None

    @property
    def is_running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None


# ── Multi-project sandbox runner ──────────────────────────────────────────────

class SandboxRunner:
    """
    Manages one subprocess *per project*.

    Each project_id gets an isolated slot with its own asyncio.Lock so multiple
    projects can run concurrently without interfering with each other.

    Calling stop(project_id) only kills that project's process.
    Calling run(..., project_id=pid) uses the ports already allocated in the
    workspace port registry for pid, so the preview proxy can always find them.
    """

    def __init__(self) -> None:
        # {project_id | None: _ProjectState}
        self._states: dict[str | None, _ProjectState] = {}
        # Per-project asyncio locks — allow concurrent multi-project runs.
        # NOTE: asyncio.Lock() must be created inside an event loop in Python <3.10,
        # but in 3.10+ it works fine here.  We create them lazily on first use.
        self._locks:  dict[str | None, asyncio.Lock]  = {}

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _lock_for(self, project_id: str | None) -> asyncio.Lock:
        if project_id not in self._locks:
            self._locks[project_id] = asyncio.Lock()
        return self._locks[project_id]

    def _state_for(self, project_id: str | None) -> _ProjectState:
        if project_id not in self._states:
            self._states[project_id] = _ProjectState()
        return self._states[project_id]

    # ── Public API ─────────────────────────────────────────────────────────────

    @property
    def is_running(self) -> bool:
        """Backward-compat: True if the legacy (no-project-id) process is alive."""
        state = self._states.get(None)
        return state is not None and state.is_running

    def is_project_running(self, project_id: str | None) -> bool:
        """True if a process is currently alive for the given project_id."""
        state = self._states.get(project_id)
        return state is not None and state.is_running

    async def stop(self, project_id: str | None = None) -> None:
        """
        Kill the process for the given project_id (or the legacy global slot if None).
        Has NO effect on any other project's processes.
        """
        state = self._states.get(project_id)
        if not state:
            return

        if state.proc:
            try:
                if state.proc.returncode is None:
                    # Kill the whole process group so child processes (e.g. webpack
                    # workers) don't become orphans and keep holding the port.
                    try:
                        pgid = os.getpgid(state.proc.pid)
                        os.killpg(pgid, signal.SIGTERM)
                    except (ProcessLookupError, OSError):
                        state.proc.terminate()
                    try:
                        await asyncio.wait_for(state.proc.wait(), timeout=3.0)
                    except asyncio.TimeoutError:
                        try:
                            pgid = os.getpgid(state.proc.pid)
                            os.killpg(pgid, signal.SIGKILL)
                        except (ProcessLookupError, OSError):
                            state.proc.kill()
                        await state.proc.wait()
            except Exception:
                pass
            state.proc = None

        if state.tmpdir and os.path.exists(state.tmpdir):
            shutil.rmtree(state.tmpdir, ignore_errors=True)
            state.tmpdir = None

        self._states.pop(project_id, None)

    async def update_files(
        self,
        files: list[dict],
        delete_paths: list[str] | None = None,
        project_id: str | None = None,
    ) -> bool:
        """
        Hot-write updated file contents to the running project's directory.
        Vite's file watcher detects the changes and triggers HMR automatically.
        Returns True if the server was running and files were written, False otherwise.
        """
        state = self._states.get(project_id)
        if not state or not state.tmpdir or not os.path.exists(state.tmpdir):
            print(f"[runner] update_files: no active tmpdir for project {project_id}", flush=True)
            return False
        if not state.is_running:
            print(f"[runner] update_files: process not running for project {project_id}", flush=True)
            return False

        tmpdir = Path(state.tmpdir)
        written = 0
        deleted = 0
        for f in files:
            dest = tmpdir / f["path"]
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(f["content"], encoding="utf-8")
            written += 1

        for path in delete_paths or []:
            dest = tmpdir / path
            if dest.exists() and dest.is_file():
                dest.unlink()
                deleted += 1

        print(
            f"[runner] hot-updated {written} file(s), deleted {deleted} file(s) "
            f"in {state.tmpdir} (Vite HMR will pick up changes)",
            flush=True,
        )
        return True

    # ── Internal: one subprocess ───────────────────────────────────────────────

    async def _run_one(
        self,
        cmd:        list[str],
        cwd:        str,
        env:        dict,
        label:      str,
        state:      _ProjectState,
        timeout_s:  int = 0,
    ) -> AsyncIterator[dict]:
        """
        Spawn ONE subprocess, stream its stdout/stderr line-by-line, then yield
        the exit code as a final {"type": "exit", "code": rc} item. Caller
        decides what to do based on the exit code (chain another command, stop
        the whole pipeline, etc.). Stores the process in `state.proc`.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout           = asyncio.subprocess.PIPE,
                stderr           = asyncio.subprocess.PIPE,
                cwd              = cwd,
                env              = env,
                start_new_session= True,   # own process group → killpg kills all children
            )
        except FileNotFoundError:
            yield {"type": "error", "text": f"❌  Command not found: `{cmd[0]}` ({label})\n"}
            yield {"type": "_done", "code": 127}
            return

        state.proc = proc

        # ── Merge stdout + stderr through one queue, sentinel per stream ─────
        queue: asyncio.Queue = asyncio.Queue()

        async def _drain(stream, stream_type: str):
            assert stream is not None
            while True:
                line = await stream.readline()
                if not line:
                    break
                await queue.put({"type": stream_type, "text": line.decode("utf-8", errors="replace")})
            await queue.put(None)   # sentinel: this stream is done

        t_out = asyncio.create_task(_drain(proc.stdout, "stdout"))
        t_err = asyncio.create_task(_drain(proc.stderr, "stderr"))

        # If a timeout is set: after timeout_s seconds, STOP STREAMING but
        # do NOT kill the process. Dev servers keep running in the background
        # so Vite HMR can pick up file changes written via update_files().
        _TIMEOUT_SENTINEL = object()
        timeout_task: asyncio.Task | None = None
        if timeout_s > 0:
            async def _stop_streaming_after_timeout():
                try:
                    await asyncio.sleep(timeout_s)
                    if proc.returncode is None:
                        # Push a sentinel — the pump sees it and breaks out
                        # WITHOUT killing the process.
                        await queue.put(_TIMEOUT_SENTINEL)
                except asyncio.CancelledError:
                    pass
            timeout_task = asyncio.create_task(_stop_streaming_after_timeout())

        # Pump items from the queue, yielding live.
        timed_out = False
        done_streams = 0
        while done_streams < 2:
            item = await queue.get()
            if item is None:
                done_streams += 1
            elif item is _TIMEOUT_SENTINEL:
                # Timeout hit — stop streaming, but let the process keep running
                timed_out = True
                break
            else:
                yield item

        if timeout_task and not timeout_task.done():
            timeout_task.cancel()
            try:
                await timeout_task
            except (asyncio.CancelledError, Exception):
                pass

        if timed_out:
            # Cancel the drain tasks so they don't accumulate in the background.
            # The subprocess itself continues running (its pipes stay open).
            t_out.cancel()
            t_err.cancel()
            await asyncio.gather(t_out, t_err, return_exceptions=True)
            yield {
                "type": "_done", "code": 0, "timed_out": True,
                "text": f"\n⏱  {label} hit {timeout_s}s — streaming paused (server still running).\n",
            }
        else:
            await proc.wait()
            await asyncio.gather(t_out, t_err, return_exceptions=True)
            yield {"type": "_done", "code": proc.returncode if proc.returncode is not None else -1}

    # ── Main run method ────────────────────────────────────────────────────────

    async def run(
        self,
        files:          list[dict],
        run_command:    str,
        env_vars:       dict[str, str] | None = None,
        setup_commands: list[str]             | None = None,
        run_timeout_s:  int                   = 0,
        project_id:     str | None            = None,
    ) -> AsyncIterator[dict]:
        """
        Write project files to a workspace/tmpdir, run setup_commands (if any),
        then the detected run command. Streams output line-by-line as dicts.

        Each project_id runs in its own slot — stopping project A never affects
        project B.  The frontend port is read from the workspace port registry
        (same source used by the preview proxy) so both sides always agree on
        which port to use.

        Yields dicts:
          { "type": "status", "text": str }   — informational message
          { "type": "stdout", "text": str }   — process stdout line
          { "type": "stderr", "text": str }   — process stderr line
          { "type": "exit",   "text": str, "code": int }  — final exit
          { "type": "error",  "text": str }   — runner error (never ran)
        """
        lock  = self._lock_for(project_id)
        async with lock:
            # Stop any existing process for THIS project only — other projects unaffected.
            await self.stop(project_id)
            state = self._state_for(project_id)

            cmd = detect_command(files, run_command)
            if not cmd:
                yield {"type": "error", "text": "❌  Could not detect a run command for this project.\n"}
                return

            # ── Workspace selection ────────────────────────────────────────────
            # If a project_id is provided use the persistent workspace so that
            # node_modules / .venv / compiled artefacts survive across runs.
            # Otherwise fall back to a disposable temp directory.
            if project_id:
                workspace_manager.create(project_id)
                state.tmpdir = str(workspace_manager.path(project_id))
                tmpdir = Path(state.tmpdir)
                workspace_manager.write_files(project_id, files)
                yield {"type": "status", "text": f"📁  workspace: {state.tmpdir}\n"}
            else:
                state.tmpdir = tempfile.mkdtemp(prefix="forge_run_")
                tmpdir = Path(state.tmpdir)
                for f in files:
                    dest = tmpdir / f["path"]
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_text(f["content"], encoding="utf-8")

            env = {**os.environ}
            if env_vars:
                env.update(env_vars)

            # ── Port assignment ────────────────────────────────────────────────
            # Force dev servers onto this project's allocated frontend port so
            # multiple projects/users can run at once without fighting over a
            # shared port.
            #
            # Source of truth (in priority order):
            #   1. Caller-supplied PORT env var (rare — allows explicit override)
            #   2. Port registry allocation for this project_id
            #   3. OS-assigned free port (only for no-project-id temp runs)
            #
            # The preview proxy (proxy/preview.py) reads the SAME port from the
            # registry, so both sides always agree on the correct port.
            is_dev_cmd = any(k in " ".join(cmd).lower() for k in ("dev", "start", "serve", "vite", "webpack"))
            if is_dev_cmd:
                fe_port = int(env.get("PORT") or 0)
                if not fe_port and project_id:
                    ports   = workspace_manager.assign_ports(project_id)
                    fe_port = int(ports.get("fe") or 0)
                if not fe_port:
                    # Last resort for legacy/temp runs — ask the OS for a free port.
                    # This avoids the old hard-coded 5174 fallback.
                    fe_port = _find_free_port()

                env["PORT"] = str(fe_port)
                env["VITE_PORT"] = str(fe_port)
                env["npm_config_port"] = str(fe_port)

                # Belt-and-suspenders: kill any orphan process still holding this
                # project's allocated port.
                _kill_port(fe_port)
                yield {"type": "status", "text": f"🔌  Port {fe_port} cleared\n", "port": fe_port}

                # Patch package.json scripts to remove any hardcoded -p / --port
                # flag that would override our PORT env var.  Next.js and Vite
                # honour PORT only when no -p flag is present in the script.
                import json as _json
                import re  as _re
                pkg_path = tmpdir / "package.json"
                if pkg_path.exists():
                    try:
                        pkg = _json.loads(pkg_path.read_text())
                        scripts = pkg.get("scripts", {})
                        changed = False

                        # ── 1. Strip hardcoded port flags so our PORT env var wins ──
                        for key in ("dev", "start", "serve", "preview"):
                            if key in scripts:
                                orig = scripts[key]
                                patched = _re.sub(r'\s+(?:--port=\d+|-p\s+\d+|--port\s+\d+)', '', orig).strip()
                                if patched != orig:
                                    scripts[key] = patched
                                    changed = True

                        # ── 2. Auto-add missing "dev" script based on framework ────
                        # If there is no "dev" script at all, infer one from deps/files
                        # so that `npm run dev` never fails with "Missing script: dev".
                        if "dev" not in scripts:
                            deps = {
                                **pkg.get("dependencies", {}),
                                **pkg.get("devDependencies", {}),
                            }
                            if "next" in deps:
                                scripts["dev"] = "next dev"
                            elif "vite" in deps:
                                scripts["dev"] = "vite"
                            elif "react-scripts" in deps:
                                scripts["dev"] = "react-scripts start"
                            elif "parcel" in deps:
                                scripts["dev"] = "parcel index.html"
                            elif (tmpdir / "vite.config.ts").exists() or (tmpdir / "vite.config.js").exists():
                                scripts["dev"] = "vite"
                            elif (tmpdir / "next.config.js").exists() or (tmpdir / "next.config.ts").exists():
                                scripts["dev"] = "next dev"
                            elif "start" in scripts:
                                # Alias dev → start as last resort
                                scripts["dev"] = scripts["start"]
                            changed = True
                            print(f"[runner] injected missing 'dev' script: {scripts.get('dev')}", flush=True)

                        if changed:
                            pkg["scripts"] = scripts
                            pkg_path.write_text(_json.dumps(pkg, indent=2))
                            print(f"[runner] patched package.json scripts", flush=True)
                    except Exception as _e:
                        print(f"[runner] could not patch package.json: {_e}", flush=True)

            # ── Setup phase: run each setup_command sequentially ──────────────
            for setup_raw in (setup_commands or []):
                setup_cmd = (setup_raw or "").strip()
                if not setup_cmd:
                    continue
                # Skip hints we can't safely shell out (no shell expansion here)
                if any(s in setup_cmd.lower() for s in ("open ", "cd ", "echo ", "#")):
                    continue

                # If the command contains shell operators, run it through bash -c
                # rather than splitting (otherwise `npm install && npm test` would
                # try to exec `npm` with `install`, `&&`, `npm`, `test` as args).
                needs_shell = any(op in setup_cmd for op in ("&&", "||", "|", ";", ">", "<", "$("))
                cmd_argv = ["bash", "-lc", setup_cmd] if needs_shell else shlex.split(setup_cmd)

                yield {"type": "status", "text": f"⚙  setup: {setup_cmd}\n"}

                rc = 0
                async for item in self._run_one(
                    cmd_argv, str(tmpdir), env, f"setup `{setup_cmd}`", state,
                ):
                    if item.get("type") == "_done":
                        rc = item["code"]
                    else:
                        yield item

                if rc != 0:
                    yield {
                        "type": "exit",
                        "text": f"\n[Setup `{setup_cmd}` failed with code {rc}]\n",
                        "code": rc,
                    }
                    if not project_id and state.tmpdir:
                        shutil.rmtree(state.tmpdir, ignore_errors=True)
                    state.tmpdir = None
                    state.proc   = None
                    return

            # ── Main run phase ────────────────────────────────────────────────
            yield {"type": "status", "text": f"⚡  {' '.join(cmd)}\n"}

            rc = 0
            timed_out = False
            async for item in self._run_one(
                cmd, str(tmpdir), env, "run", state, timeout_s=run_timeout_s,
            ):
                if item.get("type") == "_done":
                    rc        = item["code"]
                    timed_out = item.get("timed_out", False)
                    if timed_out and item.get("text"):
                        yield {"type": "status", "text": item["text"]}
                else:
                    yield item

            # Only emit an exit event when the process actually exited.
            # When the run timed out the server is still running in the background —
            # emitting a fake "exit code 0" confuses the frontend into thinking the
            # server died and prevents the proxy preview from activating.
            if not timed_out:
                yield {
                    "type": "exit",
                    "text": f"\n[Process exited with code {rc}]\n",
                    "code": rc,
                }

            if timed_out:
                # Dev server is still running — keep tmpdir and proc alive so
                # update_files() can hot-write new files for Vite HMR.
                print(
                    f"[runner] dev server still running (project={project_id}) in "
                    f"{state.tmpdir} — streaming paused, ready for hot file updates",
                    flush=True,
                )
            else:
                # Process actually exited.
                # Only delete the directory when it was a temp dir (no project_id).
                if project_id:
                    print(f"[runner] process exited — workspace kept at {state.tmpdir}", flush=True)
                elif state.tmpdir:
                    shutil.rmtree(state.tmpdir, ignore_errors=True)
                state.tmpdir = None
                state.proc   = None


# ── Global singleton ──────────────────────────────────────────────────────────
runner = SandboxRunner()
