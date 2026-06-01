"""
forge/data/generate.py
======================
Synthetic training data pipeline.
Uses Llama 3.1 405B (teacher) to generate high-quality instruction→codebase pairs.
This is how we fine-tune Phi-3.5 Mini to become Forge.

Usage:
  python -m forge.data.generate --n 1000 --output data/generated/batch_001.jsonl

Run this on a cloud machine (free Colab, RunPod) — it calls Together AI API.
Expected cost: ~$5 per 10,000 examples using Llama 3.1 8B as teacher.
              ~$50 per 10,000 examples using Llama 3.1 405B as teacher.
"""

import json
import asyncio
import argparse
import random
import time
from pathlib import Path
from tqdm import tqdm
import httpx
from forge.config import config


# ── Prompt seeds — diverse, real-world scenarios ─────────────────────────────

PROMPT_SEEDS = {
    "python": [
        "REST API with FastAPI, JWT auth, and SQLite database",
        "CLI tool that converts CSV files to JSON and vice versa",
        "Web scraper that extracts product prices from an e-commerce site",
        "Discord bot that tracks server activity and posts daily summaries",
        "File watcher that auto-compresses images when added to a folder",
        "Simple key-value store with persistence to disk",
        "Task queue system with priority support",
        "Password manager CLI with AES encryption",
    ],
    "javascript": [
        "Todo app with React, local storage persistence, and dark mode",
        "Weather dashboard that fetches data from OpenWeatherMap API",
        "Markdown editor with live preview using vanilla JS",
        "Expense tracker with charts using Chart.js",
        "Pomodoro timer with notifications and session history",
        "URL shortener with custom aliases using Node.js and Express",
        "Real-time chat UI with WebSocket support",
    ],
    "typescript": [
        "Next.js blog with MDX support and syntax highlighting",
        "Type-safe API client generator from OpenAPI spec",
        "Zustand state management example with persistence",
        "Form validation library with Zod integration",
    ],
    "go": [
        "HTTP server with middleware, routing, and graceful shutdown",
        "CLI tool to monitor system metrics (CPU, memory, disk)",
        "Simple load balancer with round-robin and health checks",
    ],
    "swift": [
        "iOS todo app with CoreData persistence and iCloud sync",
        "SwiftUI weather app with location services",
    ],
    "mobile_web": [
        "Mobile-first landing page with PWA support and offline mode",
        "Touch-friendly image gallery with swipe gestures",
        "Mobile form with multi-step wizard and validation",
    ],
}

DIFFICULTY_MODIFIERS = [
    "",
    " with comprehensive error handling",
    " with unit tests",
    " with Docker support",
    " with environment variable configuration",
    " with logging and monitoring",
    " optimized for mobile performance",
    " with rate limiting",
]


def build_prompt(seed: str, language: str) -> str:
    modifier = random.choice(DIFFICULTY_MODIFIERS)
    return f"{seed}{modifier}"


TEACHER_SYSTEM = """You are an expert software engineer. Generate a complete, production-ready codebase in response to the user's request.

Respond ONLY with valid JSON matching this exact schema — no markdown, no explanation:
{
  "project_name": "Human Readable Title (e.g. 'Personal Finance Dashboard') — NOT kebab-case",
  "description": "One sentence description",
  "tech_stack": ["list", "of", "technologies"],
  "files": [
    {
      "path": "relative/path/to/file",
      "content": "complete file content here",
      "description": "what this file does"
    }
  ],
  "setup_commands": ["pip install -r requirements.txt"],
  "run_command": "python main.py"
}

Generate COMPLETE working code. No placeholders. No TODOs."""


async def generate_one(
    prompt: str,
    client: httpx.AsyncClient,
    teacher_model: str,
    api_key: str,
) -> dict | None:
    """Call the teacher model and return a training example."""
    try:
        resp = await client.post(
            "https://api.together.xyz/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": teacher_model,
                "messages": [
                    {"role": "system", "content": TEACHER_SYSTEM},
                    {"role": "user",   "content": f"Build: {prompt}"},
                ],
                "max_tokens": 6000,
                "temperature": 0.3,
            },
            timeout=90,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"].strip()

        # Validate it's parseable JSON
        json.loads(content)

        return {
            "instruction": f"Build: {prompt}",
            "input": "",
            "output": content,
            "metadata": {
                "teacher_model": teacher_model,
                "prompt_seed": prompt,
                "timestamp": time.time(),
            }
        }
    except Exception as e:
        return None  # Skip failed examples — quality > quantity


async def generate_batch(
    prompts: list[str],
    output_path: Path,
    teacher_model: str,
    api_key: str,
    concurrency: int = 5,
) -> int:
    """Generate a batch of examples with controlled concurrency."""
    semaphore = asyncio.Semaphore(concurrency)
    written = 0

    async def _guarded(prompt: str, client: httpx.AsyncClient):
        async with semaphore:
            return await generate_one(prompt, client, teacher_model, api_key)

    output_path.parent.mkdir(parents=True, exist_ok=True)

    async with httpx.AsyncClient() as client:
        tasks = [_guarded(p, client) for p in prompts]
        with open(output_path, "a") as f:
            for coro in tqdm(asyncio.as_completed(tasks), total=len(tasks), desc="Generating"):
                result = await coro
                if result:
                    f.write(json.dumps(result) + "\n")
                    written += 1

    return written


def build_prompt_list(n: int) -> list[str]:
    """Build a diverse list of n prompts from seeds."""
    all_prompts = []
    for lang, seeds in PROMPT_SEEDS.items():
        for seed in seeds:
            all_prompts.append(build_prompt(seed, lang))

    # Repeat and shuffle to reach n
    multiplied = (all_prompts * ((n // len(all_prompts)) + 2))[:n]
    random.shuffle(multiplied)
    return multiplied


async def main(n: int, output: str, concurrency: int):
    api_key = config.data.teacher_api_key
    teacher = config.data.teacher_model

    if not api_key:
        print("ERROR: Set TEACHER_API_KEY in your .env file")
        return

    print(f"\n🔥 Forge Data Generator")
    print(f"   Teacher model : {teacher}")
    print(f"   Generating    : {n} examples")
    print(f"   Output        : {output}")
    print(f"   Concurrency   : {concurrency}\n")

    prompts = build_prompt_list(n)
    written = await generate_batch(
        prompts, Path(output), teacher, api_key, concurrency
    )

    print(f"\n✅ Done! Generated {written}/{n} examples → {output}")
    print(f"   Success rate: {written/n*100:.1f}%")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate Forge training data")
    parser.add_argument("--n",           type=int,  default=1000,                          help="Number of examples")
    parser.add_argument("--output",      type=str,  default="data/generated/batch_001.jsonl")
    parser.add_argument("--concurrency", type=int,  default=5,                             help="Parallel API calls")
    args = parser.parse_args()

    asyncio.run(main(args.n, args.output, args.concurrency))
