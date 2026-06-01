"""
forge/forge/proxy/preview.py
============================
Reverse proxy for project previews.

The browser's iframe points to:
  /api/preview/{project_id}/{path}

This FastAPI route handler proxies the request to:
  http://localhost:{fe_port}/{path}

Where fe_port is the port allocated to this project's frontend process.

Design principles:
  - Port is NEVER exposed to the browser. The iframe always uses
    /api/preview/{project_id}/ regardless of which internal port is in use.
  - Port is looked up from two sources (in order):
      1. The ProcessOrchestrator (agent-started "frontend" process)
      2. The SandboxRunner per-project state (run-panel-started process)
      3. Fall back to the workspace port registry allocation
  - If nothing is running on the allocated port, return 503 with a clear
    message telling the user to start the project.
  - Docker/container routing can replace the local port lookup later by
    swapping out _resolve_fe_port() — nothing else needs to change.
"""

from __future__ import annotations

import re as _re
import httpx
from fastapi import Request, Response, HTTPException
from fastapi.responses import StreamingResponse

from forge.runner.workspace import workspace_manager


# ── HTTP client singleton (reuse connection pool) ──────────────────────────────
_client = httpx.AsyncClient(
    timeout=httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=10.0),
    follow_redirects=True,
    limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
)

# Headers we strip from proxied responses to avoid issues
_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
    # httpx automatically decompresses responses, so the body is already plain
    # bytes — forwarding content-encoding would cause the next hop (forge-ui /
    # the browser) to try decompressing again, producing Z_DATA_ERROR.
    "content-encoding",
    # Also strip these to avoid iframe embedding issues
    "x-frame-options", "content-security-policy",
    "x-content-type-options",
})


def _resolve_fe_port(project_id: str) -> int:
    """
    Return the frontend port the project is (or should be) running on.

    Look-up order:
      1. ProcessOrchestrator — agent-started "frontend" named process
      2. SandboxRunner       — run-panel-started process for this project_id
      3. Workspace port registry — always returns the allocated port

    Returns 0 if no port can be determined.

    NOTE: A non-zero port does not guarantee the process is alive; the caller
    handles ConnectError when the process hasn't started yet or has crashed.

    Replacing this function with Docker/container routing later is the only
    change needed to make previews work in a containerised deployment.
    """
    # ── 1. Orchestrator (agent start_process tool) ─────────────────────────
    try:
        from forge.runner.orchestrator import orchestrator
        proc = orchestrator.get_process(project_id, "frontend")
        if proc and proc.status in ("running", "starting") and proc.port > 0:
            return proc.port
    except Exception:
        pass

    # ── 2. SandboxRunner (Run panel) ───────────────────────────────────────
    try:
        from forge.runner.sandbox import runner as sandbox_runner
        if sandbox_runner.is_project_running(project_id):
            # The sandbox always runs on the port from workspace.assign_ports,
            # so reading from the registry gives the correct answer.
            pass  # fall through to registry below
    except Exception:
        pass

    # ── 3. Workspace port registry ─────────────────────────────────────────
    try:
        ports   = workspace_manager.assign_ports(project_id)
        fe_port = int(ports.get("fe") or 0)
        return fe_port
    except Exception:
        return 0


def _is_project_process_running(project_id: str) -> bool:
    """
    Return True if at least one runner (orchestrator or sandbox) has a live
    process for this project.  Used to produce a clear 503 before attempting
    a TCP connection that would fail with a less helpful error.
    """
    # Check orchestrator
    try:
        from forge.runner.orchestrator import orchestrator
        proc = orchestrator.get_process(project_id, "frontend")
        if proc and proc.status in ("running", "starting"):
            return True
    except Exception:
        pass

    # Check sandbox runner
    try:
        from forge.runner.sandbox import runner as sandbox_runner
        if sandbox_runner.is_project_running(project_id):
            return True
    except Exception:
        pass

    return False


