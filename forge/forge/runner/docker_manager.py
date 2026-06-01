"""
forge/runner/docker_manager.py
===============================
DockerManager — builds and runs generated project apps as Docker containers.

Each project gets:
  - An image tagged `forge-project-{project_id}`
  - A container named `forge-proj-{project_id}`

Two routing modes, selected by PREVIEW_DOMAIN:

  LOCAL (PREVIEW_DOMAIN not set / "preview.localhost"):
    - Container port 8000 is mapped to a stable host port in range 10000–19999.
      The port is derived from the project_id hash so it survives restarts.
    - Preview URL: http://localhost:{port}
    - No Traefik required. Runs on the host's Docker daemon directly.

  PRODUCTION (PREVIEW_DOMAIN=forge.com or any real domain):
    - No host port exposure. Container joins the "forge-net" Docker network.
    - Traefik routes {project_id}.{domain} → container:8000 via labels.
    - Preview URL: https://{project_id}.{domain}

Requirements: pip install docker
Docker socket must be mounted: /var/run/docker.sock:/var/run/docker.sock
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import AsyncIterator, Optional

import docker
import docker.errors

from forge.config import config

# Port range for local-dev host port assignment.
_LOCAL_PORT_MIN = 10000
_LOCAL_PORT_MAX = 19999


class DockerManager:

    def __init__(self):
        self._client: Optional[docker.DockerClient] = None

    def _get_client(self) -> docker.DockerClient:
        if self._client is None:
            try:
                self._client = docker.from_env()
            except Exception as exc:
                raise RuntimeError(
                    "Docker is not running or the Docker socket is not accessible. "
                    "Start Docker Desktop and try again."
                ) from exc
        return self._client

    def _docker_available(self) -> bool:
        """Return True if Docker is reachable, False otherwise (no raise)."""
        try:
            self._get_client()
            return True
        except RuntimeError:
            self._client = None   # reset so next attempt retries
            return False

    # ── Mode detection ────────────────────────────────────────────────────────

    def _is_local(self) -> bool:
        """
        Local mode: PREVIEW_DOMAIN is not set, empty, or is the default
        "preview.localhost". In local mode we expose a stable host port instead
        of relying on Traefik.
        """
        domain = config.preview_domain.strip().lower()
        return not domain or domain in ("preview.localhost", "localhost")

    def _local_port(self, project_id: str) -> int:
        """
        Derive a stable host port for this project in range 10000–19999.
        Same project always gets the same port so page-reload works.
        """
        h = int(hashlib.md5(project_id.encode()).hexdigest(), 16)
        return _LOCAL_PORT_MIN + (h % (_LOCAL_PORT_MAX - _LOCAL_PORT_MIN + 1))

    # ── Naming ────────────────────────────────────────────────────────────────

    def image_tag(self, project_id: str) -> str:
        return f"forge-project-{project_id}"

    def container_name(self, project_id: str) -> str:
        return f"forge-proj-{project_id}"

    def preview_url(self, project_id: str) -> str:
        if self._is_local():
            return f"http://localhost:{self._local_port(project_id)}"
        return f"https://{project_id}.{config.preview_domain}"

    # ── Build ─────────────────────────────────────────────────────────────────

    async def build(self, project_id: str, workspace_path: str) -> AsyncIterator[str]:
        """
        Build the Docker image from the project's workspace Dockerfile.
        Streams build log lines as they arrive.

        Usage:
            async for line in docker_manager.build(project_id, workspace_path):
                print(line)  # "Step 1/8 : FROM node:20-alpine"
        """
        client = self._get_client()
        loop = asyncio.get_event_loop()

        def _build():
            _, logs = client.images.build(
                path=workspace_path,
                tag=self.image_tag(project_id),
                rm=True,
                forcerm=True,
            )
            return list(logs)

        log_chunks = await loop.run_in_executor(None, _build)

        for chunk in log_chunks:
            if "stream" in chunk:
                line = chunk["stream"].rstrip("\n")
                if line:
                    yield line
            elif "error" in chunk:
                yield f"ERROR: {chunk['error']}"
                raise RuntimeError(f"Docker build failed: {chunk['error']}")

    # ── Run ───────────────────────────────────────────────────────────────────

    async def run(self, project_id: str, user_id: str) -> str:
        """
        Start the project container. Returns the container ID.

        Local mode (PREVIEW_DOMAIN not set / preview.localhost):
          - Maps container:8000 → host:{stable_port} (10000-19999, hash of project_id)
          - Each project gets its own port — no conflict with Forge UI on 3000
          - Preview: http://localhost:{port}

        Production mode (PREVIEW_DOMAIN=forge.com):
          - Joins forge-net, Traefik routes {project_id}.{domain} → container:8000
          - No host ports exposed
          - Preview: https://{project_id}.{domain}
        """
        client = self._get_client()
        domain = f"{project_id}.{config.preview_domain}"
        loop = asyncio.get_event_loop()

        # Stop any existing container for this project first
        await self.stop(project_id)

        local = self._is_local()
        host_port = self._local_port(project_id) if local else None

        def _run():
            kwargs: dict = dict(
                image=self.image_tag(project_id),
                name=self.container_name(project_id),
                detach=True,
                environment={
                    "NODE_ENV": "production",
                    "PORT": "8000",
                },
                labels={
                    "forge.project_id": project_id,
                    "forge.user_id": user_id,
                },
                restart_policy={"Name": "on-failure", "MaximumRetryCount": 3},
            )

            if local:
                # ── Local dev: expose a stable host port, no Traefik needed ──
                # Each project gets its own port (10000-19999) so it never
                # conflicts with Forge's own UI on port 3000.
                kwargs["ports"] = {"8000/tcp": host_port}
            else:
                # ── Production: Traefik subdomain routing, no host ports ──
                kwargs["network"] = "forge-net"
                kwargs["labels"].update({
                    "traefik.enable": "true",
                    f"traefik.http.routers.{project_id}.rule": f"Host(`{domain}`)",
                    f"traefik.http.routers.{project_id}.entrypoints": "web",
                    f"traefik.http.routers.{project_id}-tls.rule": f"Host(`{domain}`)",
                    f"traefik.http.routers.{project_id}-tls.entrypoints": "websecure",
                    f"traefik.http.routers.{project_id}-tls.tls": "true",
                    f"traefik.http.services.{project_id}.loadbalancer.server.port": "8000",
                })

            container = client.containers.run(**kwargs)
            return container.id

        return await loop.run_in_executor(None, _run)

    # ── Stop / Remove ─────────────────────────────────────────────────────────

    async def stop(self, project_id: str) -> None:
        """Gracefully stop and remove the project container."""
        client = self._get_client()
        loop = asyncio.get_event_loop()

        def _stop():
            try:
                c = client.containers.get(self.container_name(project_id))
                c.stop(timeout=10)
                c.remove(force=True)
            except docker.errors.NotFound:
                pass

        await loop.run_in_executor(None, _stop)

    async def remove_image(self, project_id: str) -> None:
        """Remove the built image (called when deleting a project)."""
        client = self._get_client()
        loop = asyncio.get_event_loop()

        def _remove():
            try:
                client.images.remove(self.image_tag(project_id), force=True)
            except docker.errors.ImageNotFound:
                pass

        await loop.run_in_executor(None, _remove)

    # ── Status & Logs ─────────────────────────────────────────────────────────

    def status(self, project_id: str) -> str:
        """
        Returns: "running" | "stopped" | "exited" | "error" | "not_built" | "docker_unavailable"
        """
        if not self._docker_available():
            return "docker_unavailable"
        client = self._get_client()
        try:
            c = client.containers.get(self.container_name(project_id))
            return c.status  # "running", "exited", "created", "paused"
        except docker.errors.NotFound:
            # Check if image exists
            try:
                client.images.get(self.image_tag(project_id))
                return "stopped"  # Built but not running
            except docker.errors.ImageNotFound:
                return "not_built"

    async def logs(self, project_id: str, lines: int = 100) -> list[str]:
        """Get last N lines of container logs."""
        client = self._get_client()
        loop = asyncio.get_event_loop()

        def _logs():
            try:
                c = client.containers.get(self.container_name(project_id))
                raw = c.logs(tail=lines, timestamps=False)
                return raw.decode("utf-8", errors="replace").splitlines()
            except docker.errors.NotFound:
                return []

        return await loop.run_in_executor(None, _logs)

    async def stream_logs(self, project_id: str) -> AsyncIterator[str]:
        """
        Stream live logs from a running container.
        Yields log lines as they arrive.
        """
        client = self._get_client()
        loop = asyncio.get_event_loop()

        def _get_container():
            return client.containers.get(self.container_name(project_id))

        try:
            container = await loop.run_in_executor(None, _get_container)
        except docker.errors.NotFound:
            return

        def _stream():
            return container.logs(stream=True, follow=True, timestamps=False)

        log_stream = await loop.run_in_executor(None, _stream)

        for chunk in log_stream:
            if isinstance(chunk, bytes):
                line = chunk.decode("utf-8", errors="replace").rstrip("\n")
                if line:
                    yield line

    def get_container_info(self, project_id: str) -> Optional[dict]:
        """Get container metadata (status, started_at, etc.)"""
        client = self._get_client()
        try:
            c = client.containers.get(self.container_name(project_id))
            attrs = c.attrs
            return {
                "id": c.short_id,
                "status": c.status,
                "started_at": attrs.get("State", {}).get("StartedAt"),
                "image": self.image_tag(project_id),
                "preview_url": self.preview_url(project_id),
            }
        except docker.errors.NotFound:
            return None


# ── Singleton ─────────────────────────────────────────────────────────────────
docker_manager = DockerManager()
