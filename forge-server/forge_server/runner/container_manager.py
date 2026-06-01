"""
forge_server/runner/container_manager.py
=========================================
Manages one Docker container per project.

Key design decisions:
  - Every container runs its dev server on :3000 INTERNALLY.
    No external port binding — zero port conflicts across 100k users.
  - Traefik discovers containers via Docker labels and routes
    {project_id}.preview.forge.com → container:3000 automatically.
  - BROWSER=none + CI=true + --no-open flags prevent the dev server
    from ever trying to open a browser tab (it's running headless in Docker).
  - node_modules are cached in a named Docker volume per project, so
    warm starts after sleep take ~3-5s instead of the cold npm install time.
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import docker
import docker.errors
from docker.models.containers import Container

from forge_server.config import get_settings

settings = get_settings()

# ── Docker client (sync, wrapped in executor for async callers) ───────────────
_docker_client: docker.DockerClient | None = None


def _get_docker() -> docker.DockerClient:
    global _docker_client
    if _docker_client is None:
        _docker_client = docker.from_env()
    return _docker_client


# ── Helpers ───────────────────────────────────────────────────────────────────

def _container_name(project_id: str) -> str:
    """forge-proj-{first 24 chars of project_id, alphanumeric only}"""
    slug = re.sub(r"[^a-z0-9]", "", project_id.lower())[:24]
    return f"{settings.container_prefix}-{slug}"


def _nm_volume_name(project_id: str) -> str:
    """Named volume for node_modules cache — survives container stop/start."""
    slug = re.sub(r"[^a-z0-9]", "", project_id.lower())[:24]
    return f"forge-nm-{slug}"


def _next_volume_name(project_id: str) -> str:
    """Named volume for .next cache — prevents cross-OS compilation corruption."""
    slug = re.sub(r"[^a-z0-9]", "", project_id.lower())[:24]
    return f"forge-next-{slug}"



def _host_port(project_id: str) -> int:
    """
    Deterministic host port for direct container debugging.
    Maps project_id → port in range 40000-49999 so there are no collisions
    and the port survives container restarts without any extra storage.

    Bound to 127.0.0.1 only — Traefik handles all browser-facing traffic
    via {project_id}.{preview_domain}. This binding is for `curl` debugging
    and as a fallback for running forge-server outside Docker.
    """
    import hashlib
    h = int(hashlib.md5(project_id.encode()).hexdigest(), 16)
    return 40000 + (h % 10000)


def _preview_url(project_id: str) -> str:
    """Browser-facing URL for the project preview, routed by Traefik."""
    return f"{settings.preview_scheme}://{project_id}.{settings.preview_domain}/"


def _traefik_labels(project_id: str) -> dict[str, str]:
    """
    Traefik dynamic routing labels.
    Routes {project_id}.preview.forge.com → this container's :3000.
    """
    router = f"proj-{project_id[:24]}"
    svc    = f"proj-svc-{project_id[:24]}"
    host   = f"{project_id}.{settings.preview_domain}"
    return {
        "traefik.enable": "true",
        f"traefik.http.routers.{router}.rule":                    f"Host(`{host}`)",
        f"traefik.http.routers.{router}.entrypoints":             "websecure",
        f"traefik.http.routers.{router}.middlewares":             "preview-security@file",
        f"traefik.http.services.{svc}.loadbalancer.server.port": str(settings.container_dev_port),
        f"traefik.http.routers.{router}.service":                 svc,
        # Also serve on plain HTTP for local dev
        f"traefik.http.routers.{router}-http.rule":               f"Host(`{host}`)",
        f"traefik.http.routers.{router}-http.entrypoints":        "web",
        f"traefik.http.routers.{router}-http.service":            svc,
        # Forge metadata
        "forge.project_id": project_id,
        "forge.managed":    "true",
    }


def _env_vars(extra: dict[str, str] | None = None) -> list[str]:
    """
    Environment variables injected into every project container.

    BROWSER=none   — CRA / react-scripts: do NOT open a browser
    CI=true        — Disables interactive prompts + browser open in many tools
    NO_OPEN=true   — Some custom tooling checks this
    VITE_NO_OPEN   — Vite-specific (belt-and-suspenders)
    PORT=3000      — Explicit port so all frameworks agree
    """
    base = {
        "PORT":           str(settings.container_dev_port),
        "VITE_PORT":      str(settings.container_dev_port),
        "BROWSER":        "none",       # CRA: never open browser
        "CI":             "true",       # disables interactive mode + auto-open
        "NO_OPEN":        "true",
        "VITE_NO_OPEN":   "true",
        "FORCE_COLOR":    "1",          # keep ANSI colours in logs
        "NODE_ENV":       "development",
    }
    if extra:
        base.update(extra)
    return [f"{k}={v}" for k, v in base.items()]


def _workspace_path(project_id: str, user_id: str) -> str:
    return str(
        Path(settings.forge_data_root)
        / "users" / user_id
        / "projects" / project_id
        / "workspace"
    )


# ── Sync Docker operations (run in thread pool) ───────────────────────────────

def _sync_get_container(name: str) -> Container | None:
    try:
        return _get_docker().containers.get(name)
    except docker.errors.NotFound:
        return None


PNPM_STORE_VOLUME = "forge-pnpm-store"
PNPM_STORE_MOUNT  = "/forge-store"


def _sync_create_container(
    project_id: str,
    user_id: str,
    extra_env: dict[str, str] | None = None,
) -> Container:
    """
    Create (but don't start) a project container.

    Volumes:
      - workspace bind-mount: /forge-data/.../workspace → /app  (read-write)
      - node_modules named volume: forge-nm-{id} → /app/node_modules
        Per-project — holds the symlink/hardlink tree pnpm creates.
      - pnpm shared store volume: forge-pnpm-store → /forge-store
        Shared by EVERY project — content-addressable package cache. Identical
        React/Vite/etc across projects dedupe to one copy on disk. See
        [[forge_storage_architecture]] for the math.
    """
    client    = _get_docker()
    name      = _container_name(project_id)
    ws_path   = _workspace_path(project_id, user_id)
    nm_vol    = _nm_volume_name(project_id)
    next_vol  = _next_volume_name(project_id)
    labels    = _traefik_labels(project_id)
    # Inject project ID for use in app code (e.g. API calls, logging).
    # Generated apps are served at http://<project_id>.<PREVIEW_DOMAIN>/ via
    # Traefik host routing, so default Next.js / React Router config works —
    # no basePath, assetPrefix, or router basename needed.
    forge_env = {"FORGE_PROJECT_ID": project_id}
    if extra_env:
        forge_env.update(extra_env)
    env       = _env_vars(forge_env)

    # Ensure workspace dir exists
    Path(ws_path).mkdir(parents=True, exist_ok=True)

    # Ensure per-project node_modules volume exists (idempotent)
    try:
        client.volumes.get(nm_vol)
    except docker.errors.NotFound:
        client.volumes.create(nm_vol)

    # Ensure .next volume exists (idempotent)
    try:
        client.volumes.get(next_vol)
    except docker.errors.NotFound:
        client.volumes.create(next_vol)

    # Ensure the SHARED pnpm store volume exists (idempotent). dev.sh also
    # ensures this, but creating here means the runner works in environments
    # where dev.sh isn't the entry point (e.g. forge-server running on a
    # CI/staging host).
    try:
        client.volumes.get(PNPM_STORE_VOLUME)
    except docker.errors.NotFound:
        client.volumes.create(PNPM_STORE_VOLUME)

    # Bind port 3000 → a deterministic host port on 127.0.0.1 for `curl`
    # debugging and as a fallback when forge-server runs outside Docker.
    # Browser traffic goes through Traefik via the Docker network — this
    # binding is never the user-visible entrypoint.
    port_bindings = {
        str(settings.container_dev_port): ("127.0.0.1", _host_port(project_id))
    }

    try:
        container = client.containers.create(
            image   = settings.container_image,
            name    = name,
            detach  = True,
            labels  = labels,
            environment = env,
            network = settings.docker_network,
            volumes = {
                # workspace files
                ws_path: {"bind": "/app", "mode": "rw"},
                # node_modules cache (persists across stop/start, never bind-mounted)
                nm_vol:  {"bind": "/app/node_modules", "mode": "rw"},
                # .next cache (prevents host macOS vs container Alpine compiler conflict)
                next_vol: {"bind": "/app/.next", "mode": "rw"},
                # Shared pnpm content-addressable store — dedupes packages
                # across every project on this host.
                PNPM_STORE_VOLUME: {"bind": PNPM_STORE_MOUNT, "mode": "rw"},
            },
            working_dir = "/app",
            ports = port_bindings,
            # Resource limits — prevent runaway containers
            mem_limit    = "1g",
            nano_cpus    = 1_000_000_000,   # 1 CPU
            restart_policy = {"Name": "no"},
        )
    except docker.errors.APIError as exc:
        if exc.response is not None and exc.response.status_code == 409:
            # Race condition: two concurrent ensure calls both saw "not_found"
            # and both tried to create. Second one wins — just fetch the existing container.
            container = client.containers.get(name)
        else:
            raise
    return container


def _sync_start_container(name: str) -> Container:
    c = _get_docker().containers.get(name)
    if c.status != "running":
        c.start()
    c.reload()
    return c


def _sync_stop_container(name: str, timeout: int = 10) -> None:
    try:
        c = _get_docker().containers.get(name)
        if c.status == "running":
            c.stop(timeout=timeout)
    except docker.errors.NotFound:
        pass


def _sync_remove_container(name: str) -> None:
    try:
        c = _get_docker().containers.get(name)
        c.remove(force=True)
    except docker.errors.NotFound:
        pass


def _sync_container_status(name: str) -> str:
    """Returns Docker status string or 'not_found'."""
    c = _sync_get_container(name)
    if c is None:
        return "not_found"
    c.reload()
    return c.status   # running | exited | created | paused | dead


def _sync_has_current_traefik_labels(name: str, project_id: str) -> bool:
    """
    Return True if the container has Traefik labels matching the CURRENT
    settings.preview_domain. Containers created with a stale PREVIEW_DOMAIN
    (e.g. before switching from preview.forge.com → preview.lvh.me) still
    have traefik.enable=true but route the wrong host, so a label-presence
    check alone isn't enough — we have to compare the host rule too.
    """
    c = _sync_get_container(name)
    if c is None:
        return False
    labels = (c.attrs.get("Config", {}) or {}).get("Labels", {}) or {}
    if labels.get("traefik.enable") != "true":
        return False
    expected_host = f"Host(`{project_id}.{settings.preview_domain}`)"
    # Any router rule on this container should reference the current host.
    for k, v in labels.items():
        if k.startswith("traefik.http.routers.") and k.endswith(".rule"):
            if v == expected_host:
                return True
    return False


def _sync_has_port_binding(name: str, container_port: int) -> bool:
    """
    Return True if the container has a host-port binding for `container_port`.

    Docker stores bindings in c.ports as e.g.:
      {"3000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "43242"}]}
    An unbound port looks like {"3000/tcp": None} or is absent entirely.
    """
    c = _sync_get_container(name)
    if c is None:
        return False
    c.reload()
    key = f"{container_port}/tcp"
    bindings = c.ports.get(key)
    return bool(bindings)  # None or [] → False; list with entries → True


def _sync_container_logs(name: str, tail: int = 100) -> str:
    c = _sync_get_container(name)
    if c is None:
        return ""
    try:
        return c.logs(tail=tail, timestamps=False).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _sync_patch_package_json(workspace_path: str) -> None:
    """
    Patch package.json to:
      1. Remove --open / --browser / -p flags that would open a browser
      2. Add BROWSER=none to dev script env (belt-and-suspenders)
      3. Ensure a 'dev' script exists
    """
    import json as _json
    pkg = Path(workspace_path) / "package.json"
    if not pkg.exists():
        return

    try:
        data = _json.loads(pkg.read_text())
        scripts = data.get("scripts", {})
        changed = False

        # Patterns that trigger browser open — strip them
        _open_flags = re.compile(
            r"\s+(?:--open|--browser(?:=\S+)?|-o\b|--launch-browser)", re.IGNORECASE
        )
        _port_flags = re.compile(
            r"\s+(?:--port[= ]\d+|-p\s+\d+)", re.IGNORECASE
        )

        for key in ("dev", "start", "serve", "preview"):
            if key not in scripts:
                continue
            orig = scripts[key]
            patched = _open_flags.sub("", orig)
            patched = _port_flags.sub("", patched).strip()
            if "vite" in patched.lower() and "--port" not in patched.lower():
                patched = f"{patched} --host 0.0.0.0 --port 3000"
            if patched != orig:
                scripts[key] = patched
                changed = True

        # Inject missing 'dev' script based on framework deps
        if "dev" not in scripts:
            deps = {
                **data.get("dependencies", {}),
                **data.get("devDependencies", {}),
            }
            if "next" in deps:
                scripts["dev"] = "next dev"
            elif "vite" in deps:
                scripts["dev"] = "vite --host 0.0.0.0 --port 3000"
            elif "react-scripts" in deps:
                scripts["dev"] = "react-scripts start"
            elif (Path(workspace_path) / "vite.config.ts").exists():
                scripts["dev"] = "vite --host 0.0.0.0 --port 3000"
            elif (Path(workspace_path) / "next.config.js").exists():
                scripts["dev"] = "next dev"
            elif "start" in scripts:
                scripts["dev"] = scripts["start"]
            changed = True

        # Concurrently launch backend/server if found in scripts
        be_key = "backend" if "backend" in scripts else "server" if "server" in scripts else None
        if be_key and "dev" in scripts:
            dev_cmd = scripts["dev"]
            be_cmd = scripts[be_key]
            if be_cmd not in dev_cmd:
                scripts["dev"] = f"{be_cmd} & {dev_cmd}"
                changed = True

        if changed:
            data["scripts"] = scripts
            pkg.write_text(_json.dumps(data, indent=2))

    except Exception as exc:
        print(f"[container_manager] patch_package_json failed: {exc}", flush=True)


# ── Async public API ──────────────────────────────────────────────────────────

async def _run(fn, *args, **kwargs):
    """Run a sync Docker call in a thread pool executor."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))


class ContainerManager:
    """
    Public async interface for managing project dev-server containers.

    Each project gets exactly one container named forge-proj-{project_id[:24]}.
    All containers expose their dev server on :3000 INTERNALLY — no host ports.
    Traefik routes *.preview.forge.com → container via Docker labels.
    """

    async def ensure(
        self,
        project_id: str,
        user_id: str,
        extra_env: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """
        Ensure the container exists and is running. Returns status dict.

        Flow:
          not_found → create + start  (cold start: npm install ~15-30s)
          exited    → start           (warm start: deps cached ~3-5s)
          running   → noop            (instant)
        """
        name   = _container_name(project_id)
        status = await _run(_sync_container_status, name)

        if status == "not_found":
            # Patch package.json before container creation
            ws = _workspace_path(project_id, user_id)
            await _run(_sync_patch_package_json, ws)
            await _run(_sync_create_container, project_id, user_id, extra_env)
            await _run(_sync_start_container, name)
            action = "cold_start"

        elif status == "exited":
            # Warm start — but first check Traefik labels are intact.
            # Containers created before subdomain routing was enabled have no
            # routing labels and would be unreachable via Traefik. Force-recreate.
            if not await _run(_sync_has_current_traefik_labels, name, project_id):
                print(
                    f"[container_manager] {name}: exited but missing traefik labels "
                    f"— removing and recreating (cold start)",
                    flush=True,
                )
                await _run(_sync_remove_container, name)
                ws = _workspace_path(project_id, user_id)
                await _run(_sync_patch_package_json, ws)
                await _run(_sync_create_container, project_id, user_id, extra_env)
                await _run(_sync_start_container, name)
                action = "cold_start"
            else:
                await _run(_sync_start_container, name)
                action = "warm_start"

        elif status in ("dead", "created"):
            # Container is in a bad/incomplete state — remove and recreate.
            await _run(_sync_remove_container, name)
            ws = _workspace_path(project_id, user_id)
            await _run(_sync_patch_package_json, ws)
            await _run(_sync_create_container, project_id, user_id, extra_env)
            await _run(_sync_start_container, name)
            action = "cold_start"

        elif status == "running":
            # Running — but verify Traefik labels are intact. A container that
            # is already running without routing labels won't appear in Traefik's
            # config; stop + recreate so the preview URL works.
            if not await _run(_sync_has_current_traefik_labels, name, project_id):
                print(
                    f"[container_manager] {name}: running but missing traefik labels "
                    f"— stopping and recreating (cold start)",
                    flush=True,
                )
                await _run(_sync_stop_container, name)
                await _run(_sync_remove_container, name)
                ws = _workspace_path(project_id, user_id)
                await _run(_sync_patch_package_json, ws)
                await _run(_sync_create_container, project_id, user_id, extra_env)
                await _run(_sync_start_container, name)
                action = "cold_start"
            else:
                action = "already_running"

        else:
            # paused or unknown — try start anyway
            await _run(_sync_start_container, name)
            action = "resumed"

        # Start the log watcher in the background. Idempotent — safe to call
        # on every ensure() including already_running. The watcher does the
        # actual `docker logs --follow` and surfaces runtime errors via SSE.
        # Lazy-import to avoid an import cycle (log_watcher imports back into
        # this module for the docker client).
        try:
            from forge_server.runner.log_watcher import start_watcher
            await start_watcher(project_id, name)
        except Exception as e:
            # Watcher is non-critical — container is up either way.
            print(f"[container_manager] start_watcher failed: {e}", flush=True)

        return {
            "container_name": name,
            "preview_url":    _preview_url(project_id),
            "action":         action,
            "status":         "starting" if action != "already_running" else "running",
        }

    async def stop(self, project_id: str) -> None:
        name = _container_name(project_id)
        try:
            from forge_server.runner.log_watcher import stop_watcher
            await stop_watcher(project_id)
        except Exception:
            pass
        await _run(_sync_stop_container, name)

    async def remove(self, project_id: str) -> None:
        name = _container_name(project_id)
        try:
            from forge_server.runner.log_watcher import stop_watcher
            await stop_watcher(project_id)
        except Exception:
            pass
        await _run(_sync_remove_container, name)

    async def status(self, project_id: str) -> str:
        """Returns: running | sleeping | stopped | not_found"""
        name         = _container_name(project_id)
        docker_status = await _run(_sync_container_status, name)
        if docker_status == "running":
            return "running"
        if docker_status in ("exited", "dead"):
            return "sleeping"
        if docker_status == "not_found":
            return "not_found"
        return "stopped"

    async def logs(self, project_id: str, tail: int = 150) -> str:
        name = _container_name(project_id)
        return await _run(_sync_container_logs, name, tail)

    async def health_check(
        self,
        project_id: str,
        timeout: float = 300.0,
        interval: float = 2.0,
    ) -> bool:
        """
        Poll the container's dev server until it responds on /.
        Returns True when ready, False on timeout.

        Cold starts require npm install inside Docker which can take 3-5 minutes,
        so the default timeout is 300s. Callers may pass a shorter timeout for
        warm starts where node_modules are already cached in the named volume.

        Two ways to reach the container, depending on how forge-server runs:
          - Native uvicorn on the host (dev.sh): use the bound host port at
            127.0.0.1:<_host_port>. Docker DNS names aren't resolvable here.
          - Inside Docker (docker-compose service): forge-server is on
            forge-net alongside project containers, so forge-proj-<id>:3000
            is reachable by container DNS — no host port hop needed.
        """
        import httpx
        import os

        name        = _container_name(project_id)
        in_docker   = os.path.exists("/.dockerenv")
        if in_docker:
            url = f"http://{name}:{settings.container_dev_port}/"
        else:
            url = f"http://127.0.0.1:{_host_port(project_id)}/"
        deadline = asyncio.get_event_loop().time() + timeout

        async with httpx.AsyncClient(timeout=3.0) as client:
            while asyncio.get_event_loop().time() < deadline:
                try:
                    r = await client.get(url)
                    if r.status_code < 500:
                        return True
                except Exception:
                    pass
                await asyncio.sleep(interval)

        return False

    def preview_url(self, project_id: str) -> str:
        return _preview_url(project_id)

    def container_name(self, project_id: str) -> str:
        return _container_name(project_id)


# ── Singleton ─────────────────────────────────────────────────────────────────
container_manager = ContainerManager()
