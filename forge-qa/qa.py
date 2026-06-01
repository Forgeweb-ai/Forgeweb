#!/usr/bin/env python3
"""
forge-qa/qa.py
==============
ONE command runs the same prompt across N model strategies and prints a
per-slot cost / token comparison.

No UI. No paste. No ENTER. The script drives opencode's HTTP API directly:
  POST /session                            create a session
  POST /session/{id}/message               send the prompt with model override
  (streamed response — consumed; when stream closes, that strategy is done)

You verify UI quality with your eyes (open each session's preview). The
numbers come from forge-llm-proxy logs, the same single source of truth
the BYOK dashboard and Platform-managed margin tracker read from.

Usage
-----
    # ./dev.sh must be running (opencode on :7777, proxy on :7799).

    python3 forge-qa/qa.py "Build me a login page, minimal and clean" \\
        --strategy sonnet:anthropic/claude-sonnet-4-6 \\
        --strategy flash35:google/gemini-3.5-flash \\
        --strategy kimi:kimi/kimi-k2.6

Strategy spec
-------------
    label:providerID/modelID

`providerID` must match the keys in forge-opencode-config/opencode.json:
  anthropic, google, kimi (or moonshot), openai

`modelID` must match a model defined under that provider in opencode.json.

Subagents (design-analyst, design-critic, error-fixer)
------------------------------------------------------
The MAIN coder loop uses whatever you pass per-strategy. Subagents use the
model from opencode.json at config-read time — they're NOT overridable per
prompt by design. So across strategies, design-analyst behavior is identical,
which is actually what you want for a fair comparison: same design spec
goes in, different builders try to render it. The cost difference you'll
see lives in the `build` slot row of the comparison table.

If you want to vary the design model too, set DESIGN_ANALYST_MODEL in your
env or user_settings (read by dev.sh) and restart between full runs.

Images / mockups
----------------
    --image path/to/mockup.png

If passed, the file is read once and attached to every strategy's prompt as
a FilePartInput. Image tokens flow through providers' `input_tokens` field
and into the cost calc with no special handling.
"""
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import urllib.error
import urllib.parse
import urllib.request

# meter lives in src/ — sibling import.
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
import meter  # type: ignore[import-not-found]

OPENCODE_DEFAULT = os.environ.get("OPENCODE_URL", "http://127.0.0.1:7777")


# ── Strategy ─────────────────────────────────────────────────────────────────

@dataclass
class Strategy:
    label:       str
    provider_id: str
    model_id:    str
    session_id:  str | None = None
    elapsed_s:   float       = 0.0
    error:       str | None = None


def parse_strategy(spec: str) -> Strategy:
    """
    Accept either `label:provider/model` (simple form) or
    `label:main=provider/model` (longer form — `main=` is optional).
    """
    if ":" not in spec:
        raise ValueError(f"strategy '{spec}' must start with 'label:' — see --help")
    label, rest = spec.split(":", 1)
    if "=" in rest:
        # longer form — keep only main=
        for part in rest.split(","):
            k, _, v = part.partition("=")
            if k.strip() == "main":
                rest = v.strip()
                break
        else:
            raise ValueError(f"strategy '{spec}': longer form must include main=provider/model")
    if "/" not in rest:
        raise ValueError(f"strategy '{spec}': expected provider/model, got '{rest}'")
    provider_id, model_id = rest.split("/", 1)
    return Strategy(
        label       = label.strip(),
        provider_id = provider_id.strip(),
        model_id    = model_id.strip(),
    )


# ── Minimal opencode HTTP client ─────────────────────────────────────────────

