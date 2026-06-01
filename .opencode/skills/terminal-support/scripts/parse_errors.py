#!/usr/bin/env python3
"""parse_errors.py — classify a captured terminal log into a verdict opencode
can act on.

Usage:
    python3 parse_errors.py --exit-code <N> [--log <path>]
    cat run.log | python3 parse_errors.py --exit-code <N>

Emits a single JSON object on stdout:
    {
      "verdict": "OK" | "FIXABLE" | "FATAL",
      "exit_code": <int>,
      "category": "<dotted.category>",
      "evidence": "<the matched line>",
      "suggested_fix": "<short imperative>",
      "files_to_edit": [<paths or globs>],
      "retry_command": "<command or empty>"
    }
"""
from __future__ import annotations
import argparse, json, re, sys, os
from dataclasses import dataclass, field
from typing import List, Optional, Pattern


@dataclass
class Rule:
    category: str
    patterns: List[Pattern]
    suggested_fix: str
    files_to_edit: List[str] = field(default_factory=list)
    retry_command: str = ""


def _r(*patterns: str) -> List[Pattern]:
    return [re.compile(p, re.IGNORECASE | re.MULTILINE) for p in patterns]


# Order matters: more specific rules first. The first match wins.
RULES: List[Rule] = [
    # ---- Node / JS ----
    Rule(
        category="node.missing_dependency",
        patterns=_r(
            r"Cannot find module ['\"]([^'\"]+)['\"]",
            r"Module not found: Can't resolve ['\"]([^'\"]+)['\"]",
            r"ERR_MODULE_NOT_FOUND",
        ),
        suggested_fix="Add the missing module to package.json dependencies, then reinstall.",
        files_to_edit=["package.json"],
        retry_command="",  # caller decides install vs build
    ),
    Rule(
        category="node.version_mismatch",
        patterns=_r(
            r'engine ["\']node["\'] is incompatible',
            r"Unsupported engine",
            r"The engine \"node\" is incompatible",
        ),
        suggested_fix="Update engines.node in package.json or the node base image in the Dockerfile.",
        files_to_edit=["package.json", "Dockerfile"],
    ),
    Rule(
        category="node.typescript_error",
        patterns=_r(
            r"error TS\d{3,5}:",
            r"Type ['\"].+?['\"] is not assignable to type",
            r"Property '.+?' does not exist on type",
        ),
        suggested_fix="Open the cited file:line and fix the type error.",
    ),
    Rule(
        category="node.syntax",
        patterns=_r(r"SyntaxError: ", r"Unexpected token"),
        suggested_fix="Open the cited file:line and fix the syntax error.",
    ),

    # ---- Python ----
    Rule(
        category="python.missing_module",
        patterns=_r(
            r"ModuleNotFoundError: No module named ['\"]([^'\"]+)['\"]",
            r"ImportError: No module named ['\"]?([^\s'\"]+)",
        ),
        suggested_fix="Add the missing module to requirements.txt or pyproject.toml, then reinstall.",
        files_to_edit=["requirements.txt", "pyproject.toml"],
    ),
    Rule(
        category="python.syntax",
        patterns=_r(r"SyntaxError: ", r"IndentationError: "),
        suggested_fix="Open the cited file:line and fix the syntax error.",
    ),

    # ---- Docker ----
    Rule(
        category="docker.port_in_use",
        patterns=_r(
            r"port is already allocated",
            r"bind: address already in use",
            r"Bind for [\d.:]+ failed: port is already allocated",
        ),
        suggested_fix="Change the host port mapping in docker-compose.yml (left side of host:container).",
        files_to_edit=["docker-compose.yml", "docker-compose.yaml", "compose.yml"],
    ),
    Rule(
        category="docker.image_pull",
        patterns=_r(
            r"pull access denied",
            r"manifest unknown",
            r"not found: manifest unknown",
            r"repository does not exist",
        ),
        suggested_fix="Fix the image name or tag in the Dockerfile / compose file.",
        files_to_edit=["Dockerfile", "docker-compose.yml"],
    ),
    Rule(
        category="docker.build_failed",
        patterns=_r(
            r"failed to solve:",
            r"executor failed running",
            r"returned a non-zero code",
        ),
        suggested_fix="Open the Dockerfile at the failing step (cited above) and fix the command.",
        files_to_edit=["Dockerfile"],
    ),
    Rule(
        category="docker.network_missing",
        patterns=_r(r"network .+ not found", r"Could not find network"),
        suggested_fix="Recreate the network with `docker network create <name>` or fix the networks: block in compose.",
        files_to_edit=["docker-compose.yml"],
    ),
    Rule(
        category="docker.oom",
        patterns=_r(r"\bKilled\b", r"Out of memory", r"exit code 137"),
        suggested_fix="Increase the container's memory limit, or shrink the build context / install footprint.",
        files_to_edit=["docker-compose.yml", "Dockerfile"],
    ),

    # ---- Database / network ----
    Rule(
        category="db.connection_refused",
        patterns=_r(
            r"ECONNREFUSED",
            r"could not connect to server",
            r"connection refused",
            r"FATAL:  password authentication failed",
        ),
        suggested_fix="Check depends_on/healthcheck ordering and DB env vars (host, port, user, password).",
        files_to_edit=["docker-compose.yml", ".env"],
    ),

    # ---- Env / config ----
    Rule(
        category="env.missing_var",
        patterns=_r(
            r"environment variable .+ is not set",
            r"KeyError: ['\"]([A-Z_][A-Z0-9_]*)['\"]",
            r"required environment variable ['\"]?([A-Z_][A-Z0-9_]*)['\"]? (?:is )?(?:not set|missing)",
        ),
        suggested_fix="Add the missing variable to .env and document it in .env.example.",
        files_to_edit=[".env", ".env.example"],
    ),

    # ---- Permissions ----
    Rule(
        category="permission.denied",
        patterns=_r(r"EACCES", r"Permission denied"),
        suggested_fix="Fix file mode, Dockerfile USER, or the bind-mount permissions.",
        files_to_edit=["Dockerfile", "docker-compose.yml"],
    ),
]


