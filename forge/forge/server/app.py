"""
forge/server/app.py
===================
FastAPI server. Streams everything via SSE.

New in v0.2:
  - PostgreSQL persistence (users, projects, conversations, messages, versions)
  - Context-aware updates via sentence-transformer vector search
  - Version history with restore-to-any-version
  - Session-based auth (no login required — X-Session-ID header)
"""

import json
import asyncio
import re
import time
from fastapi import FastAPI, HTTPException, Depends, Header, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse
from pathlib import Path
from typing import Optional, AsyncIterator
from pydantic import BaseModel

from forge.config import config
from forge.model import get_backend
from forge.auth import (
    hash_password, verify_password, create_token, get_current_user
)
from forge.data.schemas import (
    RunRequest, SaveProjectRequest, UpdateProjectFilesRequest,
    UpdateProjectMetaRequest,
    CreateConversationRequest, AddMessageRequest,
    ContextSearchRequest, RestoreVersionRequest,
)
from forge.runner.sandbox import runner as sandbox_runner
from forge.runner.workspace import workspace_manager
from forge.runner.orchestrator import orchestrator
from forge.runner.port_registry import port_registry
from forge.agent.loop import run_agent, abort_agent
from forge.proxy.preview import proxy_preview
from forge.db.database import get_db, init_db
from forge.db import crud
from forge.embeddings.indexer import build_index, embed_query
from forge.embeddings.search import search_context

from sqlalchemy.ext.asyncio import AsyncSession

app = FastAPI(
    title       = config.product_name,
    version     = config.version,
    description = "Mobile-first AI codebase generator powered by Kimi-K2",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins     = config.server.cors_origins,
    allow_methods     = ["*"],
    allow_headers     = ["*", "X-Session-ID"],
)

backend = get_backend()

WEB_DIR = Path(__file__).parent.parent.parent / "web"
if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

# Serve project thumbnails from the forge-data root
THUMBNAILS_DIR = Path(config.forge_data_root) / "thumbnails"
THUMBNAILS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/api/thumbnails", StaticFiles(directory=str(THUMBNAILS_DIR)), name="thumbnails")


@app.on_event("startup")
async def on_startup():
    try:
        await init_db()
        print("[DB] Tables created / verified OK", flush=True)
        # Idempotent migrations — add columns that older schemas may be missing.
        # Uses per-column try/except so one failure never blocks the rest.
        # Works on both SQLite and PostgreSQL.
        from sqlalchemy import text as _text
        from forge.db.database import engine as _engine, _is_sqlite

        _migrations = [
            ("context_summary",      "TEXT",    "''"),
            ("workspace_path",       "TEXT",    "''"),
            ("stack",                "TEXT",    "'{}'"),   # JSON stored as TEXT on SQLite
            ("fe_port",              "INTEGER", "0"),
            ("be_port",              "INTEGER", "0"),
            ("opencode_session_id",  "TEXT",    "NULL"),
            # showcase / gallery columns
            ("showcased_at",         "DATETIME","NULL"),
            ("showcase_name",        "TEXT",    "NULL"),
            ("showcase_description", "TEXT",    "NULL"),
            ("thumbnail_url",        "TEXT",    "NULL"),
        ]

        async with _engine.begin() as conn:
            for col, col_type, default in _migrations:
                try:
                    if _is_sqlite:
                        # SQLite: ALTER TABLE doesn't support IF NOT EXISTS —
                        # just attempt it; it raises OperationalError if already exists
                        await conn.execute(_text(
                            f"ALTER TABLE projects ADD COLUMN {col} {col_type} DEFAULT {default}"
                        ))
                    else:
                        await conn.execute(_text(
                            f"ALTER TABLE projects ADD COLUMN IF NOT EXISTS "
                            f"{col} {col_type} DEFAULT {default}"
                        ))
                except Exception:
                    pass  # column already exists — that's fine

        print("[DB] Migrations checked OK", flush=True)
    except Exception as e:
        print(f"[DB] WARNING: Could not init DB: {e}", flush=True)
        print("[DB] Running without persistence — set DATABASE_URL in .env", flush=True)


def get_session_id(x_session_id: Optional[str] = Header(default=None)) -> str:
    if not x_session_id:
        raise HTTPException(status_code=400, detail="X-Session-ID header is required")
    return x_session_id


def sse_event(event_type: str, data) -> str:
    payload = data if isinstance(data, str) else json.dumps(data)
    return f"event: {event_type}\ndata: {payload}\n\n"


REASONING_PREFIX  = "__FORGE_REASONING__:"
ERROR_PREFIX      = "__FORGE_ERROR__:"
STATUS_PREFIX     = "__FORGE_STATUS__:"
PLAN_PREFIX       = "__FORGE_PLAN__:"
FILE_START_PREFIX = "__FORGE_FILE_START__:"
FILE_DONE_PREFIX  = "__FORGE_FILE_DONE__:"
RESULT_PREFIX     = "__FORGE_RESULT__:"
THOUGHT_PREFIX    = "__FORGE_THOUGHT__:"


def _classify(token: str):
    if token.startswith(REASONING_PREFIX):  return "reasoning",  token[len(REASONING_PREFIX):]
    if token.startswith(ERROR_PREFIX):      return "error",      token[len(ERROR_PREFIX):]
    if token.startswith(STATUS_PREFIX):     return "status",     token[len(STATUS_PREFIX):]
    if token.startswith(PLAN_PREFIX):       return "plan",       token[len(PLAN_PREFIX):]
    if token.startswith(FILE_START_PREFIX): return "file_start", token[len(FILE_START_PREFIX):]
    if token.startswith(FILE_DONE_PREFIX):  return "file_done",  token[len(FILE_DONE_PREFIX):]
    if token.startswith(RESULT_PREFIX):     return "result",     token[len(RESULT_PREFIX):]
    if token.startswith(THOUGHT_PREFIX):    return "thought",    token[len(THOUGHT_PREFIX):]
    return "token", token


_SEARCH_STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "can", "do",
    "does", "for", "from", "has", "have", "how", "i", "in", "is", "it",
    "like", "make", "my", "of", "on", "or", "our", "please", "should",
    "that", "the", "this", "to", "update", "use", "want", "with", "you",
}


def _tokenize_search_query(instruction: str) -> list[str]:
    tokens = re.findall(r"[A-Za-z0-9_.$#@/-]{2,}", instruction.lower())
    kept: list[str] = []
    for token in tokens:
        normalized = token.strip("/.,:;()[]{}\"'")
        if len(normalized) < 2 or normalized in _SEARCH_STOP_WORDS:
            continue
        if normalized not in kept:
            kept.append(normalized)
    return kept[:24]