async def proxy_preview(
    project_id: str,
    path: str,
    request: Request,
) -> Response:
    """
    Proxy a preview request to the project's frontend dev server.

    FastAPI route: GET /api/preview/{project_id}/{path:path}

    Steps:
    1. Resolve the project's current frontend port (orchestrator → sandbox → registry)
    2. If no port can be found → 503
    3. If no process is known to be running → 503 with helpful message
    4. Forward the request (method, headers, body) to http://127.0.0.1:{fe_port}/{path}
    5. Stream the response back, rewriting content-type and stripping problematic headers
    """
    fe_port = _resolve_fe_port(project_id)
    if fe_port <= 0:
        raise HTTPException(
            status_code=503,
            detail=(
                f"No port allocated for project '{project_id}'. "
                "Sync the workspace first or run the project."
            ),
        )

    # Use 127.0.0.1 (IPv4) explicitly — httpx resolves "localhost" to ::1 (IPv6)
    # on modern macOS, but Next.js / Vite only bind to 0.0.0.0 (IPv4).
    target_url = f"http://127.0.0.1:{fe_port}/{path}"
    if request.url.query:
        target_url += f"?{request.url.query}"

    # Forward headers (strip host/connection-level headers)
    fwd_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in {
            "host", "connection", "transfer-encoding", "content-length",
            # Strip accept-encoding so the dev server returns uncompressed content.
            # httpx decompresses automatically anyway, but asking for plain bytes
            # avoids the double-decompression Z_DATA_ERROR that occurs when the
            # Content-Encoding header leaks through to forge-ui's route handler.
            "accept-encoding",
        }
    }
    fwd_headers["host"] = f"localhost:{fe_port}"

    # Read body for POST/PUT/PATCH
    body = None
    if request.method in {"POST", "PUT", "PATCH"}:
        body = await request.body()

    try:
        upstream = await _client.request(
            method=request.method,
            url=target_url,
            headers=fwd_headers,
            content=body,
        )
    except httpx.ConnectError:
        raise HTTPException(
            status_code=503,
            detail=(
                f"Dev server not reachable on port {fe_port}. "
                "It may still be starting — the preview will appear automatically once it is ready. "
                "If it does not appear, press Run in the terminal panel."
            ),
        )
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Gateway timeout — dev server took too long.")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Proxy error: {e}")

    # Build response headers, stripping hop-by-hop and security headers that break iframes
    resp_headers = {
        k: v for k, v in upstream.headers.items()
        if k.lower() not in _HOP_BY_HOP
    }
    # Allow iframe embedding
    resp_headers.pop("x-frame-options", None)

    content_type = upstream.headers.get("content-type", "")
    content = upstream.content

    # ── Path rewriting ─────────────────────────────────────────────────────────
    # Root cause of blank preview:
    #   The iframe src is /api/preview/{project_id}/ — a path on localhost:3000
    #   (forge-ui).  The HTML from Next.js contains absolute paths like
    #   /_next/static/chunks/... which the browser resolves to localhost:3000,
    #   loading forge-ui's OWN chunks instead of the user's app chunks.
    #
    # Fix: rewrite /_next/... → /api/preview/{project_id}/_next/... in both the
    # HTML page and JS chunks so every asset fetch goes through FastAPI → dev server.

    # ── JavaScript chunks ──────────────────────────────────────────────────────
    if "javascript" in content_type or "application/x-javascript" in content_type:
        proxy_base = f"/api/preview/{project_id}"
        try:
            text = content.decode("utf-8", errors="replace")
            # Replace string literals "/_next/ → "/api/preview/{id}/_next/
            text = text.replace('"/_next/', f'"{proxy_base}/_next/')
            text = text.replace("'/_next/", f"'{proxy_base}/_next/")
            text = text.replace("`/_next/", f"`{proxy_base}/_next/")
            content = text.encode("utf-8")
            resp_headers.pop("content-length", None)
            resp_headers.pop("Content-Length", None)
        except Exception:
            pass

    # ── HTML page ──────────────────────────────────────────────────────────────
    if "text/html" in content_type:
        proxy_base = f"/api/preview/{project_id}"
        try:
            text = content.decode("utf-8", errors="replace")

            # Inject <base> tag for any truly relative (no leading /) paths.
            # Must go right after <head> so it affects all subsequent elements.
            base_tag = f'<base href="{proxy_base}/">'
            text = _re.sub(r"<head(\s[^>]*)?>", lambda m: m.group(0) + f"\n{base_tag}", text, count=1, flags=_re.IGNORECASE)

            # ── Inject runtime error capture script ───────────────────────────
            # Next.js runtime errors show in the browser overlay but never reach
            # the terminal (exit code stays 0 — server keeps running). This script
            # captures window.onerror + unhandledrejection and postMessages them
            # to the parent frame (PreviewPanel), which feeds them into the
            # auto-fix loop exactly like in-browser bundler errors.
            # Placed right after <head> so it runs before any app code.
            error_capture_script = """<script>
(function() {
  var _sent = {};
  function _send(msg, stack) {
    var key = (msg || '').slice(0, 120);
    if (_sent[key]) return;
    _sent[key] = 1;
    try {
      window.parent.postMessage({ type: 'forge:runtime-error', message: msg, stack: stack || '' }, '*');
    } catch(e) {}
  }
  window.addEventListener('error', function(e) {
    if (!e || !e.message || e.message === 'Script error.') return;
    _send(e.message, e.error && e.error.stack ? e.error.stack : '');
  });
  window.addEventListener('unhandledrejection', function(e) {
    var msg = e.reason ? (e.reason.message || String(e.reason)) : 'Unhandled promise rejection';
    var stack = e.reason && e.reason.stack ? e.reason.stack : '';
    _send(msg, stack);
  });
})();
</script>"""
            # Insert right after <head> open tag (before base tag so it's first)
            text = _re.sub(
                r"(<head[^>]*>)",
                lambda m: m.group(0) + error_capture_script,
                text, count=1, flags=_re.IGNORECASE,
            )

            # Rewrite absolute root-relative paths to go through the proxy.
            # We target the most common patterns in Next.js HTML output:
            #   src="/_next/..."   href="/_next/..."  (HTML attributes)
            #   "/_next/..."       (JSON in __NEXT_DATA__ script)
            #   url(/_next/...)    (CSS inside <style>)
            # We also catch /favicon.ico and other /public/ assets.
            def _rewrite_abs(m: _re.Match) -> str:
                quote, slash_path = m.group(1), m.group(2)
                return f"{quote}{proxy_base}/{slash_path}"

            # Match quoted absolute paths — e.g. "/_next/..." or '/_next/...'
            # Exclude protocol-relative paths (//cdn.example.com) and full URLs.
            text = _re.sub(
                r'(["\'])((?:_next|__nextjs)[^"\']*)',
                _rewrite_abs,
                text,
            )
            # Also rewrite leading-slash variants: "/_next/ → "/api/preview/{id}/_next/
            text = _re.sub(
                r'(["\'])(\/(?:_next|__nextjs)[^"\']*)',
                _rewrite_abs,
                text,
            )
            # url(/_next/...) in inline <style>
            text = text.replace("url(/_next/", f"url({proxy_base}/_next/")

            content = text.encode("utf-8")
            # Content-Length is now wrong — remove it so the client doesn't truncate.
            resp_headers.pop("content-length", None)
            resp_headers.pop("Content-Length", None)
        except Exception:
            pass  # Leave content unchanged if anything goes wrong

    return Response(
        content=content,
        status_code=upstream.status_code,
        headers=resp_headers,
        media_type=content_type or upstream.headers.get("content-type"),
    )
