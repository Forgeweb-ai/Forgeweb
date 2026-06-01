"""
forge_server/runner/log_watcher.py
===================================
Continuous tail of project dev-container logs. Detects runtime errors,
broadcasts them to FE via SSE, and (in a follow-up milestone) auto-triggers
the verify subagent in opencode.

Why this exists:
  The verify subagent fires when the main build agent finishes a turn. But
  many runtime errors only appear LATER — e.g. a drizzle migration runs at
  container start, or a fetch from the user's browser hits a broken route.
  Without a continuous tail, those errors never reach verify and the user
  sees a broken preview with no auto-recovery.

Architecture:
  - One asyncio task per running container, registered by project_id.
  - The actual `docker logs --follow` iterator is blocking, so we run it
    in a thread pool executor and pump each decoded line into an asyncio
    queue the main task drains.
  - Each non-noise line goes through runner/log_parser.match_line.
  - On a new (signature, detail) pair, we:
      1. Stash {signature, detail, line, ts} in an in-memory ring per project
         so the /api/projects/{id}/verify endpoint can return "last seen"
         without needing the watcher to be active right at request time.
      2. Broadcast an SSE event of shape {event_type: "runtime_error", ...}
         to whatever clients are subscribed to /api/dev/stream.
      3. (TODO) POST to opencode session.promptAsync to spawn verify, gated
         on the global verify-loop budget defined in forge-verify.ts.

  - Debounce: identical (signature, detail) within DEBOUNCE_SEC is treated
    as a repeat and suppressed. Otherwise an error that fires on every
    page render would flood SSE and re-trigger verify on every keystroke.

  - Lifecycle: container_manager.ensure() calls start_watcher() AFTER the
    container is verified running. container_manager.stop()/.remove() calls
    stop_watcher(). The module is safe to import from anywhere; nothing
    starts on import.

Restart resilience:
  In-memory state is lost on forge-server restart. That's acceptable for
  v1 — on the next log line after restart, the watcher will re-detect any
  ongoing error and re-broadcast. Persisting to dev_containers.last_runtime_error
  is a follow-up (requires an alembic migration).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, asdict, field
from typing import Any

import docker
import httpx
from docker.models.containers import Container

from forge_server.config import get_settings
from forge_server.runner.log_parser import LogError, match_line

log      = logging.getLogger("forge.log_watcher")
settings = get_settings()


# Debounce window: identical (signature, detail) within this many seconds
# is suppressed. 60s is long enough to coalesce a render-loop error firing
# every frame but short enough that a recurring intermittent error still
# surfaces a few times per minute.
DEBOUNCE_SEC = 60.0

# Per-project ring size — we keep the last N distinct errors for the /verify
# endpoint to return. Small because errors compound: usually one root cause
# manifests as multiple downstream signatures and we want to surface the
# most recent few, not the full history.
RING_SIZE = 10

# ── Auto-fix budget ───────────────────────────────────────────────────────────
# These mirror forge-verify.ts so the watcher and the post-completion verify
# agent agree on when to stop trying. At BYOK scale every prompt is the user's
# own money; the budget is what stops a render-loop error from burning $$$.
#
# Budget is per (project_id, opencode_session_id). A new session resets.
AUTOFIX_TOTAL_BUDGET         = 8         # max prompts per session, all signatures
AUTOFIX_PER_SIGNATURE_BUDGET = 3         # same signature retried at most N times
AUTOFIX_WALL_CLOCK_SEC       = 10 * 60   # session-wide wall clock
AUTOFIX_QUIET_SEC            = 20.0      # wait this long after an error before
                                          # firing — if the agent's still typing,
                                          # the agent will fix it before we do


@dataclass
class RuntimeError_:
    signature: str
    detail:    str
    line:      str
    ts:        float

    def to_payload(self) -> dict[str, Any]:
        d = asdict(self)
        return d


@dataclass
class _AutoFixBudget:
    """Per-(project, session) bookkeeping for the auto-fix loop.

    Mirrors the structure used by forge-verify.ts in the opencode plugin so
    both sides enforce the same cost ceiling. Reset whenever the active
    opencode_session_id for a project changes (new session = new budget).
    """
    session_id:         str
    started_at:         float
    total_attempts:     int = 0
    signature_attempts: dict[str, int] = field(default_factory=dict)

    def block_reason(self, signature: str) -> str | None:
        if self.total_attempts >= AUTOFIX_TOTAL_BUDGET:
            return "total budget exhausted"
        if time.time() - self.started_at > AUTOFIX_WALL_CLOCK_SEC:
            return "wall clock exhausted"
        n = self.signature_attempts.get(signature, 0)
        if n >= AUTOFIX_PER_SIGNATURE_BUDGET:
            return f"signature budget exhausted ({signature})"
        return None

    def record(self, signature: str) -> None:
        self.total_attempts += 1
        self.signature_attempts[signature] = self.signature_attempts.get(signature, 0) + 1


class _ProjectWatcher:
    """One watcher per project container. Owns its background task."""

    def __init__(self, project_id: str, container_name: str):
        self.project_id     = project_id
        self.container_name = container_name
        self.task: asyncio.Task | None = None
        self.recent: list[RuntimeError_] = []     # ring of last RING_SIZE errors
        self._last_emit: dict[tuple[str, str], float] = {}  # debounce state
        self._autofix: _AutoFixBudget | None = None
        # External stop signal — set ONLY by stop(). _tail_once must never
        # set this. Internal "tail ended naturally" is signalled by the
        # pump pushing None into the line queue.
        self._external_stop = asyncio.Event()

    async def start(self) -> None:
        if self.task and not self.task.done():
            return
        self._external_stop.clear()
        self.task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._external_stop.set()
        if self.task:
            try:
                await asyncio.wait_for(self.task, timeout=5.0)
            except asyncio.TimeoutError:
                self.task.cancel()
            except asyncio.CancelledError:
                pass
        self.task = None

    async def _run(self) -> None:
        """
        Main loop. We dispatch the blocking docker logs stream to a thread
        and pump lines through a bounded queue. On any failure we back off
        and retry — containers restart, log streams break, that's normal.
        """
        backoff = 2.0
        while not self._external_stop.is_set():
            try:
                await self._tail_once()
                # _tail_once returns when stream ends (container stopped).
                # If stop wasn't requested, the container probably restarted —
                # short pause then reconnect.
                if self._external_stop.is_set():
                    return
                await asyncio.sleep(1.0)
            except docker.errors.NotFound:
                log.info("watcher: container %s not found, exiting", self.container_name)
                return
            except Exception as e:
                log.warning(
                    "watcher: %s tail failed (%s); retrying in %.0fs",
                    self.container_name, e, backoff,
                )
                try:
                    await asyncio.wait_for(self._external_stop.wait(), timeout=backoff)
                    return  # external stop fired
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, 30.0)

    async def _tail_once(self) -> None:
        """Single attached `docker logs -f` session. Returns when stream ends."""
        from forge_server.runner.container_manager import _get_docker

        loop  = asyncio.get_running_loop()
        queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=1000)

        def _pump_blocking() -> None:
            try:
                client = _get_docker()
                c = client.containers.get(self.container_name)
                # follow=True → infinite stream; tail=0 → don't replay history.
                stream = c.logs(
                    stream=True, follow=True, tail=0, stdout=True, stderr=True,
                )
                for chunk in stream:
                    if self._external_stop.is_set():
                        break
                    try:
                        text = chunk.decode("utf-8", errors="replace")
                    except Exception:
                        continue
                    for line in text.splitlines():
                        if not line:
                            continue
                        loop.call_soon_threadsafe(queue.put_nowait, line)
            except Exception as e:
                log.debug("watcher: %s pump exception: %s", self.container_name, e)
            finally:
                # Sentinel signals "no more lines from this stream attempt".
                loop.call_soon_threadsafe(queue.put_nowait, None)

        pump_future = loop.run_in_executor(None, _pump_blocking)

        try:
            while not self._external_stop.is_set():
                try:
                    line = await asyncio.wait_for(queue.get(), timeout=5.0)
                except asyncio.TimeoutError:
                    continue
                if line is None:
                    return  # pump signalled end of stream
                self._handle_line(line)
        finally:
            # If we're returning because of external stop, the pump's inner
            # loop will see _external_stop on its next chunk and exit. If the
            # stream is blocked on read, it'll unblock when docker closes it
            # (container stop/remove) — bounded by the 2s wait below.
            try:
                await asyncio.wait_for(pump_future, timeout=2.0)
            except asyncio.TimeoutError:
                pass

    def _handle_line(self, line: str) -> None:
        err = match_line(line)
        if err is None:
            return
        key = (err.signature, err.detail)
        now = time.time()
        last = self._last_emit.get(key, 0.0)
        if now - last < DEBOUNCE_SEC:
            return
        self._last_emit[key] = now

        rec = RuntimeError_(
            signature=err.signature,
            detail=err.detail,
            line=err.line,
            ts=now,
        )
        self.recent.append(rec)
        if len(self.recent) > RING_SIZE:
            self.recent.pop(0)

        log.info(
            "watcher: %s detected %s detail=%r",
            self.container_name, err.signature, err.detail[:60],
        )
        _broadcast(self.project_id, rec)
        # Persist to the shared runtime-errors ring so the agent + UI can
        # read it without depending on the in-memory state of this watcher
        # (which is per-process and lost on restart). Fire-and-forget — the
        # store handles its own failures and we must not block the pump.
        try:
            from forge_server.runner.runtime_errors_store import record as _re_record

            asyncio.create_task(_re_record(self.project_id, {
                "source":    "server",
                "signature": err.signature,
                "detail":    err.detail,
                "line":      err.line,
                "container": self.container_name,
            }))
        except Exception as e:
            log.debug("watcher: runtime-errors store unavailable: %s", e)

        # Auto-fix: hand the error to the build agent so the user sees the
        # preview self-heal without having to type "fix it". Bounded by the
        # AUTOFIX_* budget so a render-loop error can't burn BYOK tokens.
        # Fire-and-forget — anything that blocks here back-pressures the
        # docker logs pump.
        asyncio.create_task(self._maybe_autofix(err))

    async def _maybe_autofix(self, err: LogError) -> None:
        """
        Trigger an opencode build-agent prompt to fix `err`.

        Gating, in order — first failure exits cleanly:
          1. Project must have an opencode_session_id (no session → nothing
             to prompt; agent will pick up errors from the ring on its next
             interactive turn).
          2. (project, session) budget must not be exhausted.
          3. Quiet window: wait AUTOFIX_QUIET_SEC for the agent to fix the
             error itself (this is the common case during a `build` turn).
             If new errors of the same signature keep firing during the
             window, we collapse them into one prompt.

        Cost shape: at most AUTOFIX_TOTAL_BUDGET prompts per session, ever.
        Cost stays FLAT as the chat grows (no per-turn re-cost). On a
        healthy app this code path never fires.
        """
        # 1. Look up the project's opencode session.
        try:
            from forge_server.db.database import AsyncSessionLocal
            from forge_server.db.models import Project
            from sqlalchemy import select

            async with AsyncSessionLocal() as db:
                row = await db.execute(
                    select(Project.opencode_session_id)
                    .where(Project.id == self.project_id)
                )
                session_id = row.scalar_one_or_none()
        except Exception as e:
            log.debug("autofix: project lookup failed: %s", e)
            return

        if not session_id:
            log.debug("autofix: project %s has no opencode session; skipping",
                      self.project_id)
            return

        # 2. Budget check (reset if session changed).
        if self._autofix is None or self._autofix.session_id != session_id:
            self._autofix = _AutoFixBudget(
                session_id=session_id, started_at=time.time(),
            )
        blocked = self._autofix.block_reason(err.signature)
        if blocked:
            log.info("autofix: blocked (%s) project=%s sig=%s",
                     blocked, self.project_id, err.signature)
            return

        # 3. Quiet window — let the agent fix it itself if it's mid-turn.
        await asyncio.sleep(AUTOFIX_QUIET_SEC)
        # Re-check budget: a concurrent watcher tick may have fired already.
        if self._autofix.block_reason(err.signature):
            return
        self._autofix.record(err.signature)

        # 4. Fire the prompt. Fire-and-forget; opencode's prompt_async
        # returns 204 immediately and queues the agent loop.
        url = f"{settings.opencode_url}/api/session/{session_id}/prompt_async"
        # Detail is bounded by parse_tail to 500 chars; truncate again here
        # so we don't ship a multi-KB payload on weird log lines.
        detail = (err.detail or err.line or "")[:600]
        payload = {
            "agent": "build",
            "parts": [{
                "type": "text",
                "text": (
                    "A runtime error appeared in the dev server logs. "
                    "Read it, find the root cause, fix it, and confirm the "
                    "preview is healthy. Do not announce that a watcher ran — "
                    "just say what you fixed in one sentence.\n\n"
                    f"Signature: {err.signature}\n"
                    f"Detail: {detail}"
                ),
            }],
        }
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.post(url, json=payload)
                if r.status_code >= 400:
                    log.warning(
                        "autofix: opencode prompt_async %s -> %d: %s",
                        url, r.status_code, r.text[:200],
                    )
                    return
            log.info(
                "autofix: prompted build agent project=%s sig=%s attempt=%d/%d",
                self.project_id, err.signature,
                self._autofix.total_attempts, AUTOFIX_TOTAL_BUDGET,
            )
        except Exception as e:
            log.warning("autofix: opencode request failed: %s", e)


# ── Registry ─────────────────────────────────────────────────────────────────

_watchers: dict[str, _ProjectWatcher] = {}


async def start_watcher(project_id: str, container_name: str) -> None:
    """Idempotent. Safe to call from container_manager.ensure() unconditionally."""
    w = _watchers.get(project_id)
    if w and w.task and not w.task.done():
        return
    w = _ProjectWatcher(project_id, container_name)
    _watchers[project_id] = w
    await w.start()
    log.info("watcher: started for project %s (container %s)", project_id, container_name)


async def stop_watcher(project_id: str) -> None:
    w = _watchers.pop(project_id, None)
    if w:
        await w.stop()
        log.info("watcher: stopped for project %s", project_id)


def recent_errors(project_id: str) -> list[dict[str, Any]]:
    """Used by /api/projects/{id}/verify to return last-seen runtime errors."""
    w = _watchers.get(project_id)
    if not w:
        return []
    return [r.to_payload() for r in w.recent]


# ── SSE bridge ───────────────────────────────────────────────────────────────

def _broadcast(project_id: str, err: RuntimeError_) -> None:
    """
    Push a runtime_error frame to /api/dev/stream subscribers.

    We import lazily to avoid a circular import: dev.py imports
    container_manager, which we don't want to make depend on dev.py.
    """
    try:
        from forge_server.api.dev import _subscribers
    except Exception:
        return

    payload = {
        "event_type": "runtime_error",
        "signature":  err.signature,
        "detail":     err.detail,
        "line":       err.line,
        "ts":         err.ts,
    }
    for q in _subscribers.get(project_id, []):
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            pass