def _lexical_context_search(instruction: str, files: list, top_k: int = 4) -> list[dict]:
    """
    Fast local fallback when embeddings are missing/stale.
    Scores file paths, names, and exact content hits so updates can still read a
    small targeted set of files instead of sending the whole codebase.
    """
    terms = _tokenize_search_query(instruction)
    if not terms:
        return []

    scored: list[dict] = []
    for file in files:
        path = getattr(file, "path", "") or ""
        content = getattr(file, "content", "") or ""
        path_l = path.lower()
        content_l = content.lower()
        score = 0.0
        line_hits: list[dict] = []

        for term in terms:
            if term in path_l:
                score += 8.0

            # Prefer exact phrase/symbol hits in file content. Cap per-term
            # contribution so one huge file with repeated text does not drown
            # out better path/name matches.
            hit_count = content_l.count(term)
            if hit_count:
                score += min(hit_count, 8) * (3.0 if any(ch in term for ch in "._-/#$@") else 1.5)

        if score <= 0:
            continue

        lines = content.splitlines()
        for idx, line in enumerate(lines):
            line_l = line.lower()
            line_score = sum(1 for term in terms if term in line_l)
            if line_score:
                line_hits.append({
                    "line_start": idx + 1,
                    "line_end": min(len(lines), idx + 2),
                    "score": float(line_score),
                })
            if len(line_hits) >= 5:
                break

        # Config and entrypoint files are commonly needed for dependency,
        # routing, and style requests, but should not always dominate.
        name = Path(path).name.lower()
        if name in {"package.json", "vite.config.ts", "vite.config.js", "next.config.js"}:
            score += 1.0
        if name in {"app.tsx", "page.tsx", "main.tsx", "index.tsx", "globals.css", "index.css"}:
            score += 1.5

        scored.append({
            "file_path": path,
            "score": round(score, 3),
            "relevant_lines": line_hits,
        })

    scored.sort(key=lambda item: item["score"], reverse=True)
    return scored[:top_k]


# Legacy V1 helpers stream_file_update and stream_chat removed.


# ── Core routes ────────────────────────────────────────────────────────────────

@app.get("/")
async def serve_app():
    index = WEB_DIR / "index.html"
    if index.exists():
        return FileResponse(str(index))
    return {"name": config.product_name, "version": config.version, "docs": "/docs"}


@app.get("/api/health")
async def health():
    return {"product": config.product_name, "version": config.version, "backend": await backend.health()}


# Legacy V1 chat, intent, and update endpoints removed.


async def stream_run(req: RunRequest):
    async for event in sandbox_runner.run(
        files=[f.model_dump() for f in req.files],
        run_command=req.run_command,
        env_vars=req.env_vars or None,
        setup_commands=req.setup_commands or None,
        run_timeout_s=req.run_timeout_s or 0,
        project_id=req.project_id or None,
    ):
        payload = {"text": event.get("text", ""), "code": event.get("code")}
        if "port" in event:
            payload["port"] = event["port"]
        yield sse_event(event["type"], payload)


