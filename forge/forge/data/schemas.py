"""
forge/data/schemas.py
=====================
Pydantic models for all structured outputs.
These schemas are the contract between the model and the UI.
Version them carefully — breaking changes = major version bump.
"""

from pydantic import BaseModel, Field
from typing import Optional


# ── Codebase Generation ───────────────────────────────────────────────────────

class ProjectFile(BaseModel):
    path: str               = Field(..., description="Relative path, e.g. src/main.py")
    content: str            = Field(..., description="Full file content")
    description: str        = Field("", description="One-line description of this file")
    language: str           = Field("", description="Detected language, e.g. python")


class CodebaseOutput(BaseModel):
    project_name: str       = Field(..., description="Slug-style name, e.g. my-todo-app")
    description: str        = Field(..., description="One-sentence description")
    tech_stack: list[str]   = Field(default_factory=list)
    files: list[ProjectFile]
    setup_commands: list[str] = Field(default_factory=list)
    run_command: str        = Field("", description="Command to start the project")


# ── File Update ───────────────────────────────────────────────────────────────

class FileUpdateOutput(BaseModel):
    path: str
    content: str
    changes_summary: str    = Field("", description="What was changed")


# ── Training Data ─────────────────────────────────────────────────────────────

class TrainingExample(BaseModel):
    """One fine-tuning example in the Alpaca instruction format."""
    instruction: str        = Field(..., description="User's natural language request")
    input: str              = Field("", description="Optional extra context")
    output: str             = Field(..., description="Model's JSON response (CodebaseOutput)")
    metadata: dict          = Field(default_factory=dict, description="language, difficulty, source")


class TrainingDataset(BaseModel):
    version: str
    examples: list[TrainingExample]
    stats: dict             = Field(default_factory=dict)


# ── API Request/Response ──────────────────────────────────────────────────────

class StackConfig(BaseModel):
    """
    Selected frontend / backend / database combination.

    v1 scope: Next.js frontend only.
    BE and DB generation are disabled until v2 — options are kept here for forward compatibility.
    """
    fe: str = Field("nextjs", description="Frontend: nextjs (v1) — react | angular | vanilla coming in v2")
    # V2: fastapi | express | hono | spring
    be: str = Field("none",   description="Backend: none only in v1 — full BE generation coming in v2")
    # V2: sqlite | postgres | mongo
    db: str = Field("none",   description="Database: none only in v1 — DB generation coming in v2")


class GenerateRequest(BaseModel):
    prompt: str             = Field(..., min_length=1, max_length=8000)
    language: str           = Field("auto", description="Target language/framework")
    extra_context: str      = Field("", description="Additional constraints or existing project context for updates")
    stack: Optional[StackConfig] = Field(None, description="Explicit stack selection from the UI")
    image_base64: Optional[str] = Field(None, description="Base64-encoded reference design image (no data URL prefix)")
    image_type:   Optional[str] = Field(None, description="MIME type of the image, e.g. image/png")


class UpdateFileRequest(BaseModel):
    file_path: str
    current_content: str
    instruction: str        = Field(..., min_length=1, max_length=4000)
    full_codebase: list[dict] = Field(default_factory=list, description="Other files for context")


class ChatMessage(BaseModel):
    role: str               = Field(..., pattern="^(user|assistant|system)$")
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage]
    codebase_context: Optional[list[dict]] = None
    system_override:  Optional[str]        = None   # replaces default CHAT_SYSTEM_PROMPT
    runtime_context:  Optional[str]        = None   # appended after CHAT_SYSTEM_PROMPT — live app/runtime state


class IntentRequest(BaseModel):
    message:      str
    has_project:  bool = False
    project_name: str  = ""


# ── Run Request ───────────────────────────────────────────────────────────────

class RunFile(BaseModel):
    path:    str
    content: str

class RunRequest(BaseModel):
    files:          list[RunFile]
    run_command:    str            = Field("", description="AI-suggested run command")
    setup_commands: list[str]      = Field(default_factory=list, description="Commands to run before run_command (e.g. npm install)")
    env_vars:       dict[str, str] = Field(default_factory=dict, description="Extra env vars")
    run_timeout_s:  int            = Field(0, description="0 = run until process exits / stopped. >0 = kill after N seconds (for dev servers)")
    project_id:     Optional[str]  = Field(None, description="If provided, use persistent workspace instead of temp dir")


# ── DB / Persistence Schemas (v0.2) ──────────────────────────────────────────

class ProjectFileInput(BaseModel):
    """A single file as sent from the frontend when saving a project."""
    path:        str = Field(..., description="Relative file path, e.g. src/App.tsx")
    content:     str = Field("", description="Full file content")
    language:    str = Field("", description="Detected language")
    description: str = Field("", description="One-line description")


class SaveProjectRequest(BaseModel):
    """Save a freshly generated project to the DB."""
    name:           str
    description:    str               = ""
    tech_stack:     list[str]         = Field(default_factory=list)
    setup_commands: list[str]         = Field(default_factory=list)
    run_command:    str               = ""
    files:          list[ProjectFileInput]
    stack:          Optional[StackConfig] = None  # FE/BE/DB selection


class UpdateProjectFilesRequest(BaseModel):
    """Replace project files and create a new version snapshot."""
    instruction: str                   = Field("", description="What the user asked to change (used as version label)")
    files:       list[ProjectFileInput]


class UpdateProjectMetaRequest(BaseModel):
    """Update project metadata (run_command, setup_commands) without touching files."""
    run_command:    Optional[str]       = None
    setup_commands: Optional[list[str]] = None


class CreateConversationRequest(BaseModel):
    title:      str           = "New chat"
    project_id: Optional[str] = None


class AddMessageRequest(BaseModel):
    role:    str = Field(..., pattern="^(user|assistant|system)$")
    content: str


class ContextSearchRequest(BaseModel):
    """Find the most relevant files for an update instruction."""
    instruction: str
    top_k:       int = Field(4, ge=1, le=10)


class RestoreVersionRequest(BaseModel):
    """Restore a project to a previous version number."""
    version_number: int = Field(..., ge=1)
