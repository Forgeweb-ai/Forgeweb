"""
forge/db/models.py
==================
SQLAlchemy ORM models for Forge V2.

Tables:
  users            — registered users (email + password auth)
  projects         — a saved codebase / webapp project
  project_files    — current file tree for a project
  file_versions    — full snapshot on every update (git-like history)
  conversations    — chat threads linked to a project
  messages         — individual chat messages
  file_embeddings  — sentence-transformer chunks for context search
"""

import uuid
from datetime import datetime
from sqlalchemy import (
    Column, String, Text, Integer, Boolean,
    DateTime, ForeignKey, JSON, UniqueConstraint
)
from sqlalchemy.orm import relationship

from forge.db.database import Base


def _uuid():
    return str(uuid.uuid4())


# Cross-database UUID column — stored as VARCHAR(36) string on both
# SQLite and PostgreSQL. Using String avoids importing PG-specific dialect.
def UUID(as_uuid=False):  # noqa: N802 — mimic the original import interface
    return String(36)


class User(Base):
    """
    Registered user. Auth via email + bcrypt password.
    Workspace root auto-derived: /forge-data/users/{id}/
    """
    __tablename__ = "users"

    id               = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    email            = Column(String(255), unique=True, nullable=False, index=True)
    username         = Column(String(100), unique=True, nullable=False)
    hashed_password  = Column(String(255), nullable=False)
    session_id       = Column(String(255), unique=True, nullable=True, index=True)
    created_at       = Column(DateTime, default=datetime.utcnow)

    projects      = relationship("Project",      back_populates="user", cascade="all, delete-orphan")
    conversations = relationship("Conversation", back_populates="user", cascade="all, delete-orphan")


class Project(Base):
    """
    A webapp project. Each project gets:
      - A workspace folder: /forge-data/users/{user_id}/projects/{id}/workspace/
      - An OpenCode session for AI chat (opencode_session_id)
      - A Docker container when deployed (container_id / container_status)
      - A subdomain preview URL: https://{id}.preview.forge.com
    """
    __tablename__ = "projects"

    id               = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id          = Column(UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name             = Column(String(255), nullable=False)
    description      = Column(Text, default="")
    tech_stack       = Column(JSON, default=list)
    stack            = Column(JSON, default=dict)   # {fe, be, db} selection
    setup_commands   = Column(JSON, default=list)
    run_command      = Column(String(500), default="")
    current_version  = Column(Integer, default=1)
    context_summary  = Column(Text, default="")

    # V2: workspace + OpenCode + Docker
    workspace_path      = Column(String(500), nullable=True)   # absolute path on server
    opencode_session_id = Column(String(255), nullable=True)   # OpenCode session ID
    container_id        = Column(String(100), nullable=True)   # Docker container ID
    container_status    = Column(String(50),  default="stopped")  # stopped|building|running|error
    preview_url         = Column(String(255), nullable=True)   # https://{id}.preview.forge.com

    # V2: showcase / gallery
    showcased_at         = Column(DateTime, nullable=True)
    showcase_name        = Column(String(255), nullable=True)
    showcase_description = Column(Text, nullable=True)
    thumbnail_url        = Column(String(500), nullable=True)

    created_at       = Column(DateTime, default=datetime.utcnow)
    updated_at       = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user          = relationship("User",         back_populates="projects")
    files         = relationship("ProjectFile",  back_populates="project", cascade="all, delete-orphan")
    versions      = relationship("FileVersion",  back_populates="project", cascade="all, delete-orphan", order_by="FileVersion.version_number")
    conversations = relationship("Conversation", back_populates="project")
    embeddings    = relationship("FileEmbedding", back_populates="project", cascade="all, delete-orphan")


class ProjectFile(Base):
    __tablename__ = "project_files"
    __table_args__ = (UniqueConstraint("project_id", "path", name="uq_project_file_path"),)

    id          = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    project_id  = Column(UUID(as_uuid=False), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    path        = Column(String(500), nullable=False)
    content     = Column(Text, default="")
    language    = Column(String(50), default="")
    description = Column(Text, default="")
    updated_at  = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    project = relationship("Project", back_populates="files")


class FileVersion(Base):
    """Full snapshot of all project files at a point in time."""
    __tablename__ = "file_versions"
    __table_args__ = (UniqueConstraint("project_id", "version_number", name="uq_project_version"),)

    id             = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    project_id     = Column(UUID(as_uuid=False), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    version_number = Column(Integer, nullable=False)
    instruction    = Column(Text, default="")
    files_snapshot = Column(JSON, nullable=False)
    created_at     = Column(DateTime, default=datetime.utcnow)

    project = relationship("Project", back_populates="versions")


class Conversation(Base):
    __tablename__ = "conversations"

    id         = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id    = Column(UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    project_id = Column(UUID(as_uuid=False), ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True)
    title      = Column(String(255), default="New chat")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user     = relationship("User",    back_populates="conversations")
    project  = relationship("Project", back_populates="conversations")
    messages = relationship("Message", back_populates="conversation", cascade="all, delete-orphan", order_by="Message.created_at")


class Message(Base):
    __tablename__ = "messages"

    id              = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    conversation_id = Column(UUID(as_uuid=False), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True)
    role            = Column(String(20), nullable=False)   # user | assistant | system
    content         = Column(Text, nullable=False)
    created_at      = Column(DateTime, default=datetime.utcnow)

    conversation = relationship("Conversation", back_populates="messages")


class FileEmbedding(Base):
    __tablename__ = "file_embeddings"

    id          = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    project_id  = Column(UUID(as_uuid=False), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    file_path   = Column(String(500), nullable=False)
    chunk_index = Column(Integer, nullable=False)
    chunk_text  = Column(Text, nullable=False)
    embedding   = Column(JSON, nullable=False)
    line_start  = Column(Integer, default=0)
    line_end    = Column(Integer, default=0)

    project = relationship("Project", back_populates="embeddings")