@app.post("/api/run")
async def run_project(req: RunRequest):
    return StreamingResponse(stream_run(req), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/stop")
async def stop_project(project_id: Optional[str] = Query(default=None)):
    """
    Stop the running dev server for a specific project (or the legacy global
    process when no project_id is given).

    Pass ?project_id=<id> so only that project's process is killed — other
    projects keep running unaffected.
    """
    await sandbox_runner.stop(project_id)
    return {"stopped": True, "project_id": project_id}


@app.post("/api/run/update-files")
async def run_update_files(req: Request):
    """
    Hot-write new file contents into the running dev server's tmpdir.
    Vite's HMR file watcher picks up the changes automatically — no restart needed.
    Pass project_id in the body so we target the correct per-project process.
    """
    body  = await req.json()
    files = body.get("files", [])
    delete_paths = body.get("delete_paths", [])
    project_id   = body.get("project_id") or None
    if not files and not delete_paths:
        return {"updated": False, "reason": "no file changes provided"}
    updated = await sandbox_runner.update_files(files, delete_paths, project_id=project_id)
    return {"updated": updated, "file_count": len(files), "delete_count": len(delete_paths)}


@app.get("/api/run/status")
async def run_status():
    return {"running": sandbox_runner.is_running}


# ── Auth (V2) ─────────────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    email:    str
    username: str
    password: str

class LoginRequest(BaseModel):
    email:    str
    password: str

@app.post("/api/auth/register")
async def register(req: RegisterRequest, db: AsyncSession = Depends(get_db)):
    """Register a new user. Creates their workspace folder on disk."""
    # Check uniqueness
    if await crud.get_user_by_email(db, req.email):
        raise HTTPException(status_code=400, detail="Email already registered")
    if await crud.get_user_by_username(db, req.username):
        raise HTTPException(status_code=400, detail="Username already taken")

    user = await crud.create_user(
        db,
        email=req.email.lower().strip(),
        username=req.username.strip(),
        hashed_password=hash_password(req.password),
    )
    await db.commit()

    # Create user workspace folder
    user_home = Path(config.forge_data_root) / "users" / user.id / "projects"
    user_home.mkdir(parents=True, exist_ok=True)

    token = create_token(user.id, user.email)
    return {
        "token": token,
        "user": {"id": user.id, "email": user.email, "username": user.username},
    }


@app.post("/api/auth/login")
async def login(req: LoginRequest, db: AsyncSession = Depends(get_db)):
    """Login with email + password. Returns JWT."""
    user = await crud.get_user_by_email(db, req.email.lower().strip())
    if not user or not verify_password(req.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    token = create_token(user.id, user.email)
    return {
        "token": token,
        "user": {"id": user.id, "email": user.email, "username": user.username},
    }


@app.get("/api/auth/me")
async def me(current_user=Depends(get_current_user)):
    """Return current user info from JWT."""
    return {
        "id": current_user.id,
        "email": current_user.email,
        "username": current_user.username,
        "created_at": current_user.created_at.isoformat(),
    }


# ── Session (legacy — kept for backward compat) ────────────────────────────────

@app.post("/api/sessions")
async def get_or_create_session(session_id: str = Depends(get_session_id), db: AsyncSession = Depends(get_db)):
    user = await crud.get_or_create_user(db, session_id)
    return {"user_id": user.id, "created_at": user.created_at.isoformat()}


# ── Projects ───────────────────────────────────────────────────────────────────

@app.get("/api/projects")
async def list_projects(current_user=Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    projects = await crud.list_projects(db, current_user.id)
    return [
        {
            "id": p.id, "name": p.name, "description": p.description,
            "tech_stack": p.tech_stack, "current_version": p.current_version,
            "run_command": p.run_command,
            "updated_at": p.updated_at.isoformat() if p.updated_at else None,
            "created_at": p.created_at.isoformat() if p.created_at else None,
        }
        for p in projects
    ]


@app.post("/api/projects")
async def create_project(req: SaveProjectRequest, current_user=Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    files_data = [f.model_dump() for f in req.files]
    project = await crud.create_project(db, user_id=current_user.id, name=req.name, description=req.description,
        tech_stack=req.tech_stack, setup_commands=req.setup_commands, run_command=req.run_command, files=files_data)
    asyncio.create_task(_index_embeddings_bg(project.id, files_data))
    asyncio.create_task(_generate_context_bg(project.id, current_user.id, files_data))
    return {"id": project.id, "name": project.name, "current_version": project.current_version}


@app.get("/api/projects/{project_id}")
async def get_project(project_id: str, current_user=Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    project = await _get_or_migrate_project(db, project_id, current_user)
    return {
        "id": project.id, "name": project.name, "description": project.description,
        "tech_stack": project.tech_stack, "setup_commands": project.setup_commands,
        "run_command": project.run_command, "current_version": project.current_version,
        "context_summary": project.context_summary or "",
        "updated_at": project.updated_at.isoformat() if project.updated_at else None,
        "files": [{"path": f.path, "content": f.content, "language": f.language, "description": f.description} for f in project.files],
    }


@app.put("/api/projects/{project_id}/files")
async def update_project_files(project_id: str, req: UpdateProjectFilesRequest,
        current_user=Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    project = await _get_or_migrate_project(db, project_id, current_user)
    files_data = [f.model_dump() for f in req.files]
    project = await crud.update_project_files(db, project_id=project_id, user_id=current_user.id,
        files=files_data, instruction=req.instruction)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    asyncio.create_task(_index_embeddings_bg(project_id, files_data))
    asyncio.create_task(_generate_context_bg(project_id, current_user.id, files_data))
    return {"id": project.id, "current_version": project.current_version}


@app.patch("/api/projects/{project_id}/meta")
async def update_project_meta(project_id: str, req: UpdateProjectMetaRequest,
        current_user=Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Update project metadata (run_command, setup_commands) without touching files."""
    project = await crud.update_project_meta(
        db, project_id=project_id, user_id=current_user.id,
        run_command=req.run_command,
        setup_commands=req.setup_commands,
    )
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return {"id": project.id, "run_command": project.run_command, "setup_commands": project.setup_commands}


@app.delete("/api/projects/{project_id}")
async def delete_project(project_id: str, current_user=Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    project = await _get_or_migrate_project(db, project_id, current_user)
    if not await crud.delete_project(db, project_id, current_user.id):
        raise HTTPException(status_code=404, detail="Project not found")
    # Best-effort: clean up workspace directory
    workspace_manager.destroy(current_user.id, project_id)
    return {"deleted": True}


# ── Versions ───────────────────────────────────────────────────────────────────

@app.get("/api/projects/{project_id}/versions")
async def list_versions(project_id: str, current_user=Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    project = await _get_or_migrate_project(db, project_id, current_user)
    versions = await crud.list_versions(db, project_id, current_user.id)
    return [
        {"id": v.id, "version_number": v.version_number, "instruction": v.instruction,
         "created_at": v.created_at.isoformat(), "file_count": len(v.files_snapshot) if v.files_snapshot else 0}
        for v in versions
    ]


@app.post("/api/projects/{project_id}/restore")
async def restore_version(project_id: str, req: RestoreVersionRequest,
        current_user=Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    project = await _get_or_migrate_project(db, project_id, current_user)
    restored_project = await crud.restore_version(db, project_id, current_user.id, req.version_number)
    if not restored_project:
        raise HTTPException(status_code=404, detail="Project or version not found")
    restored = await crud.get_project(db, project_id, current_user.id)
    return {
        "id": project.id, "current_version": project.current_version,
        "files": [{"path": f.path, "content": f.content, "language": f.language, "description": f.description}
                  for f in (restored.files if restored else [])],
    }


# ── Context search ─────────────────────────────────────────────────────────────

@app.post("/api/projects/{project_id}/context-search")
async def context_search(project_id: str, req: ContextSearchRequest,
        current_user=Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    t0 = time.perf_counter()
    print(f"\n[CTX-SEARCH] query={req.instruction[:80]!r}  project={project_id[:8]}", flush=True)

    project = await _get_or_migrate_project(db, project_id, current_user)

    t1 = time.perf_counter()
    print(f"  [+{(time.perf_counter()-t0)*1000:.0f}ms] project loaded — {len(project.files)} files", flush=True)

    chunks = await crud.get_embeddings(db, project_id)
    print(f"  [+{(time.perf_counter()-t0)*1000:.0f}ms] embeddings loaded — {len(chunks)} chunks", flush=True)

    if not chunks:
        results = _lexical_context_search(req.instruction, project.files, top_k=req.top_k or 4)
        if results:
            print(
                f"  [!] no embeddings indexed → lexical fallback selected {len(results)} file(s):\n"
                + "\n".join(f"      {r['file_path']}  score={r['score']:.3f}" for r in results),
                flush=True,
            )
            return {"relevant_files": [r["file_path"] for r in results], "details": results, "fallback": True}

        # Last resort for vague instructions where lexical search has no hooks.
        # Keep it bounded so we do not regress to shipping every file.
        fallback_files = [
            f.path for f in project.files
            if Path(f.path).name.lower() in {
                "package.json", "app.tsx", "page.tsx", "main.tsx", "index.tsx",
                "globals.css", "index.css",
            }
        ][:req.top_k or 4]
        if not fallback_files:
            fallback_files = [f.path for f in project.files[:req.top_k or 4]]
        print(f"  [!] no search hits → bounded fallback: {len(fallback_files)} file(s)", flush=True)
        return {"relevant_files": fallback_files, "details": [], "fallback": True}

    t2 = time.perf_counter()
    query_emb = embed_query(req.instruction)
    print(f"  [+{(time.perf_counter()-t0)*1000:.0f}ms] query embedded ({time.perf_counter()-t2:.2f}s)", flush=True)

    t3 = time.perf_counter()
    results = search_context(query_emb, chunks, top_k_files=req.top_k or 4)
    print(
        f"  [+{(time.perf_counter()-t0)*1000:.0f}ms] search done ({time.perf_counter()-t3:.3f}s)\n"
        f"  → selected {len(results)} file(s):\n"
        + "\n".join(f"      {r['file_path']}  score={r['score']:.3f}" for r in results),
        flush=True,
    )
    print(f"  [TOTAL] context-search: {(time.perf_counter()-t0)*1000:.0f}ms\n", flush=True)

    return {"relevant_files": [r["file_path"] for r in results], "details": results, "fallback": False}


# ── Conversations ──────────────────────────────────────────────────────────────

@app.get("/api/conversations")
async def list_conversations(project_id: Optional[str] = Query(default=None),
        current_user=Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    convos = await crud.list_conversations(db, current_user.id, project_id)
    return [{"id": c.id, "title": c.title, "project_id": c.project_id,
             "updated_at": c.updated_at.isoformat() if c.updated_at else None} for c in convos]


@app.post("/api/conversations")
async def create_conversation(req: CreateConversationRequest,
        current_user=Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    convo = await crud.create_conversation(db, current_user.id, req.title, req.project_id)
    return {"id": convo.id, "title": convo.title, "project_id": convo.project_id}


@app.get("/api/conversations/{convo_id}")
async def get_conversation(convo_id: str, current_user=Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    convo = await crud.get_conversation_with_messages(db, convo_id, current_user.id)
    if not convo:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {
        "id": convo.id, "title": convo.title, "project_id": convo.project_id,
        "messages": [{"id": m.id, "role": m.role, "content": m.content,
                      "created_at": m.created_at.isoformat()} for m in convo.messages],
    }


@app.post("/api/conversations/{convo_id}/messages")
async def add_message(convo_id: str, req: AddMessageRequest,
        current_user=Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    convo = await crud.get_conversation_with_messages(db, convo_id, current_user.id)
    if not convo:
        raise HTTPException(status_code=404, detail="Conversation not found")
    msg = await crud.add_message(db, convo_id, req.role, req.content)
    return {"id": msg.id, "role": msg.role}


@app.delete("/api/conversations/{convo_id}")
async def delete_conversation(convo_id: str, current_user=Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    if not await crud.delete_conversation(db, convo_id, current_user.id):
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {"deleted": True}


# ── Background helpers ─────────────────────────────────────────────────────────

async def _index_embeddings_bg(project_id: str, files: list):
    """Index project files in the background — does not block the API response."""
    from forge.db.database import AsyncSessionLocal
    try:
        chunks = build_index(files)
        if chunks:
            async with AsyncSessionLocal() as session:
                await crud.save_embeddings(session, project_id, chunks)
                await session.commit()
            print(f"[Embeddings] Indexed {len(chunks)} chunks for {project_id}", flush=True)
    except Exception as e:
        print(f"[Embeddings] Indexing failed for {project_id}: {e}", flush=True)


async def _generate_context_bg(project_id: str, user_id: str, files: list):
    """
    Generate and store an AI project context summary in the background.
    Runs after every build/update — non-blocking, best-effort.
    """
    from forge.db.database import AsyncSessionLocal
    try:
        summary = await backend.generate_context_summary(files)
        if summary:
            async with AsyncSessionLocal() as session:
                await crud.update_project_context(session, project_id, user_id, summary)
                await session.commit()
            print(f"[Context] Stored context summary for {project_id[:8]} ({len(summary)} chars)", flush=True)
    except Exception as e:
        print(f"[Context] Summary failed for {project_id}: {e}", flush=True)


# ── Project context endpoint ───────────────────────────────────────────────────

@app.get("/api/projects/{project_id}/context")
async def get_project_context(project_id: str,
        current_user=Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Return the stored AI context summary for a project."""
    project = await _get_or_migrate_project(db, project_id, current_user)
    summary = await crud.get_project_context(db, project_id, current_user.id)
    return {"context_summary": summary, "has_context": bool(summary)}


@app.post("/api/projects/{project_id}/context")
async def regenerate_project_context(project_id: str,
        current_user=Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """
    Trigger on-demand context regeneration (e.g. for projects without a context yet).
    Returns immediately; generation runs in the background.
    """
    project = await _get_or_migrate_project(db, project_id, current_user)
    files_data = [
        {"path": f.path, "content": f.content, "language": f.language, "description": f.description}
        for f in project.files
    ]
    asyncio.create_task(_generate_context_bg(project_id, current_user.id, files_data))
    return {"status": "generating", "file_count": len(files_data)}


# ── Workspaces ─────────────────────────────────────────────────────────────────

@app.get("/api/workspaces/{project_id}")
async def get_workspace(project_id: str, current_user=Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Return workspace info (path, stack, file count) for a project."""
    project = await _get_or_migrate_project(db, project_id, current_user)
    ws_path = workspace_manager.path(current_user.id, project_id)
    if not ws_path.exists():
        workspace_manager.create(current_user.id, project_id)
    
    file_count = len(workspace_manager.list_files(current_user.id, project_id))
    size = workspace_manager.size_kb(current_user.id, project_id)
    return {
        "project_id": project_id,
        "exists": True,
        "path": str(ws_path),
        "file_count": file_count,
        "size_kb": size,
    }


@app.post("/api/workspaces/{project_id}/sync")
async def sync_workspace(project_id: str, current_user=Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """
    Write the current project files from DB into the workspace directory.
    Call this before running the agent or a process when workspace files may be stale.
    """
    project = await _get_or_migrate_project(db, project_id, current_user)
    files_data = [
        {"path": f.path, "content": f.content}
        for f in project.files
    ]
    workspace_manager.create(current_user.id, project_id)
    workspace_manager.write_files(current_user.id, project_id, files_data)
    return {"synced": True, "file_count": len(files_data)}


@app.get("/api/workspaces/{project_id}/file")
async def get_single_workspace_file(
    project_id: str,
    path: str,
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Read a single file from the workspace by relative path.
    Called by the frontend on each file.edited event to show files in real-time.
    The `path` query param may be relative ("src/app/page.tsx") or an absolute
    path on the host — we normalise to relative automatically.
    """
    project = await _get_or_migrate_project(db, project_id, current_user)
    workspace = workspace_manager.path(current_user.id, project_id)

    # Normalise: strip workspace prefix if the caller sent an absolute path
    abs_path = Path(path)
    try:
        w_resolved = workspace.resolve()
        a_resolved = abs_path.resolve()
        if a_resolved.is_absolute():
            if str(a_resolved).lower().startswith(str(w_resolved).lower()):
                rel_str = str(a_resolved)[len(str(w_resolved)):].lstrip("/")
                rel_path = Path(rel_str)
            else:
                rel_path = a_resolved.relative_to(w_resolved)
        else:
            rel_path = abs_path
    except Exception:
        path_str = str(path)
        if "/workspace/" in path_str:
            rel_path = Path(path_str.split("/workspace/", 1)[1])
        else:
            rel_path = Path(path)

    file_path = (workspace / rel_path).resolve()

    # Security: must stay inside the workspace
    try:
        file_path.relative_to(workspace.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="Path outside workspace")

    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Cannot read file: {e}")

    rel_str  = str(file_path.relative_to(workspace.resolve()))
    lang     = file_path.suffix.lstrip(".") or "text"
    return {"path": rel_str, "content": content, "language": lang}


@app.get("/api/workspaces/{project_id}/files")
async def get_workspace_files(project_id: str, current_user=Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """
    Read all non-ignored files from the workspace directory and return them as JSON.
    Called by the frontend after the agent fixes files, to sync changes back into the UI.
    """
    project = await _get_or_migrate_project(db, project_id, current_user)
    workspace = workspace_manager.path(current_user.id, project_id)
    if not workspace.exists():
        raise HTTPException(status_code=404, detail="Workspace not found")

    IGNORE = {"node_modules", ".next", ".git", "__pycache__", ".venv", "dist", "build", ".cache"}
    files = []
    for f in workspace.rglob("*"):
        if not f.is_file():
            continue
        # Skip ignored dirs anywhere in the path
        if any(part in IGNORE or part.startswith(".") for part in f.relative_to(workspace).parts[:-1]):
            continue
        try:
            content = f.read_text(encoding="utf-8", errors="replace")
            rel = str(f.relative_to(workspace))
            lang = f.suffix.lstrip(".") or "text"
            files.append({"path": rel, "content": content, "language": lang})
        except Exception:
            pass
    return {"files": files}


@app.delete("/api/workspaces/{project_id}")
async def delete_workspace(project_id: str, current_user=Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Destroy the workspace directory (called when a project is deleted)."""
    project = await _get_or_migrate_project(db, project_id, current_user)
    workspace_manager.destroy(current_user.id, project_id)
    return {"destroyed": True}


# ── Agent endpoints ────────────────────────────────────────────────────────────

class AgentRunRequest(BaseModel):
    project_id: str
    task:       str
    stack:      Optional[dict] = None


async def _stream_agent(project_id: str, task: str, stack: Optional[dict]) -> AsyncIterator[str]:
    """
    Convert run_agent() AgentEvent dicts into SSE strings.
    Each event is: `event: <type>\\ndata: <json>\\n\\n`
    """
    try:
        async for event in run_agent(project_id=project_id, task=task, stack=stack):
            event_type = event.get("type", "status")
            yield f"event: {event_type}\ndata: {json.dumps(event)}\n\n"
            if event_type in ("done", "error"):
                return
    except Exception as e:
        yield f"event: error\ndata: {json.dumps({'type': 'error', 'text': str(e)})}\n\n"


@app.post("/api/agent/run")
async def agent_run(req: AgentRunRequest):
    """Start an AI agent for a project task. Streams AgentEvent objects via SSE."""
    print(
        f"\n{'='*60}\n"
        f"[AGENT] project_id={req.project_id}\n"
        f"  task : {req.task[:120]!r}\n"
        f"  stack: {req.stack}\n"
        f"{'='*60}",
        flush=True,
    )
    return StreamingResponse(
        _stream_agent(req.project_id, req.task, req.stack),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/agent/stop/{project_id}")
async def agent_stop(project_id: str):
    """Signal the running agent for a project to stop."""
    abort_agent(project_id)
    return {"ok": True, "project_id": project_id}


# ── Process management endpoints ───────────────────────────────────────────────

class StartProcessRequest(BaseModel):
    name:    str
    command: str
    port:    int
    env:     Optional[dict] = None


@app.get("/api/projects/{project_id}/processes")
async def list_processes(project_id: str):
    """Return all running processes for a project."""
    procs = orchestrator.list_processes(project_id)
    return {"processes": [p.to_dict() for p in procs]}


@app.post("/api/projects/{project_id}/processes")
async def start_process(project_id: str, req: StartProcessRequest):
    """Start a named process for a project."""
    info = await orchestrator.start(
        project_id=project_id,
        name=req.name,
        command=req.command,
        port=req.port,
        env=req.env,
    )
    return info.to_dict()


@app.delete("/api/projects/{project_id}/processes/{name}")
async def stop_process_endpoint(project_id: str, name: str):
    """Stop a named process for a project."""
    info = await orchestrator.stop(project_id, name)
    if info is None:
        raise HTTPException(status_code=404, detail=f"Process '{name}' not found")
    return info.to_dict()


@app.delete("/api/projects/{project_id}/processes")
async def stop_all_processes(project_id: str):
    """Stop all processes for a project."""
    infos = await orchestrator.stop_all(project_id)
    return {"stopped": [i.to_dict() for i in infos]}


@app.get("/api/projects/{project_id}/processes/{name}/logs")
async def get_process_logs(project_id: str, name: str, last_n: int = 100):
    """Return the last N log lines for a named process."""
    lines = orchestrator.get_logs(project_id, name, last_n=last_n)
    return {"name": name, "lines": lines}


# ── Port registry diagnostics ─────────────────────────────────────────────────

@app.get("/api/ports/stats")
async def port_stats():
    """
    Return port registry summary — how many projects have ports allocated
    and how full the registry is.  Useful for capacity monitoring.
    """
    total = port_registry.total_allocated()
    from forge.runner.port_registry import MAX_BLOCKS
    return {
        "allocated":  total,
        "capacity":   MAX_BLOCKS,
        "free":       MAX_BLOCKS - total,
        "usage_pct":  round(total / MAX_BLOCKS * 100, 2),
        "port_range": "10000–59999",
        "block_size": 5,
    }


@app.get("/api/ports/project/{project_id}")
async def project_ports(project_id: str):
    """Return the allocated port block for a project_id."""
    ports = port_registry.get(project_id)
    if ports is None:
        raise HTTPException(status_code=404, detail="No port allocation found for this project")
    return {"project_id": project_id, "ports": ports}


# ── Preview proxy ──────────────────────────────────────────────────────────────

@app.get("/api/preview/{project_id}/status")
async def preview_status(project_id: str):
    """
    Return the running status and allocated port for a project's frontend.
    Useful for the frontend to decide whether to show the proxy iframe or the
    "press Run" placeholder — without making a full proxy request first.

    Response:
      { "running": bool, "fe_port": int, "source": "orchestrator"|"sandbox"|"registry"|"none" }
    """
    from forge.proxy.preview import _resolve_fe_port, _is_project_process_running
    fe_port = _resolve_fe_port(project_id)
    running = _is_project_process_running(project_id)

    # Determine which source provided the port
    source = "none"
    if running:
        try:
            from forge.runner.orchestrator import orchestrator
            proc = orchestrator.get_process(project_id, "frontend")
            if proc and proc.status in ("running", "starting"):
                source = "orchestrator"
        except Exception:
            pass
        if source == "none":
            try:
                from forge.runner.sandbox import runner as _sr
                if _sr.is_project_running(project_id):
                    source = "sandbox"
            except Exception:
                pass
    elif fe_port > 0:
        source = "registry"

    return {
        "project_id": project_id,
        "running":    running,
        "fe_port":    fe_port,
        "source":     source,
    }


@app.api_route("/api/preview/{project_id}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def preview_proxy(project_id: str, path: str, request: Request):
    """
    Reverse proxy: forwards requests to the project's running frontend dev server.
    The iframe in PreviewPanel points to this endpoint instead of using srcdoc.

    Usage: <iframe src="/api/preview/{project_id}/" />

    The internal port is NEVER exposed to the browser — only the project_id-based
    URL is visible externally.  Internally, port is resolved via the port registry.
    """
    return await proxy_preview(project_id, path, request)


async def _get_or_migrate_project(db: AsyncSession, project_id: str, current_user):
    project = await crud.get_project_global(db, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    if project.user_id != current_user.id:
        owner = await crud.get_user_by_id(db, project.user_id)
        if owner and owner.session_id:
            # Physically move the project workspace directory on disk first
            old_project_dir = workspace_manager.user_home(owner.id) / project_id
            new_project_dir = workspace_manager.user_home(current_user.id) / project_id
            if old_project_dir.exists() and not new_project_dir.exists():
                import shutil
                new_project_dir.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.move(str(old_project_dir), str(new_project_dir))
                    print(f"[Migration] Moved project workspace dir from {old_project_dir} to {new_project_dir}", flush=True)
                except Exception as e:
                    print(f"[Migration] WARNING: Failed to move workspace dir: {e}", flush=True)

            # Update DB owner
            project.user_id = current_user.id
            await db.flush()
            await db.commit()
            print(f"[Migration] Migrated project {project_id} database owner from mock user {owner.id} to registered user {current_user.id}", flush=True)
        else:
            raise HTTPException(status_code=403, detail="Not authorized to access this project")

    return project


# ── Showcase / gallery ─────────────────────────────────────────────────────────

def _showcase_payload(p) -> dict:
    """Serialize a Project into the ShowcaseProject shape the FE expects."""
    stack_val = None
    try:
        s = p.stack
        if isinstance(s, dict):
            stack_val = s.get("fe") or s.get("be") or None
        elif isinstance(s, str) and s not in ("{}", ""):
            import json as _json
            d = _json.loads(s)
            stack_val = d.get("fe") or d.get("be") or None
    except Exception:
        pass
    return {
        "id":                   p.id,
        "name":                 p.name,
        "description":          p.description or "",
        "workspace_path":       p.workspace_path or "",
        "stack":                stack_val,
        "container_status":     p.container_status or "stopped",
        "preview_url":          p.preview_url,
        "showcased_at":         p.showcased_at.isoformat() if p.showcased_at else None,
        "showcase_name":        p.showcase_name,
        "showcase_description": p.showcase_description,
        "thumbnail_url":        p.thumbnail_url,
    }


class ShowcaseRequest(BaseModel):
    showcase_name:        Optional[str] = None
    showcase_description: Optional[str] = None


@app.post("/api/projects/{project_id}/showcase")
async def set_showcase(
    project_id: str,
    req: ShowcaseRequest,
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Mark a project as publicly showcased."""
    project = await crud.showcase_project(
        db, project_id, current_user.id,
        showcase_name=req.showcase_name,
        showcase_description=req.showcase_description,
    )
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    await db.commit()
    return _showcase_payload(project)


@app.delete("/api/projects/{project_id}/showcase")
async def remove_showcase(
    project_id: str,
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Remove a project from the public showcase."""
    ok = await crud.unshowcase_project(db, project_id, current_user.id)
    if not ok:
        raise HTTPException(status_code=404, detail="Project not found")
    await db.commit()
    return {"unshowcased": True}


@app.get("/api/projects/showcase")
async def list_my_showcases(
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return all projects the current user has showcased."""
    projects = await crud.list_user_showcases(db, current_user.id)
    return [_showcase_payload(p) for p in projects]


@app.get("/api/projects/gallery")
async def public_gallery(db: AsyncSession = Depends(get_db)):
    """Public gallery — all showcased projects from all users. No auth required."""
    projects = await crud.list_all_showcases(db)
    return [_showcase_payload(p) for p in projects]


class ThumbnailRequest(BaseModel):
    image_data: str  # data:image/jpeg;base64,...  or data:image/png;base64,...


@app.post("/api/projects/{project_id}/thumbnail")
async def upload_thumbnail(
    project_id: str,
    req: ThumbnailRequest,
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Accept a base64 screenshot from the frontend and save it as a JPEG.
    Returns { thumbnail_url: "/api/thumbnails/{project_id}.jpg" }
    """
    import base64 as _b64

    raw = req.image_data
    # Strip data URI prefix if present
    if "," in raw:
        raw = raw.split(",", 1)[1]

    try:
        img_bytes = _b64.b64decode(raw)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid base64 image data")

    thumb_path = THUMBNAILS_DIR / f"{project_id}.jpg"
    thumb_path.write_bytes(img_bytes)

    thumb_url = f"/api/thumbnails/{project_id}.jpg"
    ok = await crud.update_project_thumbnail(db, project_id, current_user.id, thumb_url)
    if not ok:
        raise HTTPException(status_code=404, detail="Project not found")
    await db.commit()
    return {"thumbnail_url": thumb_url}


@app.get("/api/projects/{project_id}/screenshot")
async def take_screenshot(
    project_id: str,
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Use a headless Chromium (Playwright) to screenshot the running project container
    and save it as the project thumbnail.  Requires: pip install playwright &&
    playwright install chromium --with-deps
    Returns { thumbnail_url: "/api/thumbnails/{project_id}.jpg" }
    """
    # Derive the preview URL directly — docker_manager computes it from the project_id
    # (no DB lookup needed; thumbnail save below enforces ownership)
    from forge.runner.docker_manager import docker_manager as _dm
    target_url = _dm.preview_url(project_id)

    try:
        from playwright.async_api import async_playwright  # lazy import
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-setuid-sandbox"])
            page = await browser.new_page(viewport={"width": 1280, "height": 800})
            try:
                await page.goto(target_url, wait_until="networkidle", timeout=12000)
            except Exception:
                # If networkidle times out, try domcontentloaded as fallback
                await page.goto(target_url, wait_until="domcontentloaded", timeout=8000)
            img_bytes = await page.screenshot(type="jpeg", quality=80, full_page=False)
            await browser.close()
    except ImportError:
        raise HTTPException(
            status_code=503,
            detail="Playwright is not installed. Run: pip install playwright && playwright install chromium --with-deps",
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Screenshot failed: {exc}")

    thumb_path = THUMBNAILS_DIR / f"{project_id}.jpg"
    thumb_path.write_bytes(img_bytes)
    thumb_url = f"/api/thumbnails/{project_id}.jpg"
    await crud.update_project_thumbnail(db, project_id, current_user.id, thumb_url)
    await db.commit()
    return {"thumbnail_url": thumb_url}


class CloneRequest(BaseModel):
    name:        Optional[str] = None
    description: Optional[str] = None


@app.post("/api/projects/{project_id}/clone")
async def clone_project(
    project_id: str,
    req: CloneRequest,
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Deep-copy a project (files + metadata) into a new project owned by the
    current user. The workspace directory is also copied on disk so the
    new project is immediately ready to run / edit.
    """
    import shutil

    # Anyone can clone a showcased project; also allow cloning your own
    source = await crud.get_project_global(db, project_id)
    if not source:
        raise HTTPException(status_code=404, detail="Source project not found")
    if not source.showcased_at and source.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Project is not public")

    new_project = await crud.clone_project(
        db,
        source_project_id=project_id,
        new_user_id=current_user.id,
        name=req.name,
        description=req.description,
    )
    if not new_project:
        raise HTTPException(status_code=500, detail="Clone failed")

    # Copy workspace directory on disk
    src_ws = workspace_manager.path(source.user_id, project_id)
    dst_ws = workspace_manager.path(current_user.id, new_project.id)
    if src_ws.exists():
        try:
            shutil.copytree(str(src_ws), str(dst_ws))
        except Exception as e:
            print(f"[Clone] workspace copy failed: {e}", flush=True)
            # Non-fatal — the new project still has DB files
    else:
        # No workspace to copy; just create an empty one from DB files
        workspace_manager.create(current_user.id, new_project.id)
        files_data = [
            {"path": f.path, "content": f.content}
            for f in (source.files or [])
        ]
        if files_data:
            workspace_manager.write_files(current_user.id, new_project.id, files_data)

    # Set workspace_path on the new project
    await crud.update_project_workspace(db, new_project.id, str(dst_ws))
    await db.commit()

    # Reload to pick up workspace_path
    new_project.workspace_path = str(dst_ws)
    return _showcase_payload(new_project)


# ── Chat message ───────────────────────────────────────────────────────────────

class ChatMessageRequest(BaseModel):
    message: str
    model_id: Optional[str] = None
    provider_id: Optional[str] = None


@app.post("/api/projects/{project_id}/chat")
async def project_chat(
    project_id: str,
    req: ChatMessageRequest,
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Send a message to OpenCode for this project.
    Creates an OpenCode session on first call, reuses it thereafter.
    Subscribe to GET /api/projects/{id}/events to see the agent's response stream.
    """
    from forge.agent.opencode_client import opencode_client

    project = await _get_or_migrate_project(db, project_id, current_user)

    # Ensure workspace exists
    ws_path = workspace_manager.path(current_user.id, project_id)
    ws_path.mkdir(parents=True, exist_ok=True)

    # Create OpenCode session if needed
    if not project.opencode_session_id:
        session_id = await opencode_client.create_session(str(ws_path))
        await crud.update_project_opencode_session(db, project_id, session_id)
        await db.commit()
        project.opencode_session_id = session_id

    # Send the message — agent starts working immediately
    await opencode_client.send_message(
        session_id=project.opencode_session_id,
        workspace_path=str(ws_path),
        text=req.message,
        model_id=req.model_id,
        provider_id=req.provider_id,
    )

    return {"status": "sent", "session_id": project.opencode_session_id}


@app.get("/api/projects/{project_id}/events")
async def project_events(
    project_id: str,
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    SSE stream of all OpenCode events for this project.

    The FE subscribes to this immediately after sending a chat message.
    It receives every event OpenCode emits — reasoning, tool calls, file patches,
    text responses — translated into clean Forge event types.

    Event types emitted:
      message.part  — reasoning | text | tool_call | tool_result | patch | step_finish
      message.done  — one agent turn completed
      session.done  — full session complete (agent stopped)
      error         — something went wrong

    Connect with EventSource:
      const es = new EventSource(`/api/projects/${id}/events`, { headers: { Authorization: `Bearer ${token}` } })
      es.addEventListener('message.part', e => handlePart(JSON.parse(e.data)))
    """
    from forge.agent.event_bridge import bridge_events

    project = await _get_or_migrate_project(db, project_id, current_user)

    ws_path = str(workspace_manager.path(current_user.id, project_id))

    async def event_stream():
        try:
            async for translated in bridge_events(ws_path, project.opencode_session_id):
                event_name = translated["event"]
                data = json.dumps(translated["data"])
                yield f"event: {event_name}\ndata: {data}\n\n"
        except Exception as e:
            yield f"event: error\ndata: {json.dumps({'message': str(e)})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # Disable Nginx buffering
        },
    )


# ── V2: Docker deploy ─────────────────────────────────────────────────────────

def _generate_dockerfile(ws_path: str) -> str:
    """
    Auto-generate an appropriate Dockerfile for the project based on its files.
    Called when the workspace has no Dockerfile (e.g. OpenCode didn't write one).

    Detects:
      - Next.js  → multi-stage Node build, `next start` on PORT 8000
      - Vite/React CRA → multi-stage Node build + `serve` static dist on 8000
      - Generic Node → single-stage, `node server.js` or npm start
    """
    import json as _json

    ws = Path(ws_path)
    pkg_path = ws / "package.json"

    if not pkg_path.exists():
        # Pure static file / HTML project (no package.json)
        return (
            "FROM node:20-alpine\n"
            "RUN npm install -g serve\n"
            "WORKDIR /app\n"
            "COPY . .\n"
            "EXPOSE 8000\n"
            'CMD ["serve", "-l", "8000"]\n'
        )

    is_next = (
        (ws / "next.config.ts").exists()
        or (ws / "next.config.js").exists()
        or (ws / "next.config.mjs").exists()
    )

    has_vite = False
    has_react_scripts = False
    if pkg_path.exists():
        try:
            pkg = _json.loads(pkg_path.read_text())
            deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
            is_next = is_next or "next" in deps
            has_vite = "vite" in deps
            has_react_scripts = "react-scripts" in deps
        except Exception:
            pass

    if is_next:
        # Next.js: build then run with next start, respects PORT env var
        return (
            "FROM node:20-alpine AS deps\n"
            "WORKDIR /app\n"
            "COPY package*.json ./\n"
            "RUN npm ci\n"
            "\n"
            "FROM node:20-alpine AS builder\n"
            "WORKDIR /app\n"
            "COPY --from=deps /app/node_modules ./node_modules\n"
            "COPY . .\n"
            "ENV NEXT_TELEMETRY_DISABLED=1\n"
            "ENV NODE_ENV=production\n"
            "RUN npm run build\n"
            "\n"
            "FROM node:20-alpine AS runner\n"
            "WORKDIR /app\n"
            "ENV NODE_ENV=production\n"
            "ENV NEXT_TELEMETRY_DISABLED=1\n"
            "ENV PORT=8000\n"
            "COPY --from=builder /app/public ./public\n"
            "COPY --from=builder /app/.next ./.next\n"
            "COPY --from=builder /app/node_modules ./node_modules\n"
            "COPY --from=builder /app/package.json ./package.json\n"
            "EXPOSE 8000\n"
            'CMD ["npm", "start"]\n'
        )

    if has_vite or has_react_scripts:
        # Vite / CRA: build static assets, serve with `serve`
        dist_dir = "dist" if has_vite else "build"
        return (
            "FROM node:20-alpine AS builder\n"
            "WORKDIR /app\n"
            "COPY package*.json ./\n"
            "RUN npm ci\n"
            "COPY . .\n"
            f"ENV VITE_PORT=8000\n"
            "RUN npm run build\n"
            "\n"
            "FROM node:20-alpine AS runner\n"
            "RUN npm install -g serve\n"
            "WORKDIR /app\n"
            f"COPY --from=builder /app/{dist_dir} ./{dist_dir}\n"
            "EXPOSE 8000\n"
            f'CMD ["serve", "-s", "{dist_dir}", "-l", "8000"]\n'
        )

    # Generic Node.js fallback
    return (
        "FROM node:20-alpine\n"
        "WORKDIR /app\n"
        "COPY package*.json ./\n"
        "RUN npm ci --only=production 2>/dev/null || npm install --omit=dev 2>/dev/null || npm install\n"
        "COPY . .\n"
        "ENV PORT=8000\n"
        "EXPOSE 8000\n"
        'CMD ["npm", "start"]\n'
    )


@app.post("/api/projects/{project_id}/deploy")
async def deploy_project(
    project_id: str,
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Build and run the project as a Docker container.
    Returns an SSE stream of build progress + container startup.

    The workspace must have a Dockerfile (generated by OpenCode agent).

    Events:
      status  — {"text": "Building Docker image..."}
      log     — {"text": "Step 1/8 : FROM node:20-alpine"}
      started — {"container_id": "abc", "preview_url": "https://..."}
      done    — {"preview_url": "https://..."}
      error   — {"message": "Build failed: ..."}
    """
    from forge.runner.docker_manager import docker_manager

    project = await _get_or_migrate_project(db, project_id, current_user)

    ws_path = str(workspace_manager.path(current_user.id, project_id))
    dockerfile = Path(ws_path) / "Dockerfile"

    # Auto-generate a Dockerfile if the workspace doesn't have one.
    # OpenCode generates the app code but not always a Dockerfile.
    if not dockerfile.exists():
        generated = _generate_dockerfile(ws_path)
        dockerfile.write_text(generated)

    preview_url = docker_manager.preview_url(project_id)

    async def deploy_stream():
        try:
            # Guard: fail fast if Docker isn't running rather than crashing mid-stream
            if not docker_manager._docker_available():
                yield f"event: error\ndata: {json.dumps({'message': 'Docker is not running. Start Docker Desktop and try again.'})}\n\n"
                return

            # Mark as building
            await crud.update_project_container(db, project_id, None, "building", preview_url)
            await db.commit()
            yield f"event: status\ndata: {json.dumps({'text': 'Building Docker image...'})}\n\n"

            # Stream build logs
            async for line in docker_manager.build(project_id, ws_path):
                yield f"event: log\ndata: {json.dumps({'text': line})}\n\n"

            yield f"event: status\ndata: {json.dumps({'text': 'Starting container...'})}\n\n"

            # Run container
            container_id = await docker_manager.run(project_id, current_user.id)

            # Save to DB
            await crud.update_project_container(db, project_id, container_id, "running", preview_url)
            await db.commit()

            yield f"event: started\ndata: {json.dumps({'container_id': container_id, 'preview_url': preview_url})}\n\n"
            yield f"event: done\ndata: {json.dumps({'preview_url': preview_url})}\n\n"

        except Exception as e:
            await crud.update_project_container(db, project_id, None, "error")
            await db.commit()
            yield f"event: error\ndata: {json.dumps({'message': str(e)})}\n\n"

    return StreamingResponse(
        deploy_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.delete("/api/projects/{project_id}/deploy")
async def stop_project(
    project_id: str,
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Stop the running project container."""
    from forge.runner.docker_manager import docker_manager

    project = await _get_or_migrate_project(db, project_id, current_user)

    await docker_manager.stop(project_id)
    await crud.update_project_container(db, project_id, None, "stopped")
    await db.commit()
    return {"status": "stopped"}


@app.get("/api/projects/{project_id}/deploy/status")
async def deploy_status(
    project_id: str,
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get current container status and preview URL."""
    from forge.runner.docker_manager import docker_manager

    project = await _get_or_migrate_project(db, project_id, current_user)

    live_status = docker_manager.status(project_id)
    return {
        "status": live_status,
        "container_id": project.container_id,
        "preview_url": project.preview_url,
    }


@app.get("/api/projects/{project_id}/deploy/logs")
async def deploy_logs(
    project_id: str,
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """SSE stream of live container logs."""
    from forge.runner.docker_manager import docker_manager

    project = await _get_or_migrate_project(db, project_id, current_user)

    async def log_stream():
        async for line in docker_manager.stream_logs(project_id):
            yield f"event: log\ndata: {json.dumps({'text': line})}\n\n"

    return StreamingResponse(
        log_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── V2: Export ────────────────────────────────────────────────────────────────

@app.get("/api/projects/{project_id}/export")
async def export_project(
    project_id: str,
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Download the project workspace as a ZIP file.
    Includes Dockerfile, docker-compose.yml, and all source files.
    Users can unzip and run `docker compose up` anywhere.
    """
    import io
    import zipfile

    project = await _get_or_migrate_project(db, project_id, current_user)

    ws = workspace_manager.path(current_user.id, project_id)
    if not ws.exists():
        raise HTTPException(status_code=404, detail="Workspace not found")

    # Build ZIP in memory
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for file_path in ws.rglob("*"):
            if file_path.is_file() and ".opencode" not in file_path.parts:
                arc_name = file_path.relative_to(ws)
                zf.write(file_path, arc_name)
    buffer.seek(0)

    safe_name = project.name.replace(" ", "_").lower()
    from fastapi.responses import Response
    return Response(
        content=buffer.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}.zip"'},
    )


# ── V2: AI proxy endpoints (provider + config via OpenCode) ──────────────────
# These proxy to OpenCode's provider/config API so the FE settings UI can
# configure models and API keys without making direct calls to OpenCode.
# In FORGE_MODE=dev, users can set any provider. In prod, these are hidden.

@app.get("/api/ai/mode")
async def ai_mode():
    """Return the forge mode (dev|prod) so the FE shows/hides the settings UI."""
    return {"mode": config.forge_mode}


@app.get("/api/ai/providers")
async def ai_providers(current_user=Depends(get_current_user)):
    """
    List all available AI providers from OpenCode.
    Returns providers with their models, auth status, and connection state.
    In prod mode, this returns an empty list (platform handles AI keys).
    """
    if config.forge_mode == "prod":
        return {"providers": [], "mode": "prod"}

    import httpx
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{config.opencode_url}/provider",
                params={"directory": str(workspace_manager.path(current_user.id, "global"))},
            )
            r.raise_for_status()
            return r.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"OpenCode unreachable: {e}")


@app.get("/api/ai/config")
async def ai_config_get(current_user=Depends(get_current_user)):
    """
    Get the current OpenCode configuration for this workspace.
    Returns provider settings, model defaults, etc.
    Only available in FORGE_MODE=dev.
    """
    if config.forge_mode == "prod":
        raise HTTPException(status_code=403, detail="Config management not available in prod mode")

    import httpx
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{config.opencode_url}/config")
            r.raise_for_status()
            return r.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"OpenCode unreachable: {e}")


@app.put("/api/ai/config")
async def ai_config_update(
    req: dict,
    current_user=Depends(get_current_user),
):
    """
    Update OpenCode configuration (API keys, model defaults, provider settings).
    Only available in FORGE_MODE=dev.
    """
    if config.forge_mode == "prod":
        raise HTTPException(status_code=403, detail="Config management not available in prod mode")

    import httpx
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            # OpenCode uses PATCH /config for partial updates
            r = await client.patch(
                f"{config.opencode_url}/config",
                json=req,
            )
            r.raise_for_status()
            return r.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"OpenCode unreachable: {e}")


class SetModelRequest(BaseModel):
    provider_id: str
    model_id:    str


@app.post("/api/ai/model")
async def ai_set_model(
    req: SetModelRequest,
    current_user=Depends(get_current_user),
):
    """
    Set the default model in OpenCode's config.
    Format stored: "providerID/modelID" (e.g. "google/gemini-2.5-pro").
    This persists across sessions — OpenCode reads it on every message.
    Also available as a per-message override via sendChatMessage({ modelId, providerId }).
    """
    if config.forge_mode == "prod":
        raise HTTPException(status_code=403, detail="Model config not available in prod mode")

    model_string = f"{req.provider_id}/{req.model_id}"
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.patch(
                f"{config.opencode_url}/config",
                json={"model": model_string},
            )
            r.raise_for_status()
            return {"model": model_string}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"OpenCode unreachable: {e}")


@app.get("/api/projects/{project_id}/diff")
async def get_project_diff(
    project_id: str,
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Get the file diff stats for the current session.
    Returns per-file additions/deletions for the DiffCard UI.
    """
    from forge.agent.opencode_client import opencode_client

    project = await _get_or_migrate_project(db, project_id, current_user)
    if not project.opencode_session_id:
        return {"diff": []}

    ws_path = str(workspace_manager.path(current_user.id, project_id))

    import httpx
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{config.opencode_url}/session/{project.opencode_session_id}/diff",
                params={"directory": ws_path},
            )
            if r.status_code == 404:
                return {"diff": []}
            r.raise_for_status()
            return r.json()  # { "diff": FileDiff[] }
    except Exception as e:
        return {"diff": [], "error": str(e)}


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    print(f"\n🔥 {config.product_name} v{config.version}")
    print(f"   Backend : {config.model.backend}")
    print(f"   DB      : {config.db.url.split('@')[-1] if '@' in config.db.url else config.db.url}")
    print(f"   Docs    : http://{config.server.host}:{config.server.port}/docs\n")
    uvicorn.run("forge.server.app:app", host=config.server.host, port=config.server.port, reload=config.server.debug)
