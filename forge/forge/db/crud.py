"""
forge/db/crud.py
================
All CRUD operations. Every function takes an AsyncSession as first arg.
No business logic here — just DB reads/writes.
"""

from datetime import datetime
from typing import Optional
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from forge.db.models import (
    User, Project, ProjectFile, FileVersion,
    Conversation, Message, FileEmbedding
)


# ── Users ─────────────────────────────────────────────────────────────────────

async def get_or_create_user(db: AsyncSession, session_id: str) -> User:
    result = await db.execute(select(User).where(User.session_id == session_id))
    user = result.scalar_one_or_none()
    if not user:
        # Fallback to check if a user with this email or username already exists
        email = f"{session_id}@example.com"
        result_existing = await db.execute(
            select(User).where((User.email == email) | (User.username == session_id))
        )
        user = result_existing.scalar_one_or_none()
        if user:
            # Update session_id to link it
            user.session_id = session_id
            await db.flush()
        else:
            user = User(
                session_id=session_id,
                email=email,
                username=session_id,
                hashed_password="mock-password-not-used"
            )
            db.add(user)
            try:
                await db.flush()
            except Exception:
                await db.rollback()
                # Try to retrieve in case of concurrent insert race condition
                result = await db.execute(
                    select(User).where(
                        (User.session_id == session_id) | 
                        (User.email == email) | 
                        (User.username == session_id)
                    )
                )
                user = result.scalar_one_or_none()
                if user:
                    user.session_id = session_id
                    await db.flush()
                else:
                    raise
    return user


# ── Projects ──────────────────────────────────────────────────────────────────

async def list_projects(db: AsyncSession, user_id: str) -> list[Project]:
    result = await db.execute(
        select(Project)
        .where(Project.user_id == user_id)
        .order_by(Project.updated_at.desc())
    )
    return result.scalars().all()


async def get_project(db: AsyncSession, project_id: str, user_id: str) -> Optional[Project]:
    result = await db.execute(
        select(Project)
        .options(
            selectinload(Project.files),
            selectinload(Project.versions),
        )
        .where(Project.id == project_id, Project.user_id == user_id)
    )
    return result.scalar_one_or_none()


async def get_project_global(db: AsyncSession, project_id: str) -> Optional[Project]:
    result = await db.execute(
        select(Project)
        .options(
            selectinload(Project.files),
            selectinload(Project.versions),
        )
        .where(Project.id == project_id)
    )
    return result.scalar_one_or_none()


async def create_project(
    db: AsyncSession,
    user_id: str,
    name: str,
    description: str,
    tech_stack: list,
    setup_commands: list,
    run_command: str,
    files: list[dict],
) -> Project:
    project = Project(
        user_id=user_id,
        name=name,
        description=description,
        tech_stack=tech_stack,
        setup_commands=setup_commands,
        run_command=run_command,
        current_version=1,
    )
    db.add(project)
    await db.flush()

    for f in files:
        pf = ProjectFile(
            project_id=project.id,
            path=f["path"],
            content=f.get("content", ""),
            language=f.get("language", ""),
            description=f.get("description", ""),
        )
        db.add(pf)

    # Create initial version snapshot
    version = FileVersion(
        project_id=project.id,
        version_number=1,
        instruction="Initial generation",
        files_snapshot=files,
    )
    db.add(version)
    await db.flush()
    return project


async def update_project_files(
    db: AsyncSession,
    project_id: str,
    user_id: str,
    files: list[dict],
    instruction: str,
) -> Optional[Project]:
    """
    Replace the project's file tree with new files, then create a version snapshot.
    Returns the updated project, or None if not found.
    """
    result = await db.execute(
        select(Project).where(Project.id == project_id, Project.user_id == user_id)
    )
    project = result.scalar_one_or_none()
    if not project:
        return None

    # Delete old files and replace
    await db.execute(delete(ProjectFile).where(ProjectFile.project_id == project_id))
    for f in files:
        pf = ProjectFile(
            project_id=project_id,
            path=f["path"],
            content=f.get("content", ""),
            language=f.get("language", ""),
            description=f.get("description", ""),
        )
        db.add(pf)

    # Bump version
    project.current_version += 1
    project.updated_at = datetime.utcnow()

    version = FileVersion(
        project_id=project_id,
        version_number=project.current_version,
        instruction=instruction[:200],
        files_snapshot=files,
    )
    db.add(version)
    await db.flush()
    return project


