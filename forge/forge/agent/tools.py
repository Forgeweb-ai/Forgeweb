"""
forge/forge/agent/tools.py
==========================
The 8 tools the AI Agent can call. Each tool is defined twice:
  1. A JSON Schema dict (for the LLM's `tools` parameter)
  2. An async Python executor (called by loop.py when the LLM emits a tool_use block)

Tools:
  exec_command   — run a shell command in the project workspace
  write_file     — create or overwrite a file
  read_file      — read a file's content
  list_dir       — list files in a directory (with .gitignore-style skips)
  search_code    — ripgrep-style regex search across workspace files (NEW)
  start_process  — start a long-running process (dev server, backend, etc.)
  stop_process   — stop a named running process
  run_query      — run an SQL query against the project DB (SQLite only for now)

search_code mirrors Claude's own Grep tool:
  {"pattern": "useState", "path": "frontend/src", "glob": "*.tsx", "context": 3}
  Returns matching lines with file paths and line numbers — identical to rg output.
  Critical for large projects: lets the AI find exactly where something is defined
  before reading/editing, instead of blindly reading every file.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from forge.runner.workspace import workspace_manager
from forge.runner.orchestrator import orchestrator


# ─────────────────────────────────────────────────────────────────────────────
# 1. Tool JSON schemas (sent to the LLM)
# ─────────────────────────────────────────────────────────────────────────────

TOOL_SCHEMAS: list[dict] = [
    {
        "name": "exec_command",
        "description": (
            "Run a shell command in the project workspace directory. "
            "Use for: npm install, pip install, running tests, checking build output, "
            "checking if a port is in use, running DB migrations, etc. "
            "Commands run with a 60-second timeout. For long-running servers, use start_process instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to execute (run via bash -c).",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default 60, max 300).",
                    "default": 60,
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "write_file",
        "description": (
            "Create or completely overwrite a file in the project workspace. "
            "Intermediate directories are created automatically. "
            "Use this to write source code, config files, scripts, etc."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path relative to the project workspace root (e.g. 'src/App.tsx').",
                },
                "content": {
                    "type": "string",
                    "description": "Full file content to write.",
                },
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "read_file",
        "description": (
            "Read the full content of a file in the project workspace. "
            "Returns the file content as a string. Useful before editing — always read first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path relative to the project workspace root.",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "list_dir",
        "description": (
            "List files in a directory of the project workspace. "
            "Returns a JSON array of relative paths. "
            "Skips node_modules, .venv, __pycache__, .git, dist, build, .next, target."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory path relative to workspace root. Use '.' for root.",
                    "default": ".",
                },
                "max_depth": {
                    "type": "integer",
                    "description": "Maximum recursion depth (default 4).",
                    "default": 4,
                },
            },
            "required": [],
        },
    },
    {
        "name": "start_process",
        "description": (
            "Start a long-running process (dev server, backend API, etc.) in the background. "
            "The process keeps running after this call returns. "
            "Use exec_command for one-shot commands; use start_process for persistent servers. "
            "Returns immediately — check logs with exec_command after a short delay."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Logical name for this process (e.g. 'frontend', 'backend', 'db'). Used to stop it later.",
                },
                "command": {
                    "type": "string",
                    "description": "Shell command to start the process.",
                },
                "port": {
                    "type": "integer",
                    "description": "Port the process will listen on (for the preview proxy).",
                },
                "env": {
                    "type": "object",
                    "description": "Extra environment variables to set (merged with inherited env).",
                    "additionalProperties": {"type": "string"},
                },
            },
            "required": ["name", "command", "port"],
        },
    },
    {
        "name": "stop_process",
        "description": "Stop a named running process that was started with start_process.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Logical name of the process to stop.",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "search_code",
        "description": (
            "Search for a pattern across all files in the project workspace using ripgrep. "
            "Use this BEFORE read_file on large projects — find exactly where a symbol, "
            "function, class, import, or string is defined/used instead of guessing. "
            "\n\nExamples:"
            "\n  • Find all useState calls: pattern='useState', glob='*.tsx'"
            "\n  • Find where a function is defined: pattern='^def my_func', output_mode='content'"
            "\n  • Find all API routes: pattern='@app\\.(get|post|put|delete)', glob='*.py'"
            "\n  • Count files with a pattern: output_mode='count'"
            "\n  • Just file names: output_mode='files_with_matches'"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Regex pattern to search for (ripgrep syntax).",
                },
                "path": {
                    "type": "string",
                    "description": "File or directory to search in, relative to workspace root. Defaults to '.' (all files).",
                    "default": ".",
                },
                "glob": {
                    "type": "string",
                    "description": "Glob pattern to filter files (e.g. '*.py', '*.{ts,tsx}', 'src/**/*.js').",
                },
                "output_mode": {
                    "type": "string",
                    "enum": ["content", "files_with_matches", "count"],
                    "description": (
                        "content: show matching lines with context (default). "
                        "files_with_matches: just return file paths. "
                        "count: return match count per file."
                    ),
                    "default": "content",
                },
                "context": {
                    "type": "integer",
                    "description": "Lines of context to show before and after each match (only for output_mode=content). Default 2.",
                    "default": 2,
                },
                "case_insensitive": {
                    "type": "boolean",
                    "description": "Case-insensitive search. Default false.",
                    "default": False,
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of result lines to return. Default 200.",
                    "default": 200,
                },
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "run_query",
        "description": (
            "Run an SQL query against the project's SQLite database. "
            "Returns results as a JSON array of row objects. "
            "Useful for checking DB state, running migrations, seeding data, "
            "or verifying that writes succeeded. Only supports SQLite for now."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "db_path": {
                    "type": "string",
                    "description": "Path to the SQLite .db file, relative to the workspace root.",
                },
                "query": {
                    "type": "string",
                    "description": "SQL query to execute.",
                },
                "params": {
                    "type": "array",
                    "description": "Optional positional parameters for the query (? placeholders).",
                    "items": {},
                },
            },
            "required": ["db_path", "query"],
        },
    },
]


# ─────────────────────────────────────────────────────────────────────────────
# 2. Tool executor functions
# ─────────────────────────────────────────────────────────────────────────────

_SKIP_DIRS = {
    "node_modules", ".venv", "venv", "__pycache__", ".git",
    "dist", "build", ".next", "target", ".gradle", ".mvn",
    ".cache", "coverage", ".nyc_output",
}


async def exec_command(
    project_id: str,
    command: str,
    timeout: int = 60,
) -> dict[str, Any]:
    """Run a shell command in the workspace directory. Returns stdout, stderr, exit_code."""
    workspace = workspace_manager.path(project_id)
    timeout = min(timeout, 300)

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(workspace),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "HOME": str(Path.home())},
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return {
                "stdout": "",
                "stderr": f"Command timed out after {timeout}s",
                "exit_code": -1,
            }

        return {
            "stdout": stdout.decode(errors="replace")[:8000],
            "stderr": stderr.decode(errors="replace")[:4000],
            "exit_code": proc.returncode,
        }
    except Exception as e:
        return {"stdout": "", "stderr": str(e), "exit_code": -1}


async def write_file(
    project_id: str,
    path: str,
    content: str,
) -> dict[str, Any]:
    """Write (create or overwrite) a file in the workspace."""
    workspace = workspace_manager.path(project_id)
    dest = workspace / path
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        return {"ok": True, "path": path, "bytes": len(content.encode())}
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def read_file(
    project_id: str,
    path: str,
) -> dict[str, Any]:
    """Read a file from the workspace."""
    workspace = workspace_manager.path(project_id)
    dest = workspace / path
    try:
        content = dest.read_text(encoding="utf-8", errors="replace")
        return {"ok": True, "path": path, "content": content[:32000]}
    except FileNotFoundError:
        return {"ok": False, "error": f"File not found: {path}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def list_dir(
    project_id: str,
    path: str = ".",
    max_depth: int = 4,
) -> dict[str, Any]:
    """List files in a workspace directory, skipping build artifacts."""
    workspace = workspace_manager.path(project_id)
    base = workspace / path

    if not base.exists():
        return {"ok": False, "error": f"Directory not found: {path}"}

    entries: list[str] = []

    def _walk(current: Path, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            for item in sorted(current.iterdir()):
                if item.name.startswith(".") and item.name not in {".env", ".env.local"}:
                    if item.name not in {".env", ".env.local"}:
                        continue
                if item.is_dir():
                    if item.name in _SKIP_DIRS:
                        continue
                    _walk(item, depth + 1)
                else:
                    rel = item.relative_to(workspace)
                    entries.append(str(rel))
        except PermissionError:
            pass

    _walk(base, 0)
    return {"ok": True, "files": entries[:500]}


async def search_code(
    project_id: str,
    pattern: str,
    path: str = ".",
    glob: str | None = None,
    output_mode: str = "content",
    context: int = 2,
    case_insensitive: bool = False,
    max_results: int = 200,
) -> dict[str, Any]:
    """
    Search for a regex pattern in the workspace using ripgrep (rg).
    Falls back to Python re-based search if rg is not installed.

    Returns:
        {
          "ok": True,
          "output": "file:line:match...",   # raw rg output (content mode)
          "files": ["a.ts", "b.py"],        # files_with_matches mode
          "counts": {"a.ts": 3},            # count mode
          "match_count": 42,
          "truncated": False
        }
    """
    workspace = workspace_manager.path(project_id)
    search_root = workspace / path

    if not search_root.exists():
        return {"ok": False, "error": f"Path not found: {path}"}

    # Build rg command
    cmd_parts = ["rg", "--no-heading"]

    if case_insensitive:
        cmd_parts.append("-i")

    if glob:
        cmd_parts.extend(["--glob", glob])

    # Skip build artifact directories
    for skip in _SKIP_DIRS:
        cmd_parts.extend(["--glob", f"!{skip}/**"])

    if output_mode == "files_with_matches":
        cmd_parts.append("-l")
    elif output_mode == "count":
        cmd_parts.append("-c")
    else:
        # content mode — with line numbers and context
        cmd_parts.extend(["-n", f"--context={context}"])

    cmd_parts.extend([pattern, str(search_root)])

    result = await exec_command(
        project_id,
        command=" ".join(f'"{p}"' if " " in p else p for p in cmd_parts),
        timeout=30,
    )

    # rg exit code 1 = no matches (not an error), 2+ = real error
    if result["exit_code"] == 2:
        # rg not found — fall back to Python search
        return await _python_search(project_id, pattern, path, glob, output_mode, context, case_insensitive, max_results)

    raw_output = result["stdout"] or ""

    if output_mode == "files_with_matches":
        files = [
            str(Path(line).relative_to(workspace)) if Path(line).is_absolute() else line
            for line in raw_output.splitlines()
            if line.strip()
        ]
        return {"ok": True, "files": files[:max_results], "match_count": len(files), "truncated": len(files) > max_results}

    elif output_mode == "count":
        counts: dict[str, int] = {}
        for line in raw_output.splitlines():
            if ":" in line:
                fp, _, n = line.rpartition(":")
                try:
                    rel = str(Path(fp.strip()).relative_to(workspace)) if Path(fp.strip()).is_absolute() else fp.strip()
                    counts[rel] = int(n.strip())
                except (ValueError, Exception):
                    pass
        total = sum(counts.values())
        return {"ok": True, "counts": counts, "match_count": total, "truncated": False}

    else:
        # content mode — make paths relative
        lines = raw_output.splitlines()
        rel_lines = []
        for line in lines:
            # rg outputs: /abs/path/to/file:line_no:content
            # Convert absolute path prefix to relative
            if line and line[0] == "/" and ":" in line:
                parts = line.split(":", 2)
                if len(parts) >= 2:
                    try:
                        rel = str(Path(parts[0]).relative_to(workspace))
                        rel_lines.append(f"{rel}:{':'.join(parts[1:])}")
                        continue
                    except ValueError:
                        pass
            rel_lines.append(line)

        truncated = len(rel_lines) > max_results
        output_str = "\n".join(rel_lines[:max_results])
        return {
            "ok": True,
            "output": output_str,
            "match_count": len([l for l in rel_lines if l and not l.startswith("--")]),
            "truncated": truncated,
        }


async def _python_search(
    project_id: str,
    pattern: str,
    path: str,
    glob: str | None,
    output_mode: str,
    context: int,
    case_insensitive: bool,
    max_results: int,
) -> dict[str, Any]:
    """Pure Python fallback search when rg is unavailable."""
    import re
    import fnmatch

    workspace = workspace_manager.path(project_id)
    search_root = workspace / path
    flags = re.IGNORECASE if case_insensitive else 0

    try:
        compiled = re.compile(pattern, flags)
    except re.error as e:
        return {"ok": False, "error": f"Invalid regex: {e}"}

    output_lines: list[str] = []
    matched_files: list[str] = []
    file_counts: dict[str, int] = {}
    total = 0

    def _matches_glob(fp: Path) -> bool:
        if not glob:
            return True
        return fnmatch.fnmatch(fp.name, glob) or fnmatch.fnmatch(str(fp), glob)

    def _walk(p: Path) -> None:
        try:
            for item in sorted(p.iterdir()):
                if item.is_dir():
                    if item.name in _SKIP_DIRS:
                        continue
                    _walk(item)
                elif item.is_file() and _matches_glob(item):
                    _search_file(item)
        except PermissionError:
            pass

    def _search_file(fp: Path) -> None:
        nonlocal total
        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
            file_lines = text.splitlines()
            rel = str(fp.relative_to(workspace))
            hit_indices = [i for i, ln in enumerate(file_lines) if compiled.search(ln)]
            if not hit_indices:
                return
            matched_files.append(rel)
            file_counts[rel] = len(hit_indices)
            total += len(hit_indices)

            if output_mode == "content":
                for idx in hit_indices:
                    start = max(0, idx - context)
                    end   = min(len(file_lines), idx + context + 1)
                    for i in range(start, end):
                        sep = ":" if i == idx else "-"
                        output_lines.append(f"{rel}:{i+1}{sep}{file_lines[i]}")
                    output_lines.append("--")
        except Exception:
            pass

    _walk(search_root)

    if output_mode == "files_with_matches":
        return {"ok": True, "files": matched_files[:max_results], "match_count": len(matched_files), "truncated": len(matched_files) > max_results}
    elif output_mode == "count":
        return {"ok": True, "counts": file_counts, "match_count": total, "truncated": False}
    else:
        truncated = len(output_lines) > max_results
        return {"ok": True, "output": "\n".join(output_lines[:max_results]), "match_count": total, "truncated": truncated}


async def start_process(
    project_id: str,
    name: str,
    command: str,
    port: int,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Start a background process via the ProcessOrchestrator."""
    info = await orchestrator.start(
        project_id=project_id,
        name=name,
        command=command,
        port=port,
        env=env,
    )
    if info.status in ("running", "starting"):
        return {"ok": True, "name": name, "pid": info.pid, "port": port}
    return {"ok": False, "name": name, "error": info.error_msg or "Failed to start"}


