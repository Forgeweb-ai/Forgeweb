"""
forge/embeddings/search.py
===========================
Cosine similarity search over stored embeddings.
Returns the most relevant files + line ranges for a given query.

This is what makes updates context-aware:
  1. User says "add dark mode to the header"
  2. We embed that query
  3. We find the top-k most similar chunks in the project
  4. We return those files (deduplicated) with their line ranges
  5. The LLM gets ONLY those files instead of the entire codebase
"""

import math
from typing import Optional


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Fast pure-Python cosine similarity for short vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def search_context(
    query_embedding: list[float],
    chunks: list,           # list of FileEmbedding ORM objects
    top_k_files: int = 3,
    top_k_chunks: int = 8,
    min_score: float = 0.15,
) -> list[dict]:
    """
    Find the most relevant files for a query.

    Returns a list of dicts sorted by relevance:
        {
          file_path: str,
          score: float,           # max cosine similarity across chunks
          relevant_lines: [       # most relevant line ranges
              {line_start, line_end, chunk_text, score}
          ]
        }

    Strategy:
    1. Score every chunk against the query
    2. Group by file_path, take the best score per file
    3. Return top_k_files files with their top chunks
    """
    if not chunks or not query_embedding:
        return []

    # Score all chunks
    scored = []
    for chunk in chunks:
        emb = chunk.embedding
        if not emb:
            continue
        score = _cosine_similarity(query_embedding, emb)
        scored.append({
            "file_path":  chunk.file_path,
            "score":      score,
            "line_start": chunk.line_start,
            "line_end":   chunk.line_end,
            "chunk_text": chunk.chunk_text,
        })

    # Filter low-confidence matches
    scored = [s for s in scored if s["score"] >= min_score]
    if not scored:
        return []

    # Group by file — keep all chunks per file
    file_chunks: dict[str, list[dict]] = {}
    for s in scored:
        file_chunks.setdefault(s["file_path"], []).append(s)

    # Sort within each file by score desc
    for path in file_chunks:
        file_chunks[path].sort(key=lambda x: x["score"], reverse=True)

    # Build per-file result: max score + top chunk lines
    file_results = []
    for path, file_chunk_list in file_chunks.items():
        top_score = file_chunk_list[0]["score"]
        relevant_lines = [
            {
                "line_start": c["line_start"],
                "line_end":   c["line_end"],
                "chunk_text": c["chunk_text"],
                "score":      round(c["score"], 4),
            }
            for c in file_chunk_list[:top_k_chunks]
        ]
        file_results.append({
            "file_path":      path,
            "score":          round(top_score, 4),
            "relevant_lines": relevant_lines,
        })

    # Sort files by best chunk score
    file_results.sort(key=lambda x: x["score"], reverse=True)
    return file_results[:top_k_files]


def get_context_files(
    query_embedding: list[float],
    chunks: list,
    all_files: list[dict],
    top_k: int = 3,
) -> list[dict]:
    """
    High-level helper used by the update endpoint.
    Returns a list of {path, content} for the top-k most relevant files.
    These are passed to the LLM instead of the full codebase.
    """
    results = search_context(query_embedding, chunks, top_k_files=top_k)
    relevant_paths = {r["file_path"] for r in results}

    # Always include config / root files that may be relevant for any change
    config_patterns = {
        "tailwind.config", "vite.config", "next.config", "package.json",
        "tsconfig", ".env", "globals.css", "index.css", "App.",
    }

    context_files = []
    for f in all_files:
        path = f.get("path", "")
        if path in relevant_paths:
            context_files.append({"path": path, "content": f.get("content", "")})
        elif any(pat in path for pat in config_patterns):
            context_files.append({"path": path, "content": f.get("content", "")})

    # Deduplicate by path
    seen = set()
    unique = []
    for f in context_files:
        if f["path"] not in seen:
            seen.add(f["path"])
            unique.append(f)

    return unique