class OpencodeClient:
    """Stdlib-only HTTP client. opencode lives on localhost — no need for httpx.

    Every request carries `?directory=<workspace>` because opencode's routing
    middleware uses it to pick the workspace target (without it: HTTP 400).
    """

    def __init__(self, base_url: str, workspace_dir: str, timeout: float = 900.0) -> None:
        self.base_url      = base_url.rstrip("/")
        self.workspace_dir = workspace_dir
        self.timeout       = timeout

    def _request(self, method: str, path: str, body: Any = None, *, with_directory: bool = True) -> Any:
        url = f"{self.base_url}{path}"
        if with_directory:
            sep = "&" if "?" in path else "?"
            url += f"{sep}directory={urllib.parse.quote(self.workspace_dir, safe='/')}"
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                ct  = resp.headers.get("content-type", "")
                raw = resp.read()
                if "json" in ct:
                    return json.loads(raw) if raw else None
                return raw.decode("utf-8", errors="replace") if raw else ""
        except urllib.error.HTTPError as e:
            body_err = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"opencode {method} {path} → HTTP {e.code}: {body_err}") from None
        except urllib.error.URLError as e:
            raise RuntimeError(
                f"opencode at {self.base_url} not reachable ({e.reason}). "
                f"Is ./dev.sh running?"
            ) from None

    def healthcheck(self) -> bool:
        try:
            self._request("GET", "/session?limit=1")
            return True
        except Exception:
            return False

    def create_session(self, *, title: str, provider_id: str, model_id: str) -> str:
        # session create's `Model` schema uses {id, providerID, variant} —
        # note `id`, NOT `modelID`. The /message endpoint takes the OTHER
        # shape ({providerID, modelID}). Different schemas in the same API
        # for the same concept — bake the difference in here.
        body = {
            "title": title,
            "model": {"id": model_id, "providerID": provider_id},
        }
        info = self._request("POST", "/session", body=body) or {}
        sid = info.get("id")
        if not isinstance(sid, str):
            raise RuntimeError(f"opencode create_session returned no id: {info!r}")
        return sid

    def send_prompt(
        self,
        session_id:  str,
        text:        str,
        *,
        provider_id: str,
        model_id:    str,
        files:       list[tuple[str, bytes, str]] | None = None,
    ) -> None:
        """
        Synchronous: POST /session/{id}/message blocks while opencode runs the
        full agent loop (incl. subagents + tools). When it returns, the strategy
        is done.

        `files` is a list of (mime, bytes, filename) — attached as FilePartInput
        with a data: URL so we don't need a separate upload step.
        """
        parts: list[dict] = [{"type": "text", "text": text}]
        if files:
            for mime, raw, name in files:
                b64 = base64.b64encode(raw).decode("ascii")
                parts.append({
                    "type":     "file",
                    "mime":     mime,
                    "filename": name,
                    "url":      f"data:{mime};base64,{b64}",
                })
        # /message uses `ModelRef` ({providerID, modelID}). Yes, different
        # from create_session's `Model` ({id, providerID}). See note above.
        body = {
            "parts": parts,
            "model": {"providerID": provider_id, "modelID": model_id},
        }
        self._request("POST", f"/session/{session_id}/message", body=body)

    def latest_session_directory(self) -> str | None:
        """Return the directory of the most recent session, if any.
        Useful as a default --workspace when the user doesn't pass one."""
        try:
            sessions = self._request("GET", "/session?limit=1", with_directory=False) or []
        except Exception:
            return None
        if isinstance(sessions, list) and sessions:
            d = sessions[0].get("directory")
            if isinstance(d, str):
                return d
        return None


# ── Run loop ─────────────────────────────────────────────────────────────────

def _load_image(path: str | None) -> list[tuple[str, bytes, str]]:
    if not path:
        return []
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        raise FileNotFoundError(f"--image: no such file: {p}")
    mime = mimetypes.guess_type(p.name)[0] or "image/png"
    return [(mime, p.read_bytes(), p.name)]


def _run_one(client: OpencodeClient, strategy: Strategy, prompt: str,
             files: list[tuple[str, bytes, str]]) -> None:
    t0 = time.monotonic()
    title = f"QA: {strategy.label} ({strategy.provider_id}/{strategy.model_id})"
    try:
        sid = client.create_session(
            title=title, provider_id=strategy.provider_id, model_id=strategy.model_id,
        )
        strategy.session_id = sid
        print(f"   session created  →  {sid}")
        print(f"   running …")
        client.send_prompt(
            sid, prompt,
            provider_id=strategy.provider_id, model_id=strategy.model_id,
            files=files,
        )
    except Exception as e:
        strategy.error = str(e)
        print(f"   ! error: {e}")
    finally:
        strategy.elapsed_s = time.monotonic() - t0
        print(f"   done in {strategy.elapsed_s:.1f}s")


