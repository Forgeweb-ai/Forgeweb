"""
tests/test_surgical_edit.py
============================
Verifies that the surgical-edit system correctly applies targeted patches
to individual files instead of falling back to full rewrites.

What this tests
---------------
1. Single-file edit: a prompt that only changes one function in one file
   produces exactly ONE modified file (not a full project regeneration).
2. Multi-file edit: a prompt touching two known files produces exactly
   those two modified files (no accidental rewrites of untouched files).
3. Surgical matching: the applied content is different from the original
   (i.e. the edits actually landed) and the file doesn't get fully rewritten.
4. No orphan artefacts: only the files mentioned in the prompt are touched.

How it works
------------
Uses get_backend() — the same factory the server uses — so whichever
MODEL_BACKEND / AI_PROVIDER is set in .env is what the tests call.
No hardcoded provider.

We call backend.generate_codebase() directly (no HTTP server) with
an "UPDATE EXISTING PROJECT:" extra_context string — the exact same format
that Composer.tsx sends.  We stream SSE tokens, parse FILE_START/FILE_DONE
events, and assert on which files changed and what their content looks like.

Run with:
    cd forge
    python tests/test_surgical_edit.py             # standalone
    python -m pytest tests/test_surgical_edit.py -v
"""

import sys
import os
import asyncio
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from forge.model import get_backend

# ─────────────────────────────────────────────────────────────────────────────
# Synthetic project fixture
# ─────────────────────────────────────────────────────────────────────────────

ORIGINAL_UTILS = """\
// utils.ts
export function add(a: number, b: number): number {
  return a + b
}

export function subtract(a: number, b: number): number {
  return a - b
}

export function multiply(a: number, b: number): number {
  return a * b
}
"""

ORIGINAL_APP = """\
// App.tsx
import { add, subtract } from './utils'

export default function App() {
  const result = add(1, 2)
  return <div>{result}</div>
}
"""

ORIGINAL_README = """\
# My App

A simple TypeScript app.
"""

PROJECT_NAME = "Surgical Test App"

PROJECT_FILES = {
    "utils.ts":  ORIGINAL_UTILS,
    "App.tsx":   ORIGINAL_APP,
    "README.md": ORIGINAL_README,
}


def build_extra_context(files_to_include: list[str] | None = None) -> str:
    """
    Build the extra_context string in the exact format Composer.tsx sends.
    files_to_include: subset of PROJECT_FILES to embed as editable context.
                      If None, all project files are included.
    """
    include = files_to_include or list(PROJECT_FILES.keys())
    all_paths = list(PROJECT_FILES.keys())

    parts = [f"UPDATE EXISTING PROJECT:\nProject: {PROJECT_NAME}\n\nFiles to update\n"]
    for path in include:
        content = PROJECT_FILES[path]
        parts.append(f"// FILE: {path}\n{content}\n\n---\n\n")
    parts.append(f"All file paths in project (for reference):\n{', '.join(all_paths)}")
    return "".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# SSE collector
# ─────────────────────────────────────────────────────────────────────────────