async def stop_process(
    project_id: str,
    name: str,
) -> dict[str, Any]:
    """Stop a named background process via the ProcessOrchestrator."""
    info = await orchestrator.stop(project_id, name)
    if info is None:
        return {"ok": False, "error": f"No process named '{name}' for project {project_id}"}
    return {"ok": True, "name": name, "exit_code": info.exit_code}


async def run_query(
    project_id: str,
    db_path: str,
    query: str,
    params: list | None = None,
) -> dict[str, Any]:
    """Run an SQL query against a SQLite database in the workspace."""
    import sqlite3
    workspace = workspace_manager.path(project_id)
    full_path = workspace / db_path

    if not full_path.exists():
        return {"ok": False, "error": f"Database not found: {db_path}"}

    try:
        conn = sqlite3.connect(str(full_path))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(query, params or [])
        if query.strip().upper().startswith("SELECT"):
            rows = [dict(row) for row in cursor.fetchmany(200)]
            conn.close()
            return {"ok": True, "rows": rows, "count": len(rows)}
        else:
            conn.commit()
            affected = cursor.rowcount
            conn.close()
            return {"ok": True, "rows_affected": affected}
    except sqlite3.Error as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# 3. Dispatcher — maps tool name → executor
# ─────────────────────────────────────────────────────────────────────────────