async def update_project_meta(
    db: AsyncSession,
    project_id: str,
    user_id: str,
    run_command: str | None = None,
    setup_commands: list[str] | None = None,
) -> Optional[Project]:
    """
    Update project metadata fields (run_command, setup_commands) without touching files.
    Returns the updated project, or None if not found.
    """
    result = await db.execute(
        select(Project).where(Project.id == project_id, Project.user_id == user_id)
    )
    project = result.scalar_one_or_none()
    if not project:
        return None
    if run_command is not None:
        project.run_command = run_command
    if setup_commands is not None:
        project.setup_commands = setup_commands
    project.updated_at = datetime.utcnow()
    await db.flush()
    return project


async def delete_project(db: AsyncSession, project_id: str, user_id: str) -> bool:
    result = await db.execute(
        select(Project).where(Project.id == project_id, Project.user_id == user_id)
    )
    project = result.scalar_one_or_none()
    if not project:
        return False
    await db.delete(project)
    return True


# ── Versions ──────────────────────────────────────────────────────────────────

async def list_versions(db: AsyncSession, project_id: str, user_id: str) -> list[FileVersion]:
    # Verify ownership
    result = await db.execute(
        select(Project).where(Project.id == project_id, Project.user_id == user_id)
    )
    if not result.scalar_one_or_none():
        return []

    result = await db.execute(
        select(FileVersion)
        .where(FileVersion.project_id == project_id)
        .order_by(FileVersion.version_number.desc())
    )
    return result.scalars().all()


async def restore_version(
    db: AsyncSession,
    project_id: str,
    user_id: str,
    version_number: int,
) -> Optional[Project]:
    """
    Restore project files to a specific version.
    Deletes all versions AFTER this version (like git reset --hard).
    Sets current_version to the restored version number.
    """
    result = await db.execute(
        select(Project).where(Project.id == project_id, Project.user_id == user_id)
    )
    project = result.scalar_one_or_none()
    if not project:
        return None

    result = await db.execute(
        select(FileVersion).where(
            FileVersion.project_id == project_id,
            FileVersion.version_number == version_number,
        )
    )
    version = result.scalar_one_or_none()
    if not version:
        return None

    # Delete versions newer than the target
    await db.execute(
        delete(FileVersion).where(
            FileVersion.project_id == project_id,
            FileVersion.version_number > version_number,
        )
    )

    # Replace files with snapshot
    await db.execute(delete(ProjectFile).where(ProjectFile.project_id == project_id))
    for f in version.files_snapshot:
        pf = ProjectFile(
            project_id=project_id,
            path=f["path"],
            content=f.get("content", ""),
            language=f.get("language", ""),
            description=f.get("description", ""),
        )
        db.add(pf)

    # Also clear embeddings — they'll be re-indexed
    await db.execute(delete(FileEmbedding).where(FileEmbedding.project_id == project_id))

    project.current_version = version_number
    project.updated_at = datetime.utcnow()
    await db.flush()
    return project


# ── Conversations ─────────────────────────────────────────────────────────────

async def list_conversations(db: AsyncSession, user_id: str, project_id: Optional[str] = None) -> list[Conversation]:
    q = select(Conversation).where(Conversation.user_id == user_id)
    if project_id:
        q = q.where(Conversation.project_id == project_id)
    q = q.order_by(Conversation.updated_at.desc())
    result = await db.execute(q)
    return result.scalars().all()


async def create_conversation(
    db: AsyncSession,
    user_id: str,
    title: str = "New chat",
    project_id: Optional[str] = None,
) -> Conversation:
    convo = Conversation(user_id=user_id, project_id=project_id, title=title)
    db.add(convo)
    await db.flush()
    return convo