def _run_all(client: OpencodeClient, strategies: list[Strategy], prompt: str,
             files: list[tuple[str, bytes, str]]) -> list[Strategy]:
    print("\n" + "=" * 78)
    print(f"PROMPT  :  {prompt}")
    if files:
        print(f"FILES   :  {', '.join(f[2] for f in files)}")
    print("=" * 78)
    for i, s in enumerate(strategies, 1):
        print(f"\n── Strategy {i}/{len(strategies)} :  {s.label}  "
              f"({s.provider_id}/{s.model_id})")
        _run_one(client, s, prompt, files)
    return [s for s in strategies if s.session_id and not s.error]


# ── Comparison ───────────────────────────────────────────────────────────────

def _print_summary(strategies: list[Strategy], log_root: Path) -> int:
    if not strategies:
        print("\nno sessions completed — nothing to compare")
        return 1

    rows = [r for r in (meter.parse_call_file(p) for p in meter.iter_call_files(log_root)) if r]
    rollups = meter.roll_up(rows)

    print(f"\n{'=' * 78}")
    print(f"COMPARISON  ({len(strategies)} strategies)")
    print(f"{'=' * 78}")
    print("Label map:")
    for s in strategies:
        print(f"  {s.label:<12}  session={s.session_id}  "
              f"model={s.provider_id}/{s.model_id}  wall={s.elapsed_s:.1f}s")
    print()
    meter._print_compare(rollups, [s.session_id for s in strategies if s.session_id])
    print()
    print("Open each session in the Forge UI to verify UI quality with your eyes.")
    return 0


# ── CLI ──────────────────────────────────────────────────────────────────────

def _cli() -> int:
    p = argparse.ArgumentParser(
        description="Run one prompt across N model strategies, print per-slot cost comparison.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("prompt", help="The prompt to test across all strategies.")
    p.add_argument("--strategy", action="append", required=True, metavar="SPEC",
                   help="label:providerID/modelID  (pass multiple times)")
    p.add_argument("--image", default=None,
                   help="Path to an image attached to every prompt.")
    p.add_argument("--opencode-url", default=OPENCODE_DEFAULT,
                   help=f"opencode HTTP API base URL (default: {OPENCODE_DEFAULT})")
    p.add_argument("--workspace", default=None,
                   help="Absolute path to an empty workspace to run all strategies in. "
                        "Defaults to the directory of your most recent opencode session. "
                        "For clean comparison, point at a fresh empty dir.")
    p.add_argument("--log-root", default="forge-llm-proxy-logs",
                   help="Path to forge-llm-proxy-logs/ (default: ./forge-llm-proxy-logs)")
    args = p.parse_args()

    try:
        strategies = [parse_strategy(s) for s in args.strategy]
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    try:
        files = _load_image(args.image)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    log_root = Path(args.log_root).expanduser().resolve()
    if not (log_root / "calls").is_dir():
        print(f"error: no calls/ directory under {log_root} — is the proxy running?",
              file=sys.stderr)
        return 2

    # Resolve workspace: explicit flag wins, else auto-detect from latest session.
    workspace = args.workspace
    if not workspace:
        probe = OpencodeClient(args.opencode_url, workspace_dir="/")
        if not probe.healthcheck():
            print(f"error: opencode at {args.opencode_url} not reachable. "
                  f"Run ./dev.sh first.", file=sys.stderr)
            return 2
        workspace = probe.latest_session_directory()
        if not workspace:
            print("error: no --workspace passed and no existing sessions to infer one from. "
                  "Pass --workspace /absolute/path/to/empty/dir", file=sys.stderr)
            return 2
        print(f"(workspace auto-detected from last session: {workspace})")

    if not Path(workspace).is_absolute():
        print(f"error: --workspace must be an absolute path, got: {workspace}",
              file=sys.stderr)
        return 2

    client = OpencodeClient(args.opencode_url, workspace_dir=workspace)
    if not client.healthcheck():
        print(f"error: opencode at {args.opencode_url} not reachable. "
              f"Run ./dev.sh first.", file=sys.stderr)
        return 2

    print(f"opencode  : {args.opencode_url}")
    print(f"workspace : {workspace}")
    print(f"log root  : {log_root}")

    done = _run_all(client, strategies, args.prompt, files)
    return _print_summary(done, log_root)


if __name__ == "__main__":
    raise SystemExit(_cli())
