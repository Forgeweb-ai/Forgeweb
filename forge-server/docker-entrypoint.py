#!/usr/bin/env python3
"""
forge-server entrypoint
=======================
Self-bootstraps everything forge-server needs before uvicorn starts. Run as
the container's PID 1 (set as ENTRYPOINT in the Dockerfile). When the script
finishes the boot work it exec()s into uvicorn, so uvicorn becomes PID 1
and signal handling (SIGTERM from `docker stop`) propagates correctly.

Boot sequence:
  1. Wait for Postgres on DATABASE_URL to accept connections (≤60s).
  2. `alembic upgrade head` — idempotent; no-op if schema already current.
  3. Build `forge-runner:latest` if it doesn't exist in the Docker daemon.
     The Python docker SDK tars `/app/runner-image` and streams it to the
     daemon via the socket, so this works whether forge-server runs inside
     or outside Docker — no DinD or shared bind-mount tricks required.
  4. exec uvicorn forge_server.app:app on $PORT (default 8000).

Failures are graded:
  - Steps 1–2 are fatal: forge-server can't function without the DB.
  - Step 3 is best-effort: forge-server still serves read endpoints, auth,
    and settings without it; only project create/start needs the runner
    image. We warn loudly and continue.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path


def log(msg: str) -> None:
    print(f"[entrypoint] {msg}", flush=True)


# ── 1. Wait for Postgres ──────────────────────────────────────────────────────
async def wait_for_postgres(db_url: str, timeout_s: int = 60) -> None:
    """asyncpg-level reachability check. Returns when the DB accepts a
    connection or raises after the timeout."""
    import asyncpg  # imported lazily so a broken import surfaces here, not at top of file

    # asyncpg wants postgresql://, not postgresql+asyncpg://
    plain = db_url.replace("postgresql+asyncpg://", "postgresql://", 1)

    deadline = time.monotonic() + timeout_s
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        try:
            conn = await asyncpg.connect(plain, timeout=2)
            await conn.close()
            log(f"Postgres ready (attempt {attempt})")
            return
        except Exception as e:
            # Progress every 5s so the boot log isn't silent
            if attempt % 5 == 0:
                log(f"  still waiting for Postgres… ({attempt}s): {type(e).__name__}")
            await asyncio.sleep(1)

    raise SystemExit(
        f"[entrypoint] FATAL: Postgres on {plain.split('@')[-1]} didn't accept "
        f"connections within {timeout_s}s. Check that the postgres service is "
        f"healthy: `docker compose logs postgres`"
    )


def step_wait_postgres() -> None:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        raise SystemExit("[entrypoint] FATAL: DATABASE_URL is not set")
    log(f"Waiting for Postgres ({db_url.split('@')[-1]})…")
    asyncio.run(wait_for_postgres(db_url))


# ── 2. Alembic schema migration ───────────────────────────────────────────────
def step_alembic_upgrade() -> None:
    import subprocess

    log("Applying Alembic migrations (alembic upgrade head)…")
    try:
        subprocess.run(
            ["alembic", "upgrade", "head"],
            check=True,
            cwd="/app",
        )
        log("Alembic upgrade complete")
    except subprocess.CalledProcessError as e:
        raise SystemExit(
            f"[entrypoint] FATAL: alembic upgrade head failed (exit {e.returncode}). "
            f"Check forge-server/alembic/versions/ and the DATABASE_URL."
        )


# ── 3. Build forge-runner:latest if missing ───────────────────────────────────
RUNNER_IMAGE = "forge-runner:latest"
RUNNER_CONTEXT = "/app/runner-image"


def step_build_runner_image() -> None:
    """Build the project-preview runner image if it doesn't exist yet.

    The Docker socket is expected to be mounted at /var/run/docker.sock
    (set in docker-compose.yml). The Python SDK tars the build context
    locally and streams it to the daemon over the socket, so the daemon
    doesn't need filesystem visibility into our container.

    Best-effort: a failure here is logged and tolerated; only operations
    that spin up project containers will hit the missing-image error.
    """
    try:
        import docker
        from docker.errors import DockerException, ImageNotFound
    except ImportError:
        log("WARN: docker SDK not installed; skipping runner image check")
        return

    try:
        client = docker.from_env()
        # Quick socket reachability probe so we fail fast with a clear message
        # rather than mid-build.
        client.ping()
    except DockerException as e:
        log(f"WARN: Docker socket not reachable ({e}); skipping runner image build")
        log("       project create/start will fail until the image is built manually.")
        return

    try:
        client.images.get(RUNNER_IMAGE)
        log(f"{RUNNER_IMAGE} already present")
        return
    except ImageNotFound:
        pass  # fall through to build

    if not Path(RUNNER_CONTEXT).is_dir():
        log(
            f"WARN: {RUNNER_CONTEXT} not in image (expected COPY . . to include "
            f"runner-image/). Skipping runner build."
        )
        return

    log(f"Building {RUNNER_IMAGE} from {RUNNER_CONTEXT} (first run; may take a minute)…")
    try:
        image, build_logs = client.images.build(
            path=RUNNER_CONTEXT,
            tag=RUNNER_IMAGE,
            rm=True,
            pull=False,
        )
        # Stream a compact build log so the user knows what happened. Full
        # docker output would flood; we surface only "Step N/M" lines.
        for chunk in build_logs:
            stream = chunk.get("stream") or ""
            if stream.startswith("Step "):
                log(f"  {stream.rstrip()}")
        log(f"{RUNNER_IMAGE} built")
    except Exception as e:
        log(f"WARN: runner image build failed ({type(e).__name__}: {e})")
        log("       project create/start will fail until the image is built manually.")


# ── 4. exec uvicorn ───────────────────────────────────────────────────────────
def step_exec_uvicorn() -> None:
    port = os.environ.get("PORT", "8000")
    cmd = [
        "uvicorn",
        "forge_server.app:app",
        "--host", "0.0.0.0",
        "--port", port,
    ]
    log(f"Bootstrap done. exec → {' '.join(cmd)}")
    os.execvp(cmd[0], cmd)


# ── main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    step_wait_postgres()
    step_alembic_upgrade()
    step_build_runner_image()
    step_exec_uvicorn()


if __name__ == "__main__":
    main()
