"""
forge_server/runner/log_parser.py
==================================
Shared log-error signature matcher used by:
  - api/verify_routes.py   — one-shot parse of a log tail
  - runner/log_watcher.py  — continuous parse of streaming docker logs

Why this lives in runner/ instead of api/:
  - The watcher imports it from runner/log_watcher.py at module load time. If
    the parser lived in api/verify_routes.py we'd get an import cycle when
    the watcher (started by container_manager) needs the parser, and the
    api router (which depends on container_manager) needs the parser too.
  - Pure functions, no FastAPI deps — easy to unit-test in isolation.

Signature strings are stable identifiers. The verify subagent uses them as
budget keys ("same signature 5x → escalate"), and the FE will eventually
key off them for visual error chips. Adding a new signature is safe;
renaming an existing one is a breaking change.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class LogError:
    signature: str       # stable id, e.g. "missing_module"
    detail:    str       # captured group (module name, error msg, etc.)
    line:      str       # original log line, truncated to 500 chars


# Each tuple: (regex, signature). First match wins for a given line.
# Order matters — more specific patterns before more general ones.
_SIGNATURES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"Cannot find module ['\"]([^'\"]+)['\"]"),              "missing_module"),
    (re.compile(r"Module not found: Can't resolve ['\"]([^'\"]+)['\"]"), "missing_module"),
    (re.compile(r"EADDRINUSE.*?:(\d+)"),                                 "port_in_use"),
    (re.compile(r"ECONNREFUSED"),                                        "connection_refused"),
    (re.compile(r"drizzle-kit.*?error", re.IGNORECASE),                  "drizzle_error"),
    (re.compile(r"Please install latest version of drizzle-orm"),        "drizzle_version_mismatch"),
    (re.compile(r"TS\d{4}:\s*(.+)"),                                     "ts_compile_error"),
    (re.compile(r"SyntaxError:\s*(.+)"),                                 "syntax_error"),
    (re.compile(r"UnhandledPromiseRejection"),                           "unhandled_rejection"),
    (re.compile(r"Error \[ERR_REQUIRE_ESM\]"),                           "esm_require_error"),
    (re.compile(r"Hydration failed because"),                            "hydration_mismatch"),
    (re.compile(r"⨯\s+(.+)"),                                            "next_runtime_error"),
]

# Lines containing any of these are noise — dropped before matching.
_NOISE: list[re.Pattern[str]] = [
    re.compile(r"\bwarning\b", re.IGNORECASE),
    re.compile(r"\bdeprecated\b", re.IGNORECASE),
    re.compile(r"^\s*$"),
    re.compile(r"^npm notice"),
]


def is_noise(line: str) -> bool:
    return any(p.search(line) for p in _NOISE)


def match_line(line: str) -> LogError | None:
    """Single-line classification. Returns None for noise + unknown lines."""
    if is_noise(line):
        return None
    for pattern, sig in _SIGNATURES:
        m = pattern.search(line)
        if m:
            detail = m.group(1) if m.groups() else ""
            return LogError(signature=sig, detail=detail, line=line[:500])
    return None


def parse_tail(logs: str) -> list[LogError]:
    """
    One-shot parse of a log tail. Returns at most one entry per unique
    (signature, detail) pair — keeping the MOST RECENT occurrence, since
    newer lines describe current container state.
    """
    seen: dict[tuple[str, str], LogError] = {}
    for raw in logs.splitlines():
        err = match_line(raw.rstrip())
        if err is None:
            continue
        seen[(err.signature, err.detail)] = err
    return list(seen.values())
