"""
forge/agent/event_bridge.py
============================
Translates OpenCode's raw SSE bus events into clean Forge UI events.

OpenCode v2 emits a single unified event bus. This module filters events
to the current session and translates each Part type into a Forge-friendly
SSE payload that the FE can render directly.

OpenCode v2 event format (types.gen.ts):
  { "type": "message.part.updated", "properties": { "part": Part, "delta"?: string } }
  { "type": "session.idle",         "properties": { "sessionID": str } }
  { "type": "session.diff",         "properties": { "sessionID": str, "diff": FileDiff[] } }
  { "type": "session.error",        "properties": { "sessionID"?: str, "error"?: {...} } }
  { "type": "file.edited",          "properties": { "file": str } }
  { "type": "session.status",       "properties": { "sessionID": str, "status": {...} } }

Part union (relevant types):
  TextPart       { type: "text",      text, synthetic?, time? }
  ReasoningPart  { type: "reasoning", text, time: { start, end? } }
  ToolPart       { type: "tool",      callID, tool, state: ToolState }
  PatchPart      { type: "patch",     hash, files: string[] }
  StepStartPart  { type: "step-start" }
  StepFinishPart { type: "step-finish", cost, tokens: { input, output, reasoning, cache } }
  AgentPart      { type: "agent",     name }
  RetryPart      { type: "retry",     attempt, error: ApiError }
  CompactionPart { type: "compaction", auto }
  SnapshotPart   { type: "snapshot" }  — skip

ToolState:
  pending   { status: "pending",   input }
  running   { status: "running",   input, title?, time: { start } }
  completed { status: "completed", input, output, title, time: { start, end } }
  error     { status: "error",     input, error, time: { start, end } }

Forge FE SSE events emitted:
  event: message.part
  data: <MessagePart JSON>        — see FE types.ts for shape

  event: session.diff
  data: { diff: FileDiff[] }      — per-file additions/deletions

  event: message.done
  data: { session_id: str }       — turn complete (session.idle)

  event: error
  data: { message: str }          — session error

  event: file.edited
  data: { file: str }             — file changed on disk
"""

from __future__ import annotations

from typing import AsyncIterator, Optional

from forge.agent.opencode_client import opencode_client


async def bridge_events(
    workspace_path: str,
    session_id: Optional[str] = None,
) -> AsyncIterator[dict]:
    """
    Subscribe to OpenCode's event bus for the given session/workspace.
    Yield translated Forge SSE events.

    Filters events by session_id (if provided) or by workspace path.
    Runs until session.idle or session.error, or the generator is cancelled.
    """
    if session_id:
        try:
            session_info = await opencode_client.get_session(session_id, workspace_path)
            session_status = session_info.get("status", "idle")

            messages = await opencode_client.get_messages(session_id, workspace_path)
            if messages:
                # Find the last assistant message
                last_assistant_msg = None
                for msg in reversed(messages):
                    if msg.get("info", {}).get("role") == "assistant":
                        last_assistant_msg = msg
                        break

                if last_assistant_msg:
                    # Stream all its parts to the client so they get the cached/existing response parts immediately!
                    parts = last_assistant_msg.get("parts", [])
                    for part in parts:
                        translated = _translate_part(part)
                        if translated:
                            yield {"event": "message.part", "data": translated}

            if session_status == "idle":
                # If the session is already idle, we are done!
                yield {"event": "message.done", "data": {"session_id": session_id}}
                return
        except Exception as e:
            print(f"[EventBridge] WARNING: Failed to pre-fetch session/messages: {e}", flush=True)

    async for raw in opencode_client.stream_events(workspace_path):
        event_type = raw.get("type", "")
        properties = raw.get("properties", {})

        # ── Filter to our session ──────────────────────────────────────────────
        # OpenCode's bus is shared across all sessions. Filter by session_id when
        # known, otherwise fall back to workspace path matching.
        if session_id:
            event_session = (
                properties.get("sessionID")
                or properties.get("part", {}).get("sessionID")
            )
            if event_session and event_session != session_id:
                continue

        # ── Message part updated ───────────────────────────────────────────────
        if event_type == "message.part.updated":
            part = properties.get("part", {})
            delta = properties.get("delta")  # text delta for streaming text
            translated = _translate_part(part, delta)
            if translated:
                yield {"event": "message.part", "data": translated}

        # ── Session idle = turn complete ───────────────────────────────────────
        # OpenCode fires session.idle when the agent finishes a turn.
        elif event_type == "session.idle":
            yield {"event": "message.done", "data": {
                "session_id": properties.get("sessionID") or session_id,
            }}
            break  # One turn done — stop streaming until next message

        # ── Session diff — file change statistics ─────────────────────────────
        # Fires after patches are committed. Contains per-file +/- line counts.
        elif event_type == "session.diff":
            diff = properties.get("diff", [])
            if diff:
                yield {"event": "session.diff", "data": {
                    "session_id": properties.get("sessionID") or session_id,
                    "diff": diff,  # Array<{ file, before, after, additions, deletions }>
                }}

        # ── File edited on disk ────────────────────────────────────────────────
        elif event_type == "file.edited":
            yield {"event": "file.edited", "data": {
                "file": properties.get("file", ""),
            }}

        # ── Session error ──────────────────────────────────────────────────────
        elif event_type == "session.error":
            error = properties.get("error", {})
            message = (
                (error.get("data") or {}).get("message")
                or error.get("message")
                or "An error occurred"
            )
            yield {"event": "error", "data": {"message": message}}
            break

        # ── Session status (busy/retry/idle) — used to show retry in UI ───────
        elif event_type == "session.status":
            status = properties.get("status", {})
            if status.get("type") == "retry":
                # Forward as a special event — FE can show retry banner
                yield {"event": "session.status", "data": {
                    "type": "retry",
                    "attempt": status.get("attempt", 0),
                    "message": status.get("message", ""),
                    "next_ms": status.get("next", 0),
                }}

        # ── Informational events (no UI action needed) ────────────────────────
        # session.created, session.updated, session.deleted, session.compacted,
        # file.watcher.updated, vcs.branch.updated, etc. — silently ignored.


