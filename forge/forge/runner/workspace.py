"""
forge/runner/workspace.py  (V2)
================================
WorkspaceManager — gives every project a permanent home on disk at
/forge-data/users/{user_id}/projects/{project_id}/workspace/

OpenCode manages the .opencode/ subdirectory automatically.
No port assignment — Docker + Traefik handle all networking.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from forge.config import config


class WorkspaceManager:
    """Manages per-user, per-project workspace directories."""

    def _root(self) -> Path:
        return Path(config.forge_data_root).resolve().absolute()

    def user_home(self, user_id: str) -> Path:
        """
        /forge-data/users/{user_id}/projects/
        Created on user register. All projects live here.
        """
        p = self._root() / "users" / user_id / "projects"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def path(self, user_id: str, project_id: str) -> Path:
        """
        /forge-data/users/{user_id}/projects/{project_id}/workspace/
        This is the directory OpenCode operates in.
        """
        return self.user_home(user_id) / project_id / "workspace"

    def create(self, user_id: str, project_id: str) -> Path:
        """Create the workspace directory and return its path."""
        ws = self.path(user_id, project_id)
        ws.mkdir(parents=True, exist_ok=True)
        return ws

    def destroy(self, user_id: str, project_id: str) -> None:
        """Remove the entire project directory (workspace + all metadata)."""
        project_dir = self.user_home(user_id) / project_id
        if project_dir.exists():
            shutil.rmtree(project_dir)

    def exists(self, user_id: str, project_id: str) -> bool:
        return self.path(user_id, project_id).exists()

    def write_files(self, user_id: str, project_id: str, files: list[dict]) -> None:
        """Write a list of {path, content} dicts to the workspace."""
        ws = self.path(user_id, project_id)
        for f in files:
            dest = ws / f["path"].lstrip("/")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(f.get("content", ""), encoding="utf-8")

    def read_file(self, user_id: str, project_id: str, rel_path: str) -> str:
        """Read a file from the workspace."""
        target = self.path(user_id, project_id) / rel_path.lstrip("/")
        if not target.exists():
            raise FileNotFoundError(f"Not found in workspace: {rel_path}")
        return target.read_text(encoding="utf-8")

    def list_files(self, user_id: str, project_id: str) -> list[str]:
        """Recursively list all relative file paths (excludes .opencode/)."""
        ws = self.path(user_id, project_id)
        if not ws.exists():
            return []
        return [
            str(f.relative_to(ws))
            for f in ws.rglob("*")
            if f.is_file() and ".opencode" not in f.parts
        ]

    def size_kb(self, user_id: str, project_id: str) -> int:
        """Total workspace size in KB (excluding .opencode/)."""
        ws = self.path(user_id, project_id)
        if not ws.exists():
            return 0
        total = sum(
            f.stat().st_size
            for f in ws.rglob("*")
            if f.is_file() and ".opencode" not in f.parts
        )
        return total // 1024


workspace_manager = WorkspaceManager()