async def dispatch_tool(
    project_id: str,
    tool_name: str,
    tool_input: dict[str, Any],
) -> dict[str, Any]:
    """
    Call the appropriate tool executor given tool_name and tool_input.
    Returns a plain dict that will be JSON-serialised and sent back to the LLM.
    Never raises — errors are returned as {"ok": False, "error": "..."}.
    """
    try:
        match tool_name:
            case "exec_command":
                return await exec_command(
                    project_id,
                    command=tool_input["command"],
                    timeout=tool_input.get("timeout", 60),
                )
            case "write_file":
                return await write_file(
                    project_id,
                    path=tool_input["path"],
                    content=tool_input["content"],
                )
            case "read_file":
                return await read_file(
                    project_id,
                    path=tool_input["path"],
                )
            case "list_dir":
                return await list_dir(
                    project_id,
                    path=tool_input.get("path", "."),
                    max_depth=tool_input.get("max_depth", 4),
                )
            case "search_code":
                return await search_code(
                    project_id,
                    pattern=tool_input["pattern"],
                    path=tool_input.get("path", "."),
                    glob=tool_input.get("glob"),
                    output_mode=tool_input.get("output_mode", "content"),
                    context=tool_input.get("context", 2),
                    case_insensitive=tool_input.get("case_insensitive", False),
                    max_results=tool_input.get("max_results", 200),
                )
            case "start_process":
                return await start_process(
                    project_id,
                    name=tool_input["name"],
                    command=tool_input["command"],
                    port=tool_input["port"],
                    env=tool_input.get("env"),
                )
            case "stop_process":
                return await stop_process(
                    project_id,
                    name=tool_input["name"],
                )
            case "run_query":
                return await run_query(
                    project_id,
                    db_path=tool_input["db_path"],
                    query=tool_input["query"],
                    params=tool_input.get("params"),
                )
            case _:
                return {"ok": False, "error": f"Unknown tool: {tool_name}"}
    except Exception as e:
        return {"ok": False, "error": f"Tool executor error: {e}"}