def _translate_part(part: dict, delta: Optional[str] = None) -> Optional[dict]:
    """
    Translate an OpenCode Part dict into a Forge FE MessagePart payload.
    Returns None for parts we skip (snapshot, compaction, file attachments, etc.)
    """
    part_type = part.get("type", "")

    # ── Reasoning (extended thinking) ─────────────────────────────────────────
    if part_type == "reasoning":
        text = part.get("text", "")
        if not text:
            return None
        time_info = part.get("time", {})
        # still streaming = start time set but no end time yet
        is_streaming = bool(time_info.get("start")) and not time_info.get("end")
        duration_ms: Optional[int] = None
        if time_info.get("start") and time_info.get("end"):
            duration_ms = int((time_info["end"] - time_info["start"]) * 1000)
        return {
            "type": "reasoning",
            "text": text,
            "streaming": is_streaming,
            "duration_ms": duration_ms,
        }

    # ── Text (final assistant response) ───────────────────────────────────────
    elif part_type == "text":
        text = part.get("text", "")
        if not text or part.get("synthetic") or part.get("ignored"):
            return None
        time_info = part.get("time", {})
        is_streaming = bool(time_info.get("start")) and not time_info.get("end")
        return {
            "type": "text",
            "text": text,
            "streaming": is_streaming,
            "delta": delta,  # incremental text chunk, if available
        }

    # ── Tool (call + state in one) ─────────────────────────────────────────────
    # OpenCode uses a single ToolPart that transitions through states:
    #   pending → running → completed | error
    # We forward the full state so the FE can render the current status.
    elif part_type == "tool":
        state = part.get("state", {})
        if not state:
            return None
        status = state.get("status", "pending")

        base: dict = {
            "type": "tool",
            "callID": part.get("callID", ""),
            "tool": part.get("tool", "unknown"),
            "state": {
                "status": status,
                "input": state.get("input", {}),
            },
        }

        if status == "running":
            base["state"]["title"] = state.get("title")
            time_info = state.get("time", {})
            base["state"]["time"] = {"start": time_info.get("start", 0)}

        elif status == "completed":
            time_info = state.get("time", {})
            base["state"]["output"] = state.get("output", "")
            base["state"]["title"]  = state.get("title", "")
            base["state"]["time"]   = {
                "start": time_info.get("start", 0),
                "end":   time_info.get("end", 0),
            }
            # Include file attachments if any (e.g. screenshots from browser tools)
            attachments = state.get("attachments")
            if attachments:
                base["state"]["attachments"] = [
                    {"url": a.get("url"), "filename": a.get("filename"), "mime": a.get("mime")}
                    for a in attachments
                ]

        elif status == "error":
            time_info = state.get("time", {})
            base["state"]["error"] = state.get("error", "Unknown error")
            base["state"]["time"]  = {
                "start": time_info.get("start", 0),
                "end":   time_info.get("end", 0),
            }

        return base

    # ── File patch (files committed to disk) ──────────────────────────────────
    # This fires when OpenCode writes a batch of files. The session.diff event
    # provides +/- counts; this just tells us which files were touched.
    elif part_type == "patch":
        files = part.get("files", [])
        return {
            "type": "patch",
            "hash": part.get("hash"),
            "files": files,
            "file_count": len(files),
        }

    # ── Step boundaries ───────────────────────────────────────────────────────
    elif part_type == "step-start":
        return {
            "type": "step_start",
        }

    elif part_type == "step-finish":
        tokens = part.get("tokens", {})
        cache = tokens.get("cache", {})
        cost = part.get("cost", 0)
        return {
            "type": "step_finish",
            "cost": cost,
            "tokens": {
                "input":       tokens.get("input", 0),
                "output":      tokens.get("output", 0),
                "reasoning":   tokens.get("reasoning", 0),
                "cache_read":  cache.get("read", 0),
                "cache_write": cache.get("write", 0),
            },
        }

    # ── Subagent activation ────────────────────────────────────────────────────
    elif part_type == "agent":
        name = part.get("name", "")
        if not name:
            return None
        return {
            "type": "agent",
            "name": name,
        }

    # ── Retry (rate-limit / transient error, will auto-retry) ────────────────
    elif part_type == "retry":
        error = part.get("error", {})
        error_data = error.get("data", {})
        return {
            "type": "retry",
            "attempt": part.get("attempt", 1),
            "error": {
                "message":     error_data.get("message", "Retrying…"),
                "isRetryable": error_data.get("isRetryable", True),
                "statusCode":  error_data.get("statusCode"),
            },
        }

    # ── Compaction notice ─────────────────────────────────────────────────────
    elif part_type == "compaction":
        return {
            "type": "compaction",
            "auto": part.get("auto", False),
        }

    # Internal/noisy types we skip: snapshot, file (attachment), subtask
    return None
