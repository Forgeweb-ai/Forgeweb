"""
forge/embeddings/indexer.py
============================
Chunks project files and creates vector embeddings for similarity search.

Primary: sentence-transformers all-MiniLM-L6-v2 (90MB, semantic search).
Fallback: hash-trick bag-of-words (no deps, keyword overlap, instant).

The model is loaded lazily on first call so startup stays fast.
"""

import re
import math
import time as _time
from typing import Optional

_model = None
_model_load_time: float | None = None
_use_keyword_fallback: bool = False   # set True once we confirm no ST

CHUNK_LINES = 40        # lines per chunk (overlapping by ~10)
OVERLAP_LINES = 10
MIN_CHUNK_CHARS = 30    # skip tiny chunks (just whitespace / empty lines)
MAX_FILES_TO_INDEX = 50  # cap indexing to keep it fast

# Keyword vector dimension — must match between index and query time
_KW_DIM = 384


def _keyword_vector(text: str) -> list[float]:
    """
    Hash-trick bag-of-words: convert text to a fixed-length float vector.
    Two independent hash projections halve collision probability.
    Returns an L2-normalised vector so cosine similarity still makes sense.
    """
    words = re.findall(r'\b[a-z][a-z0-9]*\b', text.lower())
    vec = [0.0] * _KW_DIM
    for word in words:
        # Skip common stop-words — they hurt precision
        if word in ('the', 'a', 'an', 'and', 'or', 'in', 'is', 'it',
                    'of', 'to', 'for', 'on', 'with', 'at', 'be', 'this',
                    'that', 'are', 'was', 'as', 'by', 'file', 'var', 'let',
                    'const', 'return', 'import', 'from', 'class', 'def'):
            continue
        h1 = hash(word)         % _KW_DIM
        h2 = hash(word + '\x00') % _KW_DIM
        vec[h1] += 1.0
        vec[h2] += 0.5   # second projection at half weight
    norm = math.sqrt(sum(x * x for x in vec))
    if norm > 0:
        vec = [x / norm for x in vec]
    return vec


def _get_model():
    """
    Return the sentence-transformer model, or None if unavailable.
    Sets _use_keyword_fallback = True on first ImportError so subsequent
    calls skip the import attempt and go straight to the keyword path.
    """
    global _model, _model_load_time, _use_keyword_fallback
    if _use_keyword_fallback:
        return None
    if _model is None:
        try:
            from sentence_transformers import SentenceTransformer
            print("[Embeddings] loading all-MiniLM-L6-v2 (first call — ~2-5s)…", flush=True)
            t0 = _time.perf_counter()
            _model = SentenceTransformer("all-MiniLM-L6-v2")
            _model_load_time = _time.perf_counter() - t0
            print(f"[Embeddings] model loaded in {_model_load_time:.1f}s", flush=True)
        except ImportError:
            print(
                "[Embeddings] sentence-transformers not installed — "
                "using keyword TF-IDF fallback (install with: pip install sentence-transformers)",
                flush=True,
            )
            _use_keyword_fallback = True
            return None
    return _model


def _chunk_file(path: str, content: str) -> list[dict]:
    """
    Split a file into overlapping line-window chunks.
    Returns list of {file_path, chunk_index, chunk_text, line_start, line_end}.
    """
    lines = content.splitlines()
    chunks = []
    idx = 0
    start = 0

    while start < len(lines):
        end = min(start + CHUNK_LINES, len(lines))
        text = "\n".join(lines[start:end]).strip()
        if len(text) >= MIN_CHUNK_CHARS:
            chunks.append({
                "file_path":   path,
                "chunk_index": idx,
                "chunk_text":  f"File: {path}\n\n{text}",
                "line_start":  start + 1,
                "line_end":    end,
            })
            idx += 1
        start += CHUNK_LINES - OVERLAP_LINES  # slide with overlap

    # Always include at least one chunk per file (the whole thing if small)
    if not chunks and content.strip():
        chunks.append({
            "file_path":   path,
            "chunk_index": 0,
            "chunk_text":  f"File: {path}\n\n{content.strip()}",
            "line_start":  1,
            "line_end":    len(lines),
        })

    return chunks


