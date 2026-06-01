"""
forge/runner/port_registry.py
==============================
SQLite-backed port registry at ~/.forge/port_registry.db

Each project is assigned a 5-port block:
  base+0  → fe      (frontend dev server)
  base+1  → be      (backend API server)
  base+2  → db      (database, e.g. Postgres)
  base+3  → extra1  (spare / second frontend)
  base+4  → extra2  (spare / websocket / docs)

Port range: 10000–59999 → 10,000 five-port blocks → 10,000 concurrent projects.

Thread-safe: uses SQLite BEGIN EXCLUSIVE transactions so multiple
workers (uvicorn, celery, etc.) can allocate ports without races.
"""

from __future__ import annotations

import sqlite3
import socket
import time
from pathlib import Path
from typing import Optional

# ── Config ─────────────────────────────────────────────────────────────────────

PORT_MIN   = 10_000
PORT_MAX   = 59_999   # inclusive
BLOCK_SIZE = 5
MAX_BLOCKS = (PORT_MAX - PORT_MIN + 1) // BLOCK_SIZE   # 10 000


# ── Registry path ──────────────────────────────────────────────────────────────

def _registry_path() -> Path:
    p = Path.home() / ".forge"
    p.mkdir(exist_ok=True)
    return p / "port_registry.db"


# ── Low-level helpers ──────────────────────────────────────────────────────────

def _port_free(port: int) -> bool:
    """Return True if nothing is listening on 127.0.0.1:{port}."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.05)
        return s.connect_ex(("127.0.0.1", port)) != 0


def _block_free(base: int) -> bool:
    """Return True if all 5 ports in the block are free at the OS level."""
    return all(_port_free(base + i) for i in range(BLOCK_SIZE))


# ── PortRegistry ───────────────────────────────────────────────────────────────

class PortRegistry:
    """
    Persistent SQLite registry.  All public methods are synchronous and
    can be called from any thread or process.
    """

    def __init__(self) -> None:
        self._db = str(_registry_path())
        self._init_db()

    # ── Schema ─────────────────────────────────────────────────────────────────

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS port_allocations (
                    project_id   TEXT PRIMARY KEY,
                    base_port    INTEGER UNIQUE NOT NULL,
                    fe_port      INTEGER NOT NULL,
                    be_port      INTEGER NOT NULL,
                    db_port      INTEGER NOT NULL,
                    extra1_port  INTEGER NOT NULL,
                    extra2_port  INTEGER NOT NULL,
                    allocated_at REAL NOT NULL   -- unix timestamp
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_base ON port_allocations(base_port)")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db, timeout=10, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    # ── Public API ─────────────────────────────────────────────────────────────

    def get(self, project_id: str) -> Optional[dict[str, int]]:
        """Return existing allocation for project_id, or None."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM port_allocations WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "fe":     row["fe_port"],
            "be":     row["be_port"],
            "db":     row["db_port"],
            "extra1": row["extra1_port"],
            "extra2": row["extra2_port"],
        }

    def allocate(self, project_id: str) -> dict[str, int]:
        """
        Return ports for project_id — existing allocation if one exists,
        otherwise find the next free 5-port block, record it, and return it.

        Uses BEGIN EXCLUSIVE to prevent two processes claiming the same block.
        """
        # Fast path: already allocated
        existing = self.get(project_id)
        if existing:
            return existing

        conn = self._connect()
        try:
            conn.execute("BEGIN EXCLUSIVE")

            # Double-check inside the lock (another process may have raced us)
            row = conn.execute(
                "SELECT * FROM port_allocations WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            if row:
                conn.execute("COMMIT")
                return {
                    "fe":     row["fe_port"],
                    "be":     row["be_port"],
                    "db":     row["db_port"],
                    "extra1": row["extra1_port"],
                    "extra2": row["extra2_port"],
                }

            # Collect all claimed base ports
            claimed = {
                r[0] for r in conn.execute("SELECT base_port FROM port_allocations").fetchall()
            }

            # Find the first available block
            base = self._find_free_block(claimed)
            if base is None:
                conn.execute("ROLLBACK")
                raise RuntimeError(
                    "Port registry exhausted — no free 5-port block in range "
                    f"{PORT_MIN}–{PORT_MAX}.  "
                    "Release unused projects or expand PORT_MAX."
                )

            ports = {
                "fe":     base,
                "be":     base + 1,
                "db":     base + 2,
                "extra1": base + 3,
                "extra2": base + 4,
            }
            conn.execute(
                """
                INSERT INTO port_allocations
                    (project_id, base_port, fe_port, be_port, db_port,
                     extra1_port, extra2_port, allocated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    base,
                    ports["fe"],
                    ports["be"],
                    ports["db"],
                    ports["extra1"],
                    ports["extra2"],
                    time.time(),
                ),
            )
            conn.execute("COMMIT")
            return ports

        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def release(self, project_id: str) -> bool:
        """
        Free the port block assigned to project_id.
        Returns True if a row was deleted, False if it wasn't found.
        """
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM port_allocations WHERE project_id = ?",
                (project_id,),
            )
            return cur.rowcount > 0

    def list_allocations(self) -> list[dict]:
        """Return all allocations — useful for admin / diagnostics."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM port_allocations ORDER BY base_port"
            ).fetchall()
        return [dict(r) for r in rows]

    def total_allocated(self) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM port_allocations").fetchone()[0]

    # ── Internal ───────────────────────────────────────────────────────────────

    def _find_free_block(self, claimed: set[int]) -> Optional[int]:
        """
        Iterate through all blocks in PORT_MIN..PORT_MAX (step BLOCK_SIZE).
        Return the first base that is:
          1. Not already in the registry
          2. All 5 ports actually free at the OS level (socket check)

        OS check prevents collisions with non-Forge processes (system services,
        other apps) that happen to sit in our range.
        """
        for i in range(MAX_BLOCKS):
            base = PORT_MIN + i * BLOCK_SIZE
            if base in claimed:
                continue
            if _block_free(base):
                return base
        return None


# ── Singleton ──────────────────────────────────────────────────────────────────

port_registry = PortRegistry()
