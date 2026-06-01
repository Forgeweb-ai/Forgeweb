"""
forge/agent/opencode_client.py
===============================
HTTP client for the OpenCode server running at OPENCODE_URL (default http://opencode:7777).

OpenCode exposes a full REST + SSE API. Every endpoint accepts ?directory= to
route the request to the correct project workspace. One OpenCode server handles
ALL users and projects — no per-project processes needed.

Usage:
    from forge.agent.opencode_client import opencode_client

    session_id = await opencode_client.create_session(workspace_path)
    await opencode_client.send_message(session_id, workspace_path, "Build a todo app")

    async for event in opencode_client.stream_events(workspace_path):
        print(event)  # {"type": "session.message.part.updated", "properties": {...}}
"""

from __future__ import annotations

import json
import asyncio
from typing import AsyncIterator, Optional

import httpx

from forge.config import config


class OpenCodeClient:
    """Thin async HTTP wrapper around OpenCode's REST API."""

    def _base(self) -> str:
        return config.opencode_url.rstrip("/")

    def _params(self, workspace_path: str) -> dict:
        """Every OpenCode API call gets ?directory= for workspace routing."""
        return {"directory": workspace_path}

    # ── Sessions ───────────────────────────────────────────────────────────────

    async def create_session(self, workspace_path: str) -> str:
        """Create a new OpenCode session for this workspace. Returns session ID."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                f"{self._base()}/session",
                params=self._params(workspace_path),
            )
            r.raise_for_status()
            return r.json()["id"]

    async def get_session(self, session_id: str, workspace_path: str) -> dict:
        """Get session metadata."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{self._base()}/session/{session_id}",
                params=self._params(workspace_path),
            )
            r.raise_for_status()
            return r.json()

    async def abort_session(self, session_id: str, workspace_path: str) -> None:
        """Abort a running session (stop the agent mid-run)."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                f"{self._base()}/session/{session_id}/abort",
                params=self._params(workspace_path),
            )

    # ── Messages ───────────────────────────────────────────────────────────────

    async def send_message(
        self,
        session_id: str,
        workspace_path: str,
        text: str,
        model_id: Optional[str] = None,
        provider_id: Optional[str] = None,
    ) -> None:
        """
        Send a user message to OpenCode. The agent starts processing immediately.
        Subscribe to stream_events() to see reasoning, tool calls, and patches in real time.
        """
        payload: dict = {
            "parts": [{"type": "text", "text": text}],
        }
        # OpenCode message API expects model as a nested object:
        # { "model": { "providerID": "google", "modelID": "gemini-2.5-pro" } }
        # Both fields are required when overriding the model per-message.
        if model_id and provider_id:
            payload["model"] = {"providerID": provider_id, "modelID": model_id}

        # Long timeout: complex tasks (e.g. "build a Next.js app with auth")
        # can take 3–5 min before OpenCode returns the initial response.
        # 600s (10 min) covers the worst-case deep agentic runs.
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=15.0, read=600.0, write=30.0, pool=5.0)) as client:
            r = await client.post(
                f"{self._base()}/session/{session_id}/message",
                params=self._params(workspace_path),
                json=payload,
            )
            r.raise_for_status()

    async def get_messages(self, session_id: str, workspace_path: str) -> list[dict]:
        """Get all messages in a session (for loading history)."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(
                f"{self._base()}/session/{session_id}/message",
                params=self._params(workspace_path),
            )
            r.raise_for_status()
            return r.json()

    # ── Event stream ───────────────────────────────────────────────────────────

    async def stream_events(self, workspace_path: str) -> AsyncIterator[dict]:
        """
        Subscribe to OpenCode's SSE event bus for this workspace.
        Yields raw event dicts as they arrive.

        Event types include:
          session.message.part.updated  — reasoning, text, tool calls, patches
          session.message.completed     — message turn finished
          session.completed             — full session done
          session.error                 — error occurred

        This streams indefinitely. The caller should cancel the async iterator
        when they no longer need events (e.g. when the session completes).
        """
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                "GET",
                f"{self._base()}/event",
                params=self._params(workspace_path),
                headers={"Accept": "text/event-stream"},
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        data = line[6:].strip()
                        if data and data != "[DONE]":
                            try:
                                yield json.loads(data)
                            except json.JSONDecodeError:
                                pass
                    # Ignore "event:", "id:", "retry:" lines — we only care about data

    async def stream_events_until_done(
        self, workspace_path: str, session_id: str
    ) -> AsyncIterator[dict]:
        """
        Like stream_events() but stops automatically when the session completes.
        Useful for single-turn agentic runs.
        """
        async for event in self.stream_events(workspace_path):
            yield event
            event_type = event.get("type", "")
            if event_type in ("session.completed", "session.error"):
                break

    # ── Health ────────────────────────────────────────────────────────────────

    async def health(self) -> bool:
        """Check if OpenCode server is reachable."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(f"{self._base()}/")
                return r.status_code < 500
        except Exception:
            return False


# ── Singleton ─────────────────────────────────────────────────────────────────
opencode_client = OpenCodeClient()