# File extensions that are worth indexing
_INDEXABLE = {
    ".ts", ".tsx", ".js", ".jsx", ".py", ".go", ".rs", ".java",
    ".cs", ".cpp", ".c", ".h", ".rb", ".php", ".swift", ".kt",
    ".vue", ".svelte", ".css", ".scss", ".html", ".json", ".yaml",
    ".yml", ".toml", ".md", ".env.example", ".sh",
}

def _is_indexable(path: str) -> bool:
    import os
    _, ext = os.path.splitext(path.lower())
    return ext in _INDEXABLE


def build_index(files: list[dict]) -> list[dict]:
    """
    Given a list of {path, content} dicts, returns chunks with embeddings.
    This is the main entry point — call it after a project is generated/updated.
    Returns list of {file_path, chunk_index, chunk_text, embedding, line_start, line_end}.

    Uses sentence-transformers when available, keyword hash-trick otherwise.
    """
    t0 = _time.perf_counter()
    model = _get_model()

    # Filter to indexable files, skip binary/huge files
    indexable = [
        f for f in files
        if _is_indexable(f.get("path", ""))
        and len(f.get("content", "")) < 200_000  # skip files > 200kb
    ][:MAX_FILES_TO_INDEX]

    print(
        f"[Embeddings] indexing {len(indexable)}/{len(files)} files "
        f"(skipped {len(files)-len(indexable)} non-indexable/huge)"
        f"  mode={'semantic' if model else 'keyword'}",
        flush=True,
    )

    if not indexable:
        return []

    # Build all chunks
    all_chunks = []
    for f in indexable:
        fc = _chunk_file(f["path"], f.get("content", ""))
        all_chunks.extend(fc)

    print(f"[Embeddings] {len(all_chunks)} chunks from {len(indexable)} files", flush=True)

    if not all_chunks:
        return []

    # ── Semantic embeddings (sentence-transformers) ────────────────────────────
    if model is not None:
        t1 = _time.perf_counter()
        texts = [c["chunk_text"] for c in all_chunks]
        embeddings = model.encode(texts, batch_size=64, show_progress_bar=False)
        t2 = _time.perf_counter()
        print(
            f"[Embeddings] encoded {len(texts)} chunks in {t2-t1:.2f}s  "
            f"(total build_index: {t2-t0:.2f}s)",
            flush=True,
        )
        for chunk, emb in zip(all_chunks, embeddings):
            chunk["embedding"] = emb.tolist()
    else:
        # ── Keyword fallback (hash-trick BoW) ─────────────────────────────────
        t1 = _time.perf_counter()
        for chunk in all_chunks:
            chunk["embedding"] = _keyword_vector(chunk["chunk_text"])
        print(
            f"[Embeddings] keyword-vectorized {len(all_chunks)} chunks "
            f"in {(_time.perf_counter()-t1)*1000:.0f}ms  "
            f"(total build_index: {_time.perf_counter()-t0:.2f}s)",
            flush=True,
        )

    return all_chunks


def embed_query(query: str) -> list[float]:
    """
    Embed a single search query string.
    Uses sentence-transformers when available, keyword hash-trick otherwise.
    """
    t0 = _time.perf_counter()
    model = _get_model()
    if model is not None:
        result = model.encode([query], show_progress_bar=False)[0].tolist()
        print(f"[Embeddings] query embedded (semantic) in {(_time.perf_counter()-t0)*1000:.0f}ms", flush=True)
    else:
        result = _keyword_vector(query)
        print(f"[Embeddings] query vectorized (keyword) in {(_time.perf_counter()-t0)*1000:.1f}ms", flush=True)
    return result