async def collect_update(prompt: str, files_to_include: list[str] | None = None) -> dict:
    """
    Run generate_codebase in UPDATE mode and collect results.
    Returns a dict with:
        changed_files:  dict[path, content]  — files whose tokens were streamed
        fallback_count: int                  — "Falling back to full rewrite" hits
        skipped_msgs:   list[str]            — STATUS lines mentioning skipped edits
        result_meta:    dict | None          — __FORGE_RESULT__ payload
    """
    backend      = get_backend()
    extra_ctx    = build_extra_context(files_to_include)

    changed_files: dict[str, list[str]] = {}
    current_file: str | None            = None
    fallback_count = 0
    skipped_msgs: list[str] = []
    result_meta = None

    async for token in backend.generate_codebase(
        prompt=prompt,
        extra_context=extra_ctx,
    ):
        if token.startswith("__FORGE_FILE_START__:"):
            payload      = json.loads(token[len("__FORGE_FILE_START__:"):])
            current_file = payload["path"]
            changed_files.setdefault(current_file, [])

        elif token.startswith("__FORGE_FILE_DONE__:"):
            current_file = None

        elif token.startswith("__FORGE_RESULT__:"):
            result_meta  = json.loads(token[len("__FORGE_RESULT__:"):])
            current_file = None

        elif token.startswith("__FORGE_STATUS__:"):
            msg = token[len("__FORGE_STATUS__:"):]
            if "falling back to full rewrite" in msg.lower():
                fallback_count += 1
            if "skipped" in msg.lower():
                skipped_msgs.append(msg)

        elif token.startswith("__FORGE_"):
            pass  # other meta — ignore for content

        elif current_file is not None:
            changed_files[current_file].append(token)

    return {
        "changed_files":  {p: "".join(chunks) for p, chunks in changed_files.items()},
        "fallback_count": fallback_count,
        "skipped_msgs":   skipped_msgs,
        "result_meta":    result_meta,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Assertion helpers
# ─────────────────────────────────────────────────────────────────────────────

PASS_SYM = "\033[32m✓\033[0m"
FAIL_SYM = "\033[31m✗\033[0m"
_failures: list[str] = []


def check(label: str, condition: bool, detail: str = ""):
    if condition:
        print(f"  {PASS_SYM} {label}")
    else:
        msg = f"  {FAIL_SYM} {label}"
        if detail:
            msg += f"\n      → {detail}"
        print(msg)
        _failures.append(label)


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────

async def test_single_file_surgical_edit():
    """Renaming a function in utils.ts should touch ONLY utils.ts."""
    print("\n[Test 1] Single-file surgical edit — rename function")

    result = await collect_update(
        prompt="In utils.ts rename the `add` function to `sum`. Keep everything else identical.",
        files_to_include=["utils.ts"],
    )

    changed = result["changed_files"]
    check("Exactly one file changed", len(changed) == 1,
          f"changed: {list(changed.keys())}")
    check("utils.ts is the changed file", "utils.ts" in changed,
          f"changed: {list(changed.keys())}")
    check("App.tsx was NOT touched", "App.tsx" not in changed)
    check("README.md was NOT touched", "README.md" not in changed)
    check("No fallback rewrite", result["fallback_count"] == 0,
          f"fallback_count={result['fallback_count']}")

    if "utils.ts" in changed:
        content = changed["utils.ts"]
        check("Output differs from original",
              content.strip() != ORIGINAL_UTILS.strip(),
              "content unchanged — edit may not have applied")
        check("'sum' function appears in output",
              "function sum" in content or "export function sum" in content,
              f"snippet: {content[:300]}")


async def test_multi_file_surgical_edit():
    """Editing two files should produce exactly two changed files."""
    print("\n[Test 2] Multi-file surgical edit — two files")

    result = await collect_update(
        prompt=(
            "1. In utils.ts add a new exported `divide(a, b)` function. "
            "2. In App.tsx import `divide` from './utils' and use it to display 10/2."
        ),
        files_to_include=["utils.ts", "App.tsx"],
    )

    changed = result["changed_files"]
    check("Exactly two files changed", len(changed) == 2,
          f"changed: {list(changed.keys())}")
    check("utils.ts changed", "utils.ts" in changed)
    check("App.tsx changed", "App.tsx" in changed)
    check("README.md was NOT touched", "README.md" not in changed)
    check("No fallback rewrite", result["fallback_count"] == 0,
          f"fallback_count={result['fallback_count']}")

    if "utils.ts" in changed:
        check("'divide' added to utils.ts",
              "divide" in changed["utils.ts"],
              f"snippet: {changed['utils.ts'][:300]}")
    if "App.tsx" in changed:
        check("'divide' used in App.tsx",
              "divide" in changed["App.tsx"],
              f"snippet: {changed['App.tsx'][:300]}")


async def test_content_actually_changed():
    """The output file must be different from the original."""
    print("\n[Test 3] Content is actually mutated by surgical edit")

    result = await collect_update(
        prompt="Add a `square(n: number): number` function to utils.ts.",
        files_to_include=["utils.ts"],
    )

    changed = result["changed_files"]
    if "utils.ts" in changed:
        new_content = changed["utils.ts"]
        check("Output differs from original",
              new_content.strip() != ORIGINAL_UTILS.strip(),
              "content was identical — edit did not apply")
        check("'square' appears in output",
              "square" in new_content,
              f"snippet: {new_content[:300]}")
    else:
        check("utils.ts was changed", False,
              f"changed: {list(changed.keys())}")

    check("No fallback rewrite", result["fallback_count"] == 0,
          f"fallback_count={result['fallback_count']}")


async def test_no_orphan_files():
    """A single-file edit must not silently emit other files."""
    print("\n[Test 4] No phantom file emissions")

    result = await collect_update(
        prompt=(
            "In README.md change 'A simple TypeScript app.' to "
            "'A simple TypeScript application.'"
        ),
        files_to_include=["README.md"],
    )

    changed = result["changed_files"]
    check("Only README.md changed",
          set(changed.keys()).issubset({"README.md"}),
          f"changed: {list(changed.keys())}")
    check("README.md is in changed files", "README.md" in changed,
          f"changed: {list(changed.keys())}")

    if "README.md" in changed:
        check("'application' appears in README",
              "application" in changed["README.md"],
              f"content: {changed['README.md']}")

    check("No fallback rewrite", result["fallback_count"] == 0,
          f"fallback_count={result['fallback_count']}")


# ─────────────────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────────────────

async def main():
    print("=" * 60)
    print("  Forge Surgical Edit Test Suite")
    print("=" * 60)

    await test_single_file_surgical_edit()
    await test_multi_file_surgical_edit()
    await test_content_actually_changed()
    await test_no_orphan_files()

    print("\n" + "=" * 60)
    if _failures:
        print(f"  {FAIL_SYM} {len(_failures)} check(s) FAILED:")
        for f in _failures:
            print(f"    • {f}")
        sys.exit(1)
    else:
        print(f"  {PASS_SYM} All checks passed")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