# Fatal categories — host-level failures opencode should not loop on.
FATAL_PATTERNS = _r(
    r"Cannot connect to the Docker daemon",
    r"docker: command not found",
    r"no space left on device",
    r"failed to register layer.*no space",
)


def classify(log: str, exit_code: int) -> dict:
    # Empty log + zero exit = success.
    if exit_code == 0:
        return {
            "verdict": "OK",
            "exit_code": 0,
            "category": "ok",
            "evidence": "",
            "suggested_fix": "",
            "files_to_edit": [],
            "retry_command": "",
        }

    # Host-level fatal first.
    for p in FATAL_PATTERNS:
        m = p.search(log)
        if m:
            return {
                "verdict": "FATAL",
                "exit_code": exit_code,
                "category": "host.fatal",
                "evidence": m.group(0)[:240],
                "suggested_fix": "Surface to user — host/docker daemon state is wrong.",
                "files_to_edit": [],
                "retry_command": "",
            }

    # Code-level rules.
    for rule in RULES:
        for p in rule.patterns:
            m = p.search(log)
            if m:
                return {
                    "verdict": "FIXABLE",
                    "exit_code": exit_code,
                    "category": rule.category,
                    "evidence": m.group(0)[:240],
                    "suggested_fix": rule.suggested_fix,
                    "files_to_edit": rule.files_to_edit,
                    "retry_command": rule.retry_command,
                }

    # Nothing matched. If there's any trace-like content, mark FIXABLE so the
    # agent has a chance — otherwise FATAL so we don't loop blindly.
    has_trace = bool(re.search(r"(Traceback|at .+:\d+:\d+|Error: )", log))
    return {
        "verdict": "FIXABLE" if has_trace else "FATAL",
        "exit_code": exit_code,
        "category": "unknown",
        "evidence": _last_nonblank_line(log)[:240],
        "suggested_fix": "Re-read the full log; this failure didn't match any known signature.",
        "files_to_edit": [],
        "retry_command": "",
    }


def _last_nonblank_line(s: str) -> str:
    for line in reversed(s.splitlines()):
        if line.strip():
            return line
    return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exit-code", type=int, required=True)
    ap.add_argument("--log", default="-", help="Path to log file, or '-' for stdin")
    args = ap.parse_args()

    if args.log == "-":
        log = sys.stdin.read()
    else:
        if not os.path.exists(args.log):
            print(json.dumps({"verdict": "FATAL", "exit_code": args.exit_code,
                              "category": "host.fatal", "evidence": f"log file not found: {args.log}",
                              "suggested_fix": "", "files_to_edit": [], "retry_command": ""}))
            return 0
        with open(args.log, "r", errors="replace") as f:
            log = f.read()

    print(json.dumps(classify(log, args.exit_code), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