async def get_conversation_with_messages(db: AsyncSession, convo_id: str, user_id: str) -> Optional[Conversation]:
    result = await db.execute(
        select(Conversation)
        .options(selectinload(Conversation.messages))
        .where(Conversation.id == convo_id, Conversation.user_id == user_id)
    )
    return result.scalar_one_or_none()


async def add_message(
    db: AsyncSession,
    conversation_id: str,
    role: str,
    content: str,
) -> Message:
    msg = Message(conversation_id=conversation_id, role=role, content=content)
    db.add(msg)

    # Update conversation updated_at + auto-title from first user message
    result = await db.execute(
        select(Conversation).where(Conversation.id == conversation_id)
    )
    convo = result.scalar_one_or_none()
    if convo:
        convo.updated_at = datetime.utcnow()
        if convo.title in ("New chat", "") and role == "user":
            convo.title = content[:60].strip()

    await db.flush()
    return msg


async def delete_conversation(db: AsyncSession, convo_id: str, user_id: str) -> bool:
    result = await db.execute(
        select(Conversation).where(Conversation.id == convo_id, Conversation.user_id == user_id)
    )
    convo = result.scalar_one_or_none()
    if not convo:
        return False
    await db.delete(convo)
    return True


# ── Project context summary ───────────────────────────────────────────────────

async def get_project_context(db: AsyncSession, project_id: str, user_id: str) -> str:
    """Return the stored AI context summary for a project, or '' if none."""
    from sqlalchemy import text as _text
    result = await db.execute(
        select(Project.context_summary).where(
            Project.id == project_id,
            Project.user_id == user_id,
        )
    )
    row = result.one_or_none()
    return (row[0] or "") if row else ""


async def update_project_context(
    db: AsyncSession,
    project_id: str,
    user_id: str,
    context_summary: str,
) -> bool:
    """Overwrite the AI context summary for a project. Returns True on success."""
    result = await db.execute(
        select(Project).where(Project.id == project_id, Project.user_id == user_id)
    )
    project = result.scalar_one_or_none()
    if not project:
        return False
    project.context_summary = context_summary
    await db.flush()
    return True


# ── Embeddings ────────────────────────────────────────────────────────────────

async def save_embeddings(db: AsyncSession, project_id: str, chunks: list[dict]):
    """
    Replace all embeddings for a project.
    chunks: list of {file_path, chunk_index, chunk_text, embedding, line_start, line_end}
    """
    await db.execute(delete(FileEmbedding).where(FileEmbedding.project_id == project_id))
    for c in chunks:
        emb = FileEmbedding(
            project_id=project_id,
            file_path=c["file_path"],
            chunk_index=c["chunk_index"],
            chunk_text=c["chunk_text"],
            embedding=c["embedding"],
            line_start=c.get("line_start", 0),
            line_end=c.get("line_end", 0),
        )
        db.add(emb)
    await db.flush()


async def get_embeddings(db: AsyncSession, project_id: str) -> list[FileEmbedding]:
    result = await db.execute(
        select(FileEmbedding).where(FileEmbedding.project_id == project_id)
    )
    return result.scalars().all()


# ── Auth (V2) ─────────────────────────────────────────────────────────────────

async def get_user_by_id(db: AsyncSession, user_id: str) -> Optional[User]:
    result = await db.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()


async def get_user_by_email(db: AsyncSession, email: str) -> Optional[User]:
    result = await db.execute(select(User).where(User.email == email))
    return result.scalar_one_or_none()


async def get_user_by_username(db: AsyncSession, username: str) -> Optional[User]:
    result = await db.execute(select(User).where(User.username == username))
    return result.scalar_one_or_none()


async def create_user(
    db: AsyncSession,
    email: str,
    username: str,
    hashed_password: str,
) -> User:
    user = User(email=email, username=username, hashed_password=hashed_password)
    db.add(user)
    await db.flush()
    return user


# ── Project V2 helpers ────────────────────────────────────────────────────────

async def update_project_container(
    db: AsyncSession,
    project_id: str,
    container_id: Optional[str],
    container_status: str,
    preview_url: Optional[str] = None,
) -> None:
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if project:
        project.container_id     = container_id
        project.container_status = container_status
        if preview_url is not None:
            project.preview_url = preview_url
        await db.flush()


