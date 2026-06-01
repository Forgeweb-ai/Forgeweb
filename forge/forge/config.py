"""
forge/config.py
===============
Central config. Every setting flows through here.
Swap models, change server, toggle telemetry — one file.
"""

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass
class ModelConfig:
    backend: str        = os.getenv("MODEL_BACKEND", "together")

    # Generic AI provider switch. Use with MODEL_BACKEND=ai.
    # Supported AI_PROVIDER values:
    #   kimi / moonshot -> Moonshot AI native Kimi API (fast, cheaper than Together)
    #   grok / xai      -> xAI Grok API
    #   deepseek        -> DeepSeek API
    #   openai          -> OpenAI / ChatGPT API (alias: chatgpt)
    #   anthropic       -> Anthropic Claude Messages API (alias: claude)
    #   gemini          -> Google Gemini OpenAI-compatible API
    #   together        -> Together AI OpenAI-compatible API
    #   custom          -> any OpenAI-compatible endpoint (set AI_BASE_URL)
    # API key is auto-resolved from provider-specific env vars (XAI_API_KEY,
    # DEEPSEEK_API_KEY, GEMINI_API_KEY, QWEN_API_KEY, etc.). AI_API_KEY is a
    # generic fallback for custom/proxy setups.
    ai_provider: str    = os.getenv("AI_PROVIDER", os.getenv("MODEL_PROVIDER", "kimi"))
    ai_api_key: str     = os.getenv("AI_API_KEY", "")
    ai_model: str       = os.getenv("AI_MODEL", "")
    ai_base_url: str    = os.getenv("AI_BASE_URL", "")
    ai_thinking: str    = os.getenv("AI_THINKING", "disabled")
    # Optional: override which model is used when an image is passed (vision tasks).
    # If blank, the provider's built-in vision model default is used.
    # Example: AI_VISION_MODEL=qwen-vl-max
    ai_vision_model: str = os.getenv("AI_VISION_MODEL", "")

    # ── Tiered model routing — optional per-task overrides ───────────────────
    # Format: "provider/model"  e.g. "gemini/gemini-2.5-flash-lite"
    #                                 "together/Qwen/Qwen2.5-Coder-32B-Instruct"
    # Leave blank to fall back to AI_PROVIDER + AI_MODEL for that task.
    #
    # INTENT  — one-word classifier (chat/build/update); runs on every message.
    #            Use the cheapest/fastest thing available (free-tier Gemini is ideal).
    # CHAT    — conversational replies when the user asks questions.
    # PLAN    — architectural planning (Phase 1 of code generation).
    # CODEGEN — file code generation (Phase 2, streaming).
    # CONTEXT — project context summarisation stored in DB after each build.
    intent_model:  str = os.getenv("INTENT_MODEL",  "")
    chat_model:    str = os.getenv("CHAT_MODEL",    "")
    plan_model:    str = os.getenv("PLAN_MODEL",    "")
    codegen_model: str = os.getenv("CODEGEN_MODEL", "")
    context_model: str = os.getenv("CONTEXT_MODEL", "")


    # Together AI
    together_api_key: str  = os.getenv("TOGETHER_API_KEY", "")
    together_model: str    = os.getenv(
        "TOGETHER_MODEL",
        # Real model slugs on Together AI as of 2025-Q2:
        #   moonshotai/Kimi-K2-Instruct           ← default (great for code)
        #   moonshotai/Kimi-K2-Instruct-0905      (newer Kimi)
        #   Qwen/Qwen2.5-Coder-32B-Instruct       (smaller, very strong at code)
        #   deepseek-ai/DeepSeek-V3
        #   meta-llama/Llama-3.3-70B-Instruct-Turbo
        # `moonshotai/Kimi-K2.6` and `deepseek-ai/DeepSeek-V4-Pro` are NOT real
        # — they will 500 on every call.
        "moonshotai/Kimi-K2-Instruct",
    )
    max_tokens: int        = int(os.getenv("MAX_TOKENS", "16384"))
    temperature: float     = float(os.getenv("TEMPERATURE", "0.2"))
    stream: bool           = True

    # Local llama.cpp
    local_model_path: str  = os.getenv("LOCAL_MODEL_PATH", "./models/forge-phi3.5-Q4_K_M.gguf")
    local_context: int     = int(os.getenv("LOCAL_CONTEXT_SIZE", "8192"))
    local_threads: int     = int(os.getenv("LOCAL_THREADS", "4"))
    local_gpu_layers: int  = int(os.getenv("LOCAL_GPU_LAYERS", "0"))


@dataclass
class DataConfig:
    teacher_model: str     = os.getenv(
        "TEACHER_MODEL",
        "moonshotai/Kimi-K2-Instruct",   # See ModelConfig comments above.
    )
    teacher_api_key: str   = os.getenv("TEACHER_API_KEY", "")
    output_dir: str        = "./data/generated"
    batch_size: int        = 10
    n_samples: int         = 50_000
    seed: int              = 42


@dataclass
class TrainingConfig:
    model_id: str          = "microsoft/Phi-3.5-mini-instruct"
    output_dir: str        = "./models/forge-phi3.5-lora"
    max_seq_length: int    = 4096
    lora_r: int            = 16
    lora_alpha: int        = 32
    lora_dropout: float    = 0.05
    batch_size: int        = 4
    grad_accum: int        = 4         # effective batch = 16
    lr: float              = 2e-4
    epochs: int            = 3
    warmup_ratio: float    = 0.05
    scheduler: str         = "cosine"
    bf16: bool             = True
    save_steps: int        = 500
    eval_steps: int        = 500
    logging_steps: int     = 50


@dataclass
class ServerConfig:
    host: str              = os.getenv("HOST", "0.0.0.0")
    port: int              = int(os.getenv("PORT", "8000"))
    debug: bool            = os.getenv("DEBUG", "true").lower() == "true"
    cors_origins: list     = field(default_factory=lambda: ["*"])


@dataclass
class TelemetryConfig:
    enabled: bool          = os.getenv("TELEMETRY_ENABLED", "false").lower() == "true"
    endpoint: str          = os.getenv("TELEMETRY_ENDPOINT", "")


@dataclass
class DatabaseConfig:
    url: str = os.getenv(
        "DATABASE_URL",
        "postgresql+asyncpg://postgres:postgres@localhost:5432/forge"
    )


@dataclass
class ForgeConfig:
    model: ModelConfig         = field(default_factory=ModelConfig)
    data: DataConfig           = field(default_factory=DataConfig)
    training: TrainingConfig   = field(default_factory=TrainingConfig)
    server: ServerConfig       = field(default_factory=ServerConfig)
    telemetry: TelemetryConfig = field(default_factory=TelemetryConfig)
    db: DatabaseConfig         = field(default_factory=DatabaseConfig)

    # V2: multi-tenant storage + OpenCode + Docker
    forge_data_root: str  = os.getenv("FORGE_DATA_ROOT", "./local-data")
    opencode_url: str     = os.getenv("OPENCODE_URL", "http://localhost:7777")
    preview_domain: str   = os.getenv("PREVIEW_DOMAIN", "preview.localhost")

    # FORGE_MODE: 'dev' = BYOK full config UI; 'prod' = platform handles AI keys
    # In dev mode, the SettingsModal lets users configure any OpenCode provider.
    # In prod mode, AI configuration is hidden (platform owns the keys).
    forge_mode: str       = os.getenv("FORGE_MODE", "dev")

    # Product version — bump this on every release
    version: str = "0.2.0"
    product_name: str = "Forge"


# ── Singleton ─────────────────────────────────────────────────────────────────
config = ForgeConfig()