async def update_project_opencode_session(
    db: AsyncSession,
    project_id: str,
    session_id: str,
) -> None:
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if project:
        project.opencode_session_id = session_id
        await db.flush()


async def update_project_workspace(
    db: AsyncSession,
    project_id: str,
    workspace_path: str,
) -> None:
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if project:
        project.workspace_path = workspace_path
        await db.flush()


# ── Showcase / gallery ────────────────────────────────────────────────────────

async def showcase_project(
    db: AsyncSession,
    project_id: str,
    user_id: str,
    showcase_name: Optional[str] = None,
    showcase_description: Optional[str] = None,
) -> Optional[Project]:
    """Mark a project as showcased (public gallery)."""
    result = await db.execute(
        select(Project).where(Project.id == project_id, Project.user_id == user_id)
    )
    project = result.scalar_one_or_none()
    if not project:
        return None
    project.showcased_at         = datetime.utcnow()
    project.showcase_name        = showcase_name or project.name
    project.showcase_description = showcase_description or project.description
    await db.flush()
    return project


async def unshowcase_project(
    db: AsyncSession,
    project_id: str,
    user_id: str,
) -> bool:
    """Remove a project from the showcase gallery."""
    result = await db.execute(
        select(Project).where(Project.id == project_id, Project.user_id == user_id)
    )
    project = result.scalar_one_or_none()
    if not project:
        return False
    project.showcased_at         = None
    project.showcase_name        = None
    project.showcase_description = None
    await db.flush()
    return True


async def list_user_showcases(db: AsyncSession, user_id: str) -> list[Project]:
    """Return all projects the given user has showcased."""
    result = await db.execute(
        select(Project)
        .where(Project.user_id == user_id, Project.showcased_at.isnot(None))
        .order_by(Project.showcased_at.desc())
    )
    return result.scalars().all()


async def list_all_showcases(db: AsyncSession) -> list[Project]:
    """Return all showcased projects across all users (public gallery)."""
    result = await db.execute(
        select(Project)
        .where(Project.showcased_at.isnot(None))
        .order_by(Project.showcased_at.desc())
    )
    return result.scalars().all()


async def update_project_thumbnail(
    db: AsyncSession,
    project_id: str,
    user_id: str,
    thumbnail_url: str,
) -> bool:
    """Set the thumbnail_url for a project."""
    result = await db.execute(
        select(Project).where(Project.id == project_id, Project.user_id == user_id)
    )
    project = result.scalar_one_or_none()
    if not project:
        return False
    project.thumbnail_url = thumbnail_url
    await db.flush()
    return True


async def clone_project(
    db: AsyncSession,
    source_project_id: str,
    new_user_id: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
) -> Optional[Project]:
    """
    Create a new project record that is a copy of an existing (showcased) project.
    Workspace files are NOT copied here — caller is responsible for copying the
    workspace directory on disk after this function returns.
    """
    result = await db.execute(
        select(Project)
        .options(selectinload(Project.files))
        .where(Project.id == source_project_id)
    )
    source = result.scalar_one_or_none()
    if not source:
        return None

    new_project = Project(
        user_id     = new_user_id,
        name        = name or source.name,
        description = description or source.description,
        tech_stack  = source.tech_stack,
        stack       = source.stack,
        run_command = source.run_command,
        setup_commands = source.setup_commands,
        current_version = 1,
    )
    db.add(new_project)
    await db.flush()

    # Copy current files
    for f in source.files:
        pf = ProjectFile(
            project_id  = new_project.id,
            path        = f.path,
            content     = f.content,
            language    = f.language,
            description = f.description,
        )
        db.add(pf)

    # Create initial version snapshot
    files_snapshot = [
        {"path": f.path, "content": f.content, "language": f.language, "description": f.description}
        for f in source.files
    ]
    version = FileVersion(
        project_id     = new_project.id,
        version_number = 1,
        instruction    = f"Cloned from {source.name}",
        files_snapshot = files_snapshot,
    )
    db.add(version)
    await db.flush()
    return new_project
