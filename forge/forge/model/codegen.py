"""
forge/model/codegen.py
=======================
Together AI backend using the official Together Python SDK.
Streams Llama / Phi / any Together-hosted model.

The SDK uses sync streaming, so we run it in a thread executor
to keep FastAPI's async event loop unblocked.
"""

import asyncio
import sys
import traceback
from typing import AsyncIterator
from together import Together
from forge.model.base import (
    ModelBackend,
    PLAN_SYSTEM_PROMPT,
    FILE_GEN_SYSTEM_PROMPT,
    CODEBASE_SYSTEM_PROMPT,
    CODEBASE_UPDATE_SYSTEM_PROMPT,
    FILE_UPDATE_SYSTEM_PROMPT,
    FILE_UPDATE_STREAMING_PROMPT,
    FILE_SURGICAL_EDIT_PROMPT,
    UPDATE_PLAN_SYSTEM_PROMPT,
    CHAT_SYSTEM_PROMPT,
    INTENT_SYSTEM_PROMPT,
    CONTEXT_SUMMARY_PROMPT,
)
from forge.config import config


class CodegenBackend(ModelBackend):

    def __init__(self):
        import os
        import httpx
        base_url = os.getenv("TOGETHER_BASE_URL", "https://api.together.xyz/v1")
        # Set explicit timeouts so hung connections don't wait forever.
        # connect=20s  — time to establish TCP+TLS
        # read=90s     — max wait between any two chunks (not total response time)
        # write=20s    — time to send the request body
        self.client = Together(
            api_key     = config.model.together_api_key or None,
            base_url    = base_url,
            timeout     = httpx.Timeout(connect=20.0, read=90.0, write=20.0, pool=20.0),
        )
        self.model = config.model.together_model
        print(f"[codegen] client init  base_url={base_url}  model={self.model}", flush=True)

    async def health(self) -> dict:
        return {
            "backend": "together",
            "model"  : self.model,
            "status" : "ok" if config.model.together_api_key else "missing_api_key",
        }

    @staticmethod
    def _humanize_error(e: Exception, model: str) -> str:
        """
        Turn the raw together-sdk exception into something the UI can show.
        The most common cause of a 500 is an invalid model slug — the API
        returns `{"error":{"message":"Internal server error",...}}` with no
        hint that the model doesn't exist. We surface that ourselves.
        """
        msg = str(e)
        klass = type(e).__name__
        looks_like_unknown_model = (
            "500" in msg
            and "Internal server error" in msg
        )
        hint = ""
        if looks_like_unknown_model:
            hint = (
                f"\nHint: Together returned 500 on every retry. The model "
                f"slug `{model}` may not exist. Open https://api.together.xyz/models "
                f"to see valid slugs (e.g. moonshotai/Kimi-K2-Instruct, "
                f"Qwen/Qwen2.5-Coder-32B-Instruct, deepseek-ai/DeepSeek-V3). "
                f"Set TOGETHER_MODEL in forge/.env then restart the server."
            )
        return f"{klass}: {msg}{hint}"

    # ── Internal streaming helper ─────────────────────────────────────────────

    async def _stream(
        self,
        system: str,
        messages: list[dict],
    ) -> AsyncIterator[str]:
        """Stream tokens via Together SDK, bridged into async with a queue."""

        # Filter out empty-content messages — API returns 400 on them.
        # Content can be a string (text) or a list (vision multipart) — handle both.
        def _has_content(m: dict) -> bool:
            c = m.get("content", "")
            if isinstance(c, list):
                return len(c) > 0
            return bool(str(c).strip())
        clean = [m for m in messages if _has_content(m)]
        all_messages = [{"role": "system", "content": system}] + clean

        # Sentinel objects so we can distinguish "done" from "error" in the queue
        DONE  = object()
        queue: asyncio.Queue = asyncio.Queue()
        loop  = asyncio.get_running_loop()

        def _run_sync():
            import time as _time

            # Network errors that are safe to retry when no tokens were sent yet.
            # Together AI's streaming endpoint occasionally drops the TCP connection
            # on large requests before sending a single byte of response body.
            RETRY_SIGNALS = (
                "incomplete chunked read",
                "peer closed connection",
                "connection reset",
                "remoteprocotolerror",
                "timed out",
                "read timeout",
                "timeout",
            )
            MAX_RETRIES = 3

            try:
                for attempt in range(1, MAX_RETRIES + 1):
                    token_count = 0
                    attempt_tag = f"  [attempt {attempt}/{MAX_RETRIES}]" if attempt > 1 else ""
                    print(
                        f"[codegen] → {self.model}  "
                        f"(msgs={len(all_messages)}, max_tokens={config.model.max_tokens})"
                        f"{attempt_tag}",
                        flush=True,
                    )
                    try:
                        response = self.client.chat.completions.create(
                            model       = self.model,
                            messages    = all_messages,
                            max_tokens  = config.model.max_tokens,
                            temperature = config.model.temperature,
                            stream      = True,
                        )
                        chunk_count = 0
                        for chunk in response:
                            chunk_count += 1
                            # Kimi-K2 (and some other models) emit keep-alive chunks
                            # with an empty choices list — log them so we can see
                            # the connection is alive even before content arrives.
                            if not chunk.choices:
                                if chunk_count == 1:
                                    print("[codegen] ◌ first keep-alive received (model is loading…)", flush=True)
                                continue
                            delta = chunk.choices[0].delta

                            # Reasoning tokens (thinking models) come on a separate
                            # channel. We tag them so the route can route them to a
                            # different SSE event and keep the JSON buffer clean.
                            reasoning = (
                                getattr(delta, "reasoning", None)
                                or getattr(delta, "reasoning_content", None)
                            )
                            if reasoning:
                                if token_count == 0:
                                    print("[codegen] 💭 model is thinking (reasoning tokens arriving)…", flush=True)
                                token_count += 1
                                loop.call_soon_threadsafe(
                                    queue.put_nowait,
                                    f"__FORGE_REASONING__:{reasoning}",
                                )

                            content = getattr(delta, "content", None)
                            if content:
                                if token_count == 0:
                                    print("[codegen] ✍ first content token received", flush=True)
                                token_count += 1
                                loop.call_soon_threadsafe(queue.put_nowait, content)

                        print(f"[codegen] ← stream complete ({token_count} tokens)", flush=True)
                        break  # success — exit retry loop

                    except Exception as e:
                        err_lower  = str(e).lower()
                        # Only retry if the connection died before we emitted anything —
                        # mid-stream retries would send duplicate tokens to the client.
                        is_network = any(sig in err_lower for sig in RETRY_SIGNALS)
                        can_retry  = is_network and token_count == 0 and attempt < MAX_RETRIES

                        if can_retry:
                            wait = attempt  # 1 s, then 2 s
                            print(
                                f"[codegen] ⚠ connection dropped before first token "
                                f"(attempt {attempt}/{MAX_RETRIES}), retrying in {wait}s… "
                                f"({type(e).__name__})",
                                file=sys.stderr, flush=True,
                            )
                            _time.sleep(wait)
                            continue

                        # Non-retryable or all retries exhausted — surface the error.
                        tb = traceback.format_exc()
                        print(
                            f"[codegen] ✗ ERROR: {type(e).__name__}: {e}\n{tb}",
                            file=sys.stderr, flush=True,
                        )
                        loop.call_soon_threadsafe(
                            queue.put_nowait,
                            f"__FORGE_ERROR__:{type(e).__name__}: {e}",
                        )
                        break  # don't loop again after reporting the error

            finally:
                loop.call_soon_threadsafe(queue.put_nowait, DONE)

        loop.run_in_executor(None, _run_sync)

        while True:
            token = await queue.get()
            if token is DONE:
                break
            yield token

    # ── Code-fence stripper ───────────────────────────────────────────────────

    @staticmethod
    def _strip_fences(text: str) -> str:
        """
        Remove leading/trailing markdown code fences that the model sometimes
        wraps file content with (e.g. ```tsx … ``` or ```python … ```).
        Works even when the fence has a language tag or trailing whitespace.
        """
        import re as _re
        t = text.strip()
        # Strip a leading fence: optional language tag after the backticks
        t = _re.sub(r'^```[a-zA-Z0-9+\-_.]*\s*\n?', '', t)
        # Strip a trailing fence
        t = _re.sub(r'\n?```\s*$', '', t)
        return t.strip()

    # ── Image design analysis pre-pass ───────────────────────────────────────

    async def _analyze_image_design(
        self,
        image_base64: str,
        image_type: str | None = None,
    ) -> str:
        """
        Vision call: extract CSS-ready, developer-actionable design specs from the
        reference image.  The result is injected into both the plan prompt and each
        file-gen prompt so the model never has to re-infer style from raw image bytes
        — it always has exact Tailwind classes, hex codes, gradient approximations, etc.

        Returns "" on any failure so that generation continues without the spec.
        """
        mime = image_type or "image/png"
        analysis_prompt = (
            "You are a senior UI engineer analyzing a screenshot to produce a COMPLETE, pixel-faithful implementation spec.\n"
            "A developer will use this spec ONLY — they cannot see the original image.\n"
            "Be exhaustive. Miss nothing. Output ONLY the structured spec below — no preamble.\n\n"

            "## COLORS (every single color visible — exact hex)\n"
            "- Page background: #HEX\n"
            "- Nav background: #HEX (transparent / solid / blur?)\n"
            "- Primary text: #HEX\n"
            "- Secondary / muted text: #HEX\n"
            "- Accent / brand: #HEX\n"
            "- CTA button bg: #HEX\n"
            "- CTA button text: #HEX\n"
            "- Secondary button bg: #HEX  border: #HEX\n"
            "- Decorative element colors (gradients, blobs, orbs): describe each with hex stops\n"
            "- Border / divider color: #HEX\n"
            "- Any other distinct color: #HEX\n\n"

            "## TYPOGRAPHY\n"
            "Identify the font family visually (serif / sans-serif / monospace / slab) and suggest the closest Google Font.\n"
            "- Font family: [visual description + suggested Google Font, e.g. 'monospace slab → Space Grotesk or Instrument Serif']\n"
            "- Hero H1: font-size (est. rem), font-weight, letter-spacing, line-height, color\n"
            "  Tailwind: [e.g. text-7xl font-black tracking-tight leading-none text-gray-900]\n"
            "- Sub-headline / eyebrow: [classes + color]\n"
            "- Body paragraph: [classes + color]\n"
            "- Nav links: [classes + color + any numbering style like '(01)']\n"
            "- CTA button label: [classes]\n"
            "- Secondary link: [classes]\n\n"

            "## LAYOUT & SPACING\n"
            "- Page structure: [describe sections top-to-bottom]\n"
            "- Nav: position (fixed/sticky/static), height, padding, logo left / links center / CTA right?\n"
            "  Tailwind: [exact classes for nav container]\n"
            "- Hero section: height (min-h-screen?), flex direction, alignment, padding\n"
            "  Tailwind: [exact classes]\n"
            "- Content column: max-width, left/right/center aligned, horizontal padding\n"
            "  Tailwind: [e.g. max-w-2xl pl-16 pr-8 pt-32]\n"
            "- Spacing between headline and body: [e.g. mt-6]\n"
            "- Spacing between body and CTAs: [e.g. mt-8]\n"
            "- CTA button gap: [e.g. gap-4]\n\n"

            "## DECORATIVE ELEMENTS (critical — describe every non-text visual)\n"
            "For each blob, orb, gradient wash, background shape, or decorative graphic:\n"
            "- Position: absolute or fixed? top/right/bottom/left values (% or px estimate)\n"
            "- Size: width and height (e.g. w-[600px] h-[500px])\n"
            "- Colors: gradient stops with exact hex (e.g. from #FF6B35 via #FF9500 to #FFD700)\n"
            "- Shape: circle (rounded-full) / blob / rectangle\n"
            "- Blur: filter blur amount (e.g. blur-3xl)\n"
            "- Opacity: (e.g. opacity-40)\n"
            "- Z-index: behind content (z-0) or in front?\n"
            "Example: 'Right-side orb: absolute top-0 right-0 w-[55%] h-screen rounded-l-full bg-gradient-to-bl from-[#FF6B00] via-[#FF9500] to-[#FFE066] opacity-60 blur-none'\n\n"

            "## BACKGROUND TREATMENT\n"
            "Page bg type: solid / gradient / photograph / pattern\n"
            "- If solid: #HEX  Tailwind: bg-[#HEX]\n"
            "- If gradient: exact CSS: background: linear-gradient(...)\n"
            "- If photograph: CSS approximation: background: linear-gradient(...)\n\n"

            "## BUTTONS & INTERACTIVE ELEMENTS\n"
            "Primary CTA:\n"
            "  Tailwind: [e.g. bg-blue-600 text-white font-semibold px-6 py-3 rounded-full hover:bg-blue-700 transition]\n"
            "Secondary CTA / ghost button:\n"
            "  Tailwind: [e.g. text-gray-900 font-medium flex items-center gap-2 hover:gap-3 transition-all]\n"
            "Nav CTA (top right):\n"
            "  Tailwind: [classes]\n\n"

            "## COMPLETE COMPONENT SPECS\n"
            "For EACH visible section write exact implementation classes:\n\n"
            "NAV:\n"
            "  Container: [classes]\n"
            "  Logo: [text style + classes]\n"
            "  Links: [classes, any special formatting like numbered labels]\n"
            "  CTA button: [classes]\n\n"
            "HERO:\n"
            "  Outer wrapper: [classes — must be relative to contain the decorative elements]\n"
            "  Content column: [classes]\n"
            "  Eyebrow/role text: [classes]\n"
            "  H1 headline: [classes + exact text if visible]\n"
            "  Body paragraph: [classes]\n"
            "  CTA row: [classes]\n"
            "  Decorative element(s): [full implementation as described in DECORATIVE ELEMENTS above]\n\n"

            "## READY-TO-PASTE CODE SNIPPETS\n"
            "Write 3-5 complete JSX snippets for the most distinctive elements.\n"
            "These must be copy-paste ready. Use Tailwind arbitrary values for exact colors.\n"
            "Example:\n"
            "  // Decorative gradient orb\n"
            "  <div className='absolute top-0 right-0 w-[50%] h-screen bg-gradient-to-bl from-[#FF6B00] via-[#FFAA00] to-[#FFE066] opacity-50 pointer-events-none' />\n\n"

            "Be exhaustive. A developer building from this spec alone must produce output indistinguishable from the screenshot."
        )
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{image_base64}"},
                    },
                    {"type": "text", "text": analysis_prompt},
                ],
            }
        ]
        try:
            spec = await self._non_stream_call(
                messages, max_tokens=3000, temperature=0.1, label="image-design-analysis"
            )
            print(f"[codegen] 🎨 design spec extracted ({len(spec)} chars)", flush=True)
            return spec.strip()
        except Exception as e:
            print(f"[codegen] ⚠ image analysis failed ({e}) — continuing without spec", flush=True)
            return ""

    # ── Codebase generation — two-phase ──────────────────────────────────────
    #
    # Phase 1 (PLAN)  : fast non-streaming call → AI announces the file list.
    #                   Client receives "plan" event immediately, file tree
    #                   shows with skeleton entries before any code is written.
    #
    # Phase 2 (FILES) : one streaming call per file → raw content (no JSON
    #                   wrapper) streams directly into Monaco in real time.
    #                   Each call is small → far less likely to drop.
    #                   If a file call does fail, we retry just that file.
    #
    # Updates bypass phase 1 and go straight to the existing single-call path.

    _MAX_CONTEXT_CHARS = 100_000

    async def _non_stream_call(
        self,
        messages: list[dict],
        max_tokens: int = 512,
        temperature: float = 0.1,
        label: str = "",
    ) -> str:
        """Non-streaming API call with heartbeat. Returns the full response text."""
        import asyncio as _asyncio, time as _time

        loop = _asyncio.get_running_loop()
        MAX_RETRY = 3

        for attempt in range(1, MAX_RETRY + 1):
            try:
                print(
                    f"[codegen] → {self.model} non-stream {label}"
                    + (f" [retry {attempt}]" if attempt > 1 else ""),
                    flush=True,
                )
                future = loop.run_in_executor(
                    None,
                    lambda: self.client.chat.completions.create(
                        model       = self.model,
                        messages    = messages,
                        max_tokens  = max_tokens,
                        temperature = temperature,
                        stream      = False,
                        timeout     = 120,
                    ),
                )
                start = _time.time()
                while True:
                    done, _ = await _asyncio.wait({future}, timeout=2.0)
                    if done:
                        break
                    print(f"[codegen] ⏳ {label} thinking… ({int(_time.time()-start)}s)", flush=True)

                response  = await future
                full_text = (response.choices[0].message.content or "").strip()
                print(f"[codegen] ← {label} done ({len(full_text)} chars)", flush=True)
                return full_text

            except Exception as e:
                tb = traceback.format_exc()
                print(f"[codegen] ✗ {label} attempt {attempt}: {e}\n{tb}", file=sys.stderr, flush=True)
                if attempt < MAX_RETRY:
                    import asyncio as _a
                    await _a.sleep(attempt * 2)
                    continue
                raise

    async def generate_codebase(
        self,
        prompt: str,
        language: str = "auto",
        extra_context: str = "",
        stack: dict | None = None,
        image_base64: str | None = None,
        image_type: str | None = None,
    ) -> AsyncIterator[str]:
        import asyncio as _asyncio, json as _json, time as _time

        is_update = extra_context.startswith("UPDATE EXISTING PROJECT:")

        if len(extra_context) > self._MAX_CONTEXT_CHARS:
            extra_context = extra_context[:self._MAX_CONTEXT_CHARS] + "\n\n[...truncated]"

        # ── UPDATE path: two-phase (plan → stream each file) ─────────────────
        if is_update:
            import re as _re

            # ── Parse extra_context to extract {path: content} dict ───────────
            # Format produced by Composer.tsx:
            #   UPDATE EXISTING PROJECT:\nProject: <name>\n\n
            #   Files to update ...\n// FILE: src/App.tsx\n<content>\n\n---\n\n
            #   // FILE: src/main.tsx\n<content>\n\n
            #   All file paths in project (for reference):\n<csv>
            project_name = ""
            m = _re.search(r'^Project:\s*(.+)$', extra_context, _re.MULTILINE)
            if m:
                project_name = m.group(1).strip()

            all_paths_str = ""
            m2 = _re.search(
                r'All file paths in project \(for reference\):\s*(.+)$',
                extra_context, _re.MULTILINE,
            )
            if m2:
                all_paths_str = m2.group(1).strip()
            all_paths = [p.strip() for p in all_paths_str.split(',') if p.strip()] if all_paths_str else []

            # Split on "// FILE: <path>" lines to extract per-file content
            context_files: dict[str, str] = {}
            parts = _re.split(r'^// FILE: (.+)$', extra_context, flags=_re.MULTILINE)
            for i in range(1, len(parts), 2):
                path = parts[i].strip()
                content_raw = parts[i + 1] if i + 1 < len(parts) else ""
                # Remove trailing --- separator (only present between files, not after last)
                content_raw = _re.sub(r'\n\s*---\s*\n.*', '', content_raw, flags=_re.DOTALL)
                # Remove trailing "All file paths" section from the last file's block
                content_raw = _re.sub(r'\n\nAll file paths.*', '', content_raw, flags=_re.DOTALL)
                context_files[path] = content_raw.strip()

            is_static_single_file_project = (
                "index.html" in (all_paths or list(context_files.keys()))
                and not any(p in all_paths for p in ("package.json", "vite.config.ts", "vite.config.js", "src/main.tsx", "src/main.jsx"))
            )

            # Detect TSX/React project conversion request — bypass static constraint entirely
            _CONVERSION_KEYWORDS = [
                "convert to tsx", "convert to react", "convert to vite", "convert to a react",
                "migrate to react", "migrate to tsx", "tsx project", "react project",
                "make it a react", "turn into react", "turn into tsx", "refactor to tsx",
                "convert html to react", "convert to nextjs", "convert to next.js",
            ]
            is_conversion_request = any(kw in prompt.lower() for kw in _CONVERSION_KEYWORDS)
            if is_conversion_request:
                is_static_single_file_project = False
                print(f"[UPDATE] TSX/React conversion detected — disabling static-project constraint", flush=True)

            print(
                f"\n{'─'*60}\n"
                f"[UPDATE] two-phase streaming\n"
                f"  model         : {self.model}\n"
                f"  instruction   : {prompt[:100]!r}\n"
                f"  context_files : {list(context_files.keys())}\n"
                f"  all_paths     : {len(all_paths)} total\n"
                f"{'─'*60}",
                flush=True,
            )

            yield f"__FORGE_STATUS__:Analysing codebase…"

            # Helper for vision messages (UPDATE path also uses same helper defined later
            # but for UPDATE path we define a local alias since _make_image_content is only
            # defined in the NEW BUILD path below).  Mirror it here so both paths work.
            def _img(text: str) -> list | str:
                if not image_base64:
                    return text
                mime = image_type or "image/png"
                return [
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{image_base64}"}},
                    {"type": "text", "text": text},
                ]

            # ── Phase 1: fast non-streaming plan — update/create/delete ops ───
            # Token-budget goal: send ONLY file paths (+ description when available).
            # No file content in the planning call — the AI decides what to edit from
            # names + descriptions alone. File content is sent per-file in Phase 2,
            # only for the files the AI actually plans to touch.
            # Exception: single-file static HTML projects send the file content because
            # the model needs to see element references ("get-started button") to route
            # the change to the right place. For multi-file projects, file names are
            # unambiguous enough ("Navbar.tsx", "Hero.tsx") so content is not needed.
            provided_files_list = "\n".join(f"- {p}" for p in context_files.keys()) or "- none"
            # Build a richer path+description list when descriptions are available
            all_files_list_parts = []
            for p in all_paths:
                cf = context_files.get(p)
                # Extract a rough description from the first JSDoc/comment line if present
                desc = ""
                if cf:
                    for line in cf.splitlines()[:8]:
                        line = line.strip().lstrip("/*# ")
                        if len(line) > 10 and not line.startswith("import") and not line.startswith("export"):
                            desc = line[:80]
                            break
                all_files_list_parts.append(f"- {p}" + (f"  # {desc}" if desc else ""))
            all_files_list = "\n".join(all_files_list_parts) or provided_files_list

            # For single-file static HTML projects only, include the file content so
            # the AI can find element references (e.g. "Get Started button").
            code_context_block = ""
            if is_static_single_file_project and "index.html" in context_files:
                html_content = context_files["index.html"]
                code_context_block = f"// FILE: index.html\n{html_content[:6000]}{'…(truncated)' if len(html_content) > 6000 else ''}"

            plan_prompt = (
                f"Instruction: {prompt}\n\n"
                f"Existing files in project:\n{all_files_list}\n\n"
                + (
                    f"File content (single-file project — find UI element references here):\n"
                    f"{code_context_block}\n\n"
                    if code_context_block else ""
                )
                + (
                    "Architecture constraint: this is a single-file static HTML/CDN project. "
                    "Do NOT create src/*.tsx, App.tsx, package.json, vite.config.ts, tsconfig.json, or any framework build files. "
                    "Auth pages (login, register, signup), dashboards, and other distinct app screens MUST be separate .html files. "
                    "Only keep things in index.html when the user asks for a modal, tab, section, or panel.\n\n"
                    if is_static_single_file_project else ""
                )
                + (
                    "╔══════════════════════════════════════════════════════════════╗\n"
                    "║   REFERENCE DESIGN IMAGE PROVIDED — CLONE ITS VISUAL STYLE  ║\n"
                    "╚══════════════════════════════════════════════════════════════╝\n"
                    "Update the project to match the reference image exactly:\n"
                    "same colors, layout, typography, spacing, and component styles.\n\n"
                    if image_base64 else ""
                )
                +
                "Return a JSON object with TWO keys:\n\n"
                "1. \"reasoning\" — an array of 3-5 short sentences showing your step-by-step thinking:\n"
                "   - What the user is actually asking for\n"
                "   - What you found in the codebase (specific elements, file names, structure)\n"
                "   - Why you chose each file to create/update\n"
                "   - Any architecture decisions (e.g. why login goes in a separate page)\n\n"
                "2. \"ops\" — an array of operation objects:\n"
                '   [{"op":"update|create|delete","path":"file/path","description":"short reason"}]\n\n'
                "Use create for brand-new files. Use delete to remove files. Use update for existing files.\n\n"
                "Example output:\n"
                '{"reasoning":["User wants a login page connected to the Get Started button.","I can see index.html has a Get Started button in the hero section at line 42.","Login is an auth flow — it must be a separate page, not a modal.","I will create login.html and update the button href in index.html."],'
                '"ops":[{"op":"create","path":"login.html","description":"New auth page"},{"op":"update","path":"index.html","description":"Update Get Started button href"}]}'
            )
            plan_messages = [
                {"role": "system", "content": UPDATE_PLAN_SYSTEM_PROMPT},
                {"role": "user",   "content": _img(plan_prompt)},
            ]

            try:
                plan_text = await self._non_stream_call(
                    plan_messages, max_tokens=700, temperature=0.1, label="update-plan"
                )
                # Clean markdown fences
                plan_text = _re.sub(r'^```(?:json)?\s*', '', plan_text, flags=_re.MULTILINE)
                plan_text = _re.sub(r'\s*```\s*$', '',        plan_text, flags=_re.MULTILINE).strip()

                # Try new {reasoning, ops} object format first, then fall back to plain array
                parsed = _json.loads(plan_text)
                plan_reasoning: list[str] = []
                if isinstance(parsed, dict):
                    plan_reasoning = [str(s) for s in parsed.get("reasoning", []) if str(s).strip()]
                    raw_plan = parsed.get("ops", parsed.get("files", []))
                    if not isinstance(raw_plan, list):
                        raise ValueError("ops is not a list")
                elif isinstance(parsed, list):
                    # Old format: plain array — still supported
                    raw_plan = parsed
                else:
                    raise ValueError("unexpected plan response type")

            except Exception as e:
                print(f"[UPDATE] plan parse failed ({e}) — falling back to all context files", flush=True)
                raw_plan = [{"op": "update", "path": p, "description": "Update relevant file"} for p in context_files.keys()]
                plan_reasoning = []

            available_paths = set(all_paths) | set(context_files.keys())
            planned_ops: list[dict] = []
            # Collect AI thoughts to emit as FORGE_THOUGHT events for the FE
            ai_thoughts: list[str] = list(plan_reasoning)  # start with overall reasoning

            for item in raw_plan:
                if isinstance(item, str):
                    path = item.strip()
                    op = "update" if path in available_paths else "create"
                    desc = "Update file" if op == "update" else "Create file"
                    thought = ""
                elif isinstance(item, dict):
                    path = str(item.get("path", "")).strip()
                    op = str(item.get("op", "update")).lower().strip()
                    desc = str(item.get("description", "")).strip()
                    thought = str(item.get("thought", "")).strip()  # legacy per-file thought
                    if op in {"edit", "modify"}:
                        op = "update"
                    if op not in {"update", "create", "delete"}:
                        op = "update" if path in available_paths else "create"
                else:
                    continue

                if not path:
                    continue
                if is_static_single_file_project and path != "index.html":
                    # Only redirect TypeScript/framework-specific files — NOT additional .html pages.
                    # Separate .html pages (login.html, register.html, etc.) are valid in CDN projects.
                    _is_framework_file = (
                        path.startswith("src/")
                        or path in (
                            "package.json", "vite.config.ts", "vite.config.js",
                            "tsconfig.json", "tsconfig.node.json",
                            "postcss.config.js", "tailwind.config.ts", "tailwind.config.js",
                        )
                        or path.endswith(".tsx")
                        or path.endswith(".ts")
                        or path.endswith(".jsx")
                    )
                    if _is_framework_file:
                        print(f"[UPDATE] static CDN project: blocking framework file {path}, redirecting to index.html", flush=True)
                        path = "index.html"
                        op = "update"
                        desc = "Update single-file app"
                    else:
                        print(f"[UPDATE] static CDN project: allowing new file {path} (non-framework)", flush=True)
                if op == "update" and path not in context_files:
                    # We cannot safely patch a file whose content was not sent.
                    # Ask the model to create only if the path is genuinely new.
                    if path in available_paths:
                        print(f"[UPDATE] skipping unavailable update target: {path}", flush=True)
                        continue
                    op = "create"
                if op == "delete" and path not in available_paths:
                    print(f"[UPDATE] skipping delete for missing file: {path}", flush=True)
                    continue

                # Per-file thought (legacy field) — add when present and not redundant
                if thought and thought not in " ".join(ai_thoughts):
                    ai_thoughts.append(f"{path}: {thought}")

                planned_ops.append({
                    "op": op,
                    "path": path,
                    "description": desc or f"{op.capitalize()} {path}",
                    "thought": thought,
                })

            if not planned_ops:
                planned_ops = [
                    {"op": "update", "path": p, "description": "Update relevant file", "thought": ""}
                    for p in context_files.keys()
                ]

            # ── Synthesise fallback thoughts if the AI left them all empty ────────
            # This guarantees the FE always shows SOMETHING in the thought block
            # even if the model skipped the reasoning field.
            if not ai_thoughts:
                # Build minimal narrative from the planned ops
                create_ops = [o for o in planned_ops if o["op"] == "create"]
                update_ops = [o for o in planned_ops if o["op"] == "update"]
                ai_thoughts.append(f"User is asking: {prompt[:120]}")
                if context_files:
                    ai_thoughts.append(
                        f"I scanned the codebase and found: "
                        + ", ".join(list(context_files.keys())[:4])
                    )
                if create_ops:
                    ai_thoughts.append(
                        f"Creating new file{'s' if len(create_ops) > 1 else ''}: "
                        + ", ".join(o['path'] for o in create_ops)
                    )
                if update_ops:
                    ai_thoughts.append(
                        f"Updating existing file{'s' if len(update_ops) > 1 else ''}: "
                        + ", ".join(o['path'] for o in update_ops)
                    )

            deduped_ops: list[dict] = []
            seen_ops: dict[tuple[str, str], dict] = {}
            for op_item in planned_ops:
                key = (op_item["op"], op_item["path"])
                if key in seen_ops:
                    existing = seen_ops[key]
                    if op_item.get("description") and op_item["description"] not in existing["description"]:
                        existing["description"] += f"; {op_item['description']}"
                    continue
                seen_ops[key] = op_item
                deduped_ops.append(op_item)
            planned_ops = deduped_ops

            files_to_write = [op for op in planned_ops if op["op"] in {"update", "create"}]
            deleted_paths = [op["path"] for op in planned_ops if op["op"] == "delete"]

            print(f"[UPDATE] plan → ops: {planned_ops}", flush=True)

            # ── Emit AI thought events BEFORE the plan so FE shows reasoning first ──
            for thought_text in ai_thoughts:
                yield f"__FORGE_THOUGHT__:{thought_text}"

            # Emit plan event (same shape as build plan so the client handles it identically)
            plan_obj = {
                "project_name": project_name,
                "description":  f"Updating: {prompt}",
                "tech_stack":   [],
                "files":        [
                    {
                        "path": op["path"],
                        "description": f"{op['op'].capitalize()}: {op['description']}",
                        "op": op["op"],
                    }
                    for op in planned_ops
                ],
                "setup_commands": [],
                "run_command":  "",
                "is_update":    True,
            }
            yield f"__FORGE_PLAN__:{_json.dumps(plan_obj)}"

            # ── Phase 2: stream each changed file directly ─────────────────────
            assembled_update: list[dict] = []
            other_summary = "\n".join(f"  - {op['op']}: {op['path']}" for op in planned_ops)

            _META_PREFIXES = (
                "__FORGE_REASONING__:",
                "__FORGE_STATUS__:",
                "__FORGE_ERROR__:",
            )

            for path in deleted_paths:
                yield f"__FORGE_STATUS__:Deleting {path}…"
                print(f"[UPDATE] - delete {path}", flush=True)

            for op_spec in files_to_write:
                path = op_spec["path"]
                op = op_spec["op"]
                current_content = context_files.get(path, "")
                action = "Creating" if op == "create" else "Editing"

                yield f"__FORGE_FILE_START__:{_json.dumps({'path': path, 'description': f'{action}…', 'op': op})}"
                yield f"__FORGE_STATUS__:{action} {path}…"

                # ── UPDATE ops → surgical old/new edits (no whole-file rewrite) ──
                # ── CREATE ops → stream the full new file as before ───────────────
                if op == "update" and current_content:
                    # Send the FULL file — truncation was the #1 cause of "old not found"
                    # because the model was shown a truncated file but asked to match
                    # against the full content. Token cost is acceptable up to 40k chars;
                    # beyond that we still truncate but much more generously.
                    _MAX_SURGICAL_CHARS = 40_000
                    content_for_edit = (
                        current_content[:_MAX_SURGICAL_CHARS] + "\n// …(file truncated — only changes to the shown section will be applied)"
                        if len(current_content) > _MAX_SURGICAL_CHARS
                        else current_content
                    )
                    edit_user = (
                        f"Instruction: {prompt}\n\n"
                        f"File: {path}\n\n"
                        f"Current content:\n{content_for_edit}\n\n"
                        + (
                            f"Other planned changes in this request:\n{other_summary}\n\n"
                            if len(planned_ops) > 1 else ""
                        )
                        + "Return ONLY the JSON array of edit operations. No explanation."
                    )
                    edit_messages = [
                        {"role": "system", "content": FILE_SURGICAL_EDIT_PROMPT},
                        {"role": "user",   "content": edit_user},
                    ]

                    file_content = current_content  # start from current, edits will be applied client-side
                    try:
                        raw_edits_text = await self._non_stream_call(
                            edit_messages, max_tokens=2048, temperature=0.1, label=f"surgical-edit:{path}"
                        )
                        # Strip markdown fences
                        raw_edits_text = _re.sub(r'^```(?:json)?\s*', '', raw_edits_text, flags=_re.MULTILINE)
                        raw_edits_text = _re.sub(r'\s*```\s*$', '', raw_edits_text, flags=_re.MULTILINE).strip()
                        arr_s = raw_edits_text.find('[')
                        arr_e = raw_edits_text.rfind(']') + 1
                        if arr_s >= 0 and arr_e > arr_s:
                            raw_edits_text = raw_edits_text[arr_s:arr_e]
                        edit_ops = _json.loads(raw_edits_text)
                        if not isinstance(edit_ops, list):
                            raise ValueError("edit response was not a list")

                        # Apply edits locally to produce the final file content.
                        # Matching strategy (tried in order, stops at first hit):
                        #   1. Exact match (fast path, most common)
                        #   2. Normalised-whitespace match — collapses runs of spaces/tabs
                        #   3. Line-trimmed match — strips leading/trailing space per line
                        #   4. Difflib best-block match — finds the longest run of lines
                        #      from `old` that appears verbatim in `content`, then anchors
                        #      the full replacement around that run. Handles the case where
                        #      the model slightly misquotes surrounding context lines.
                        import re as _re2
                        import difflib as _difflib

                        def _norm_ws(s: str) -> str:
                            return _re2.sub(r'[ \t]+', ' ', s)

                        def _find_and_replace(content: str, old: str, new: str) -> tuple[str, bool]:
                            # 1. Exact
                            if old in content:
                                return content.replace(old, new, 1), True

                            # 2. Normalised whitespace
                            norm_c = _norm_ws(content)
                            norm_o = _norm_ws(old)
                            if norm_o in norm_c:
                                idx = norm_c.find(norm_o)
                                # Walk both strings to map normalised → original span
                                oi = ci = 0
                                start_orig = end_orig = -1
                                while ci < len(content) and oi < len(norm_c):
                                    if oi == idx:           start_orig = ci
                                    if oi == idx + len(norm_o): end_orig = ci; break
                                    if content[ci] in (' ', '\t') and ci + 1 < len(content) and content[ci+1] in (' ', '\t'):
                                        ci += 1
                                    else:
                                        oi += 1; ci += 1
                                if start_orig >= 0 and end_orig > start_orig:
                                    return content[:start_orig] + new + content[end_orig:], True

                            # 3. Line-trimmed
                            def _trim_lines(s: str) -> str:
                                return '\n'.join(l.strip() for l in s.splitlines())
                            trimmed_c = _trim_lines(content)
                            trimmed_o = _trim_lines(old)
                            if trimmed_o in trimmed_c:
                                old_lines     = [l.strip() for l in old.splitlines()]
                                content_lines = content.splitlines(keepends=True)
                                for li in range(len(content_lines)):
                                    if all(
                                        li + i < len(content_lines) and
                                        content_lines[li + i].strip() == old_lines[i]
                                        for i in range(len(old_lines))
                                    ):
                                        end_li = li + len(old_lines)
                                        return (
                                            "".join(content_lines[:li]) + new + "\n" +
                                            "".join(content_lines[end_li:]),
                                            True,
                                        )

                            # 4. Difflib best-block: model may have slightly misquoted context.
                            # Find the longest matching block between old_lines and content_lines,
                            # then use that anchor to locate the full old span in content.
                            old_lines_raw     = old.splitlines()
                            content_lines_raw = content.splitlines(keepends=True)
                            content_lines_cmp = [l.rstrip('\n').strip() for l in content_lines_raw]
                            old_lines_cmp     = [l.strip() for l in old_lines_raw]
                            if len(old_lines_cmp) >= 2:
                                sm = _difflib.SequenceMatcher(None, content_lines_cmp, old_lines_cmp, autojunk=False)
                                best = sm.find_longest_match(0, len(content_lines_cmp), 0, len(old_lines_cmp))
                                # Require at least 2 matching lines AND >60% of old lines covered
                                if best.size >= 2 and best.size / max(len(old_lines_cmp), 1) >= 0.6:
                                    # old[best.b : best.b+best.size] matches content[best.a : best.a+best.size]
                                    # Extend the match to cover the full `old` span in content
                                    start_in_content = best.a - best.b                   # offset by where old starts relative to anchor
                                    end_in_content   = start_in_content + len(old_lines_cmp)
                                    start_in_content = max(0, start_in_content)
                                    end_in_content   = min(len(content_lines_raw), end_in_content)
                                    if start_in_content < end_in_content:
                                        print(f"[SURGICAL] difflib matched lines {start_in_content}–{end_in_content} (block size={best.size})", flush=True)
                                        return (
                                            "".join(content_lines_raw[:start_in_content]) + new + "\n" +
                                            "".join(content_lines_raw[end_in_content:]),
                                            True,
                                        )

                            return content, False

                        applied = 0
                        for edit_op in edit_ops:
                            old_text = edit_op.get("old", "")
                            new_text = edit_op.get("new", "")
                            summary  = edit_op.get("summary", "")
                            if not old_text:
                                continue
                            file_content, matched = _find_and_replace(file_content, old_text, new_text)
                            if matched:
                                applied += 1
                                if summary:
                                    yield f"__FORGE_THOUGHT__:{path}: {summary}"
                            else:
                                print(f"[SURGICAL] ⚠ edit not applied (old not found): {repr(old_text[:80])}", flush=True)

                        skipped = len(edit_ops) - applied
                        print(f"[SURGICAL] ✓ {path}: {applied}/{len(edit_ops)} edits applied ({skipped} skipped)", flush=True)
                        if skipped:
                            yield f"__FORGE_STATUS__:{path}: {applied}/{len(edit_ops)} edits applied ({skipped} skipped — old text not found)"

                        if applied == 0:
                            # Zero edits matched — only THEN fall back to streaming whole file.
                            # Partial success (applied > 0) uses the patched content as-is.
                            raise ValueError("no edits matched, falling back to whole-file")

                        # Emit the updated content as tokens so the Monaco editor refreshes
                        CHUNK = 400
                        for i in range(0, len(file_content), CHUNK):
                            yield file_content[i:i+CHUNK]

                    except Exception as e:
                        print(f"[SURGICAL] ✗ {path}: surgical edit failed ({e}), falling back to whole-file stream", flush=True)
                        yield f"__FORGE_STATUS__:Falling back to full rewrite of {path}…"
                        # Fall back to streaming whole file
                        fallback_user = (
                            f"Instruction: {prompt}\n\nFile: {path}\n\n"
                            f"Current content:\n{current_content}\n\n"
                            "Output ONLY the complete final file content. No fences. No explanation."
                        )
                        content_parts: list[str] = []
                        async for token in self._stream(FILE_UPDATE_STREAMING_PROMPT, [{"role": "user", "content": fallback_user}]):
                            if token.startswith("__FORGE_ERROR__"):
                                yield token; break
                            if any(token.startswith(p) for p in _META_PREFIXES):
                                yield token; continue
                            content_parts.append(token)
                            yield token
                        if content_parts:
                            file_content = self._strip_fences("".join(content_parts))

                else:
                    # CREATE — stream the full new file
                    file_user = (
                        f"Instruction: {prompt}\n\n"
                        f"Operation: create\nFile path: {path}\n\n"
                        + (
                            "Existing files for style/imports/routing reference:\n"
                            + "\n\n".join(
                                f"// FILE: {ctx_path}\n{ctx_content[:12000]}"
                                for ctx_path, ctx_content in list(context_files.items())[:4]
                                if ctx_path != path
                            )
                            + "\n\n"
                            if context_files else ""
                        )
                        + (
                            f"Other planned operations:\n{other_summary}\n\n"
                            if len(planned_ops) > 1 else ""
                        )
                        + "Output ONLY the complete file content. No fences. No explanation."
                    )

                    content_parts_create: list[str] = []
                    MAX_FILE_RETRY = 2
                    for attempt in range(1, MAX_FILE_RETRY + 1):
                        content_parts_create = []
                        try:
                            async for token in self._stream(FILE_UPDATE_STREAMING_PROMPT, [{"role": "user", "content": file_user}]):
                                if token.startswith("__FORGE_ERROR__"):
                                    yield token; raise RuntimeError(token)
                                if any(token.startswith(p) for p in _META_PREFIXES):
                                    yield token; continue
                                content_parts_create.append(token)
                                yield token
                            break
                        except Exception as e:
                            if attempt < MAX_FILE_RETRY:
                                yield f"__FORGE_STATUS__:Retrying {path}… (attempt {attempt})"
                                await _asyncio.sleep(attempt)
                            else:
                                yield f"__FORGE_ERROR__:Failed to create {path}: {e}"
                    file_content = self._strip_fences("".join(content_parts_create))

                assembled_update.append({
                    "path":        path,
                    "content":     file_content,
                    "description": "Created" if op == "create" else "Updated",
                    "language":    path.rsplit(".", 1)[-1] if "." in path else "text",
                })

                size_kb = len(file_content) / 1024
                yield f"__FORGE_FILE_DONE__:{_json.dumps({'path': path, 'size_kb': round(size_kb, 1)})}"
                yield f"__FORGE_STATUS__:{path} {'created' if op == 'create' else 'edited'} ({size_kb:.1f}kb)"
                print(f"[UPDATE] ✓ {op} {path} ({len(file_content)} chars)", flush=True)

            # Emit result metadata only (files already streamed)
            result_meta = {
                "project_name":   project_name,
                "description":    f"Updated: {prompt}",
                "tech_stack":     [],
                "setup_commands": [],
                "run_command":    "",
                "file_count":     len(assembled_update),
                "deleted_paths":   deleted_paths,
                "is_update":      True,
            }
            yield f"__FORGE_RESULT__:{_json.dumps(result_meta)}"
            return

        # ── NEW PROJECT path: two-phase plan → stream each file ──────────────

        # ── Image context helper ──────────────────────────────────────────────
        # When the user uploaded a reference design, build a multipart content
        # block so vision-capable models can see the image.  Text-only models
        # will receive only the text portion and generate from the description.
        def _make_image_content(text: str) -> list | str:
            if not image_base64:
                return text
            mime = image_type or "image/png"
            return [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{image_base64}"},
                },
                {"type": "text", "text": text},
            ]

        # ── Image design analysis pre-pass ────────────────────────────────────
        # Before planning, do a quick vision call that turns the reference image
        # into a concrete text spec (colors, layout, components, spacing, effects).
        # This spec is then injected into every subsequent prompt so the model
        # always generates to explicit requirements rather than guessing from bytes.
        import re as _re

        design_spec = ""
        if image_base64:
            yield f"__FORGE_STATUS__:Analysing reference design…"
            design_spec = await self._analyze_image_design(image_base64, image_type)
            if design_spec:
                # Surface the extracted spec in the thought block so users can verify
                yield f"__FORGE_THOUGHT__:Design spec extracted from reference image:\n{design_spec}"

        # Build the plan prompt
        plan_user = f"Project request: {prompt}"
        if language != "auto":
            plan_user += f"\nLanguage/Framework: {language}"
        # Stack config — inject explicit technology choices so the plan matches
        if stack and isinstance(stack, dict):
            fe  = stack.get("fe", "none")
            be  = stack.get("be", "none")
            db  = stack.get("db", "none")
            stack_lines = [f"Frontend: {fe}"]
            if be != "none":
                stack_lines.append(f"Backend: {be}")
            if db != "none":
                stack_lines.append(f"Database: {db}")
            plan_user += "\nSelected stack:\n" + "\n".join(f"  - {l}" for l in stack_lines)
            plan_user += (
                "\nIMPORTANT: You MUST use exactly this stack. "
                "Do not suggest alternative frameworks or switch to a different technology."
            )
        if extra_context:
            plan_user += f"\nExtra context: {extra_context}"
        if image_base64:
            plan_user += (
                "\n\n"
                "╔══════════════════════════════════════════════════════════════╗\n"
                "║         REFERENCE DESIGN IMAGE — VISUAL CLONING REQUIRED    ║\n"
                "╚══════════════════════════════════════════════════════════════╝\n"
                "A reference design image has been provided. The generated project\n"
                "MUST be a visual clone of it — exact colors, layout, typography,\n"
                "spacing, backgrounds, and component styles. This image overrides\n"
                "ALL default design decisions. It is the single source of truth."
            )
            if design_spec:
                plan_user += (
                    "\n\n"
                    "REFERENCE DESIGN SPECS (extracted from the image — implement these exactly):\n"
                    "────────────────────────────────────────────────────────────────\n"
                    f"{design_spec}\n"
                    "────────────────────────────────────────────────────────────────"
                )

        # ── Phase 1: plan with live heartbeat yields ─────────────────────────
        # We inline the API call here (instead of delegating to _non_stream_call)
        # so we can yield a status heartbeat every 2 s while the model thinks.
        # Without these yields, the SSE connection is silent for 10-15 s and the
        # browser shows no progress at all.

        yield f"__FORGE_STATUS__:Planning your project…"

        plan: dict | None = None
        MAX_PLAN_RETRY = 3

        for plan_attempt in range(1, MAX_PLAN_RETRY + 1):
            try:
                loop        = _asyncio.get_running_loop()
                plan_messages = [
                    {"role": "system", "content": PLAN_SYSTEM_PROMPT},
                    {"role": "user",   "content": _make_image_content(plan_user)},
                ]
                plan_future = loop.run_in_executor(
                    None,
                    lambda: self.client.chat.completions.create(
                        model       = self.model,
                        messages    = plan_messages,
                        max_tokens  = 8192,   # thinking models (Gemini 2.5-flash, DeepSeek-R1) burn
                        temperature = 0.1,    # reasoning tokens before output — 2048 left ~200 for JSON
                        stream      = False,
                        timeout     = 120,
                    ),
                )
                start_ts = _time.time()
                while True:
                    done, _ = await _asyncio.wait({plan_future}, timeout=2.0)
                    if done:
                        break
                    elapsed = int(_time.time() - start_ts)
                    yield f"__FORGE_STATUS__:Planning… ({elapsed}s)"   # ← heartbeat

                response  = await plan_future
                plan_text = (response.choices[0].message.content or "").strip()
                print(f"[codegen] ← plan done ({len(plan_text)} chars)", flush=True)

                # ── Robust JSON extraction ────────────────────────────────────
                # Strip markdown fences with regex (not character-level lstrip)
                plan_text = _re.sub(r'^```(?:json)?\s*', '', plan_text, flags=_re.MULTILINE)
                plan_text = _re.sub(r'\s*```\s*$',        '', plan_text, flags=_re.MULTILINE)
                plan_text = plan_text.strip()

                # Isolate the outermost { ... }
                brace_start = plan_text.find('{')
                brace_end   = plan_text.rfind('}') + 1
                if brace_start >= 0 and brace_end > brace_start:
                    plan_text = plan_text[brace_start:brace_end]

                # Parse — fall back to truncating at last depth-0 brace on error
                try:
                    plan = _json.loads(plan_text)
                except _json.JSONDecodeError:
                    depth, in_str, escape, last_safe = 0, False, False, -1
                    for i, ch in enumerate(plan_text):
                        if escape:                escape = False;  continue
                        if ch == '\\' and in_str: escape = True;  continue
                        if ch == '"':             in_str = not in_str; continue
                        if in_str:                continue
                        if ch == '{':             depth += 1
                        elif ch == '}':
                            depth -= 1
                            if depth == 0: last_safe = i
                    if last_safe >= 0:
                        plan = _json.loads(plan_text[:last_safe + 1])
                    else:
                        raise ValueError("No complete JSON object in plan response")
                break   # success

            except Exception as e:
                tb = traceback.format_exc()
                print(f"[forge] ✗ plan attempt {plan_attempt}: {e}\n{tb}", file=sys.stderr, flush=True)
                if plan_attempt < MAX_PLAN_RETRY:
                    yield f"__FORGE_STATUS__:Retrying plan… (attempt {plan_attempt})"
                    await _asyncio.sleep(plan_attempt * 2)
                else:
                    human = self._humanize_error(e, self.model)
                    yield f"__FORGE_ERROR__:Plan failed after {MAX_PLAN_RETRY} attempts. {human}"
                    return

        # Sanitize project_name: convert kebab-case / snake_case to Title Case
        raw_name = plan.get("project_name", "My Project")
        if raw_name and ("-" in raw_name or "_" in raw_name) and raw_name == raw_name.lower():
            plan["project_name"] = " ".join(
                w.capitalize() for w in raw_name.replace("_", "-").split("-") if w
            )

        print(f"[forge] plan: {len(plan.get('files', []))} file(s) → {[f['path'] for f in plan.get('files', [])]}", flush=True)
        yield f"__FORGE_PLAN__:{_json.dumps(plan)}"

        # ── Phase 2: stream each file ─────────────────────────────────────────
        file_list_summary = "\n".join(
            f"  - {f['path']}: {f.get('description', '')}"
            for f in plan.get("files", [])
        )
        assembled: list[dict] = []

        for file_spec in plan.get("files", []):
            path = file_spec["path"]
            desc = file_spec.get("description", "")

            yield f"__FORGE_FILE_START__:{_json.dumps({'path': path, 'description': desc})}"
            yield f"__FORGE_STATUS__:Writing {path}…"

            file_user = (
                f"Project: {plan.get('project_name', '')} — {plan.get('description', '')}\n"
                f"Tech stack: {', '.join(plan.get('tech_stack', []))}\n"
                f"Original request: {prompt}\n\n"
                f"All files in this project:\n{file_list_summary}\n\n"
                f"NOW generate the complete content for: {path}\n"
                f"File purpose: {desc}"
                + (
                    "\n\n"
                    "╔══════════════════════════════════════════════════════════════╗\n"
                    "║             REFERENCE DESIGN — ABSOLUTE OVERRIDE            ║\n"
                    "╚══════════════════════════════════════════════════════════════╝\n"
                    "A reference design image was provided. Your job is to CLONE it visually.\n"
                    "Every color, font size, layout, background, spacing, and component style\n"
                    "must come from the spec below — NOT from your default design preferences.\n"
                    "Ignore the generic design defaults in your system prompt. Use the spec.\n\n"
                    + (
                        "REFERENCE DESIGN SPECS:\n"
                        "────────────────────────────────────────────────────────────────\n"
                        f"{design_spec}\n"
                        "────────────────────────────────────────────────────────────────\n"
                        "Implement these specs exactly. Use the Tailwind classes listed.\n"
                        "Use the hex colors listed. Use the layout structure described.\n"
                        "For background images described in the spec: implement using the\n"
                        "CSS gradient approximation provided, OR if a base64 image URL is\n"
                        "available in the spec, use it as background-image.\n"
                        if design_spec else
                        "The attached image IS the design reference. Study it carefully:\n"
                        "match every color, font size, layout, spacing, and visual detail\n"
                        "you can see. Approximate photo backgrounds with CSS gradients.\n"
                    )
                    if image_base64 else ""
                )
            )
            file_messages = [
                {"role": "system", "content": FILE_GEN_SYSTEM_PROMPT},
                {"role": "user",   "content": _make_image_content(file_user)},
            ]

            # Stream this file's content directly to the client.
            # IMPORTANT: _stream() emits tagged meta-tokens (reasoning, errors, status)
            # alongside raw content tokens. We must forward them to the client for display
            # but MUST NOT include them in content_parts — only raw code goes in the file.
            _META_PREFIXES = (
                "__FORGE_REASONING__:",
                "__FORGE_STATUS__:",
                "__FORGE_ERROR__:",
            )
            content_parts: list[str] = []
            MAX_FILE_RETRY = 3

            for attempt in range(1, MAX_FILE_RETRY + 1):
                content_parts = []
                success        = False
                try:
                    async for token in self._stream(FILE_GEN_SYSTEM_PROMPT, [{"role": "user", "content": _make_image_content(file_user)}]):
                        if token.startswith("__FORGE_ERROR__"):
                            yield token   # forward the error event
                            raise RuntimeError(token)
                        if any(token.startswith(p) for p in _META_PREFIXES):
                            yield token   # pass through for status/reasoning display…
                            continue      # …but do NOT add to file content
                        content_parts.append(token)
                        yield token   # ← raw file content streams to client as token events
                    success = True
                    break
                except Exception as e:
                    if attempt < MAX_FILE_RETRY:
                        yield f"__FORGE_STATUS__:Retrying {path}… (attempt {attempt}/{MAX_FILE_RETRY})"
                        await _asyncio.sleep(attempt)
                        continue
                    yield f"__FORGE_ERROR__:Failed to generate {path}: {e}"

            file_content = self._strip_fences("".join(content_parts))
            assembled.append({
                "path":        path,
                "content":     file_content,
                "description": desc,
                "language":    path.rsplit(".", 1)[-1] if "." in path else "text",
            })

            size_kb = len(file_content) / 1024
            yield f"__FORGE_FILE_DONE__:{_json.dumps({'path': path, 'size_kb': round(size_kb, 1)})}"
            yield f"__FORGE_STATUS__:{path} done ({size_kb:.1f}kb)"
            print(f"[forge] ✓ {path} ({len(file_content)} chars)", flush=True)

        # ── Emit result metadata (NO file contents) ───────────────────────────
        # The client already has all file contents from streaming (file_start →
        # tokens → file_done). Re-sending them here would create a 100-200 KB
        # SSE event that reliably fails JSON.parse in the browser's catch {}.
        # The client builds the final CodeProject from its local assembledFiles.
        result_meta = {
            "project_name":   plan.get("project_name", "project"),
            "description":    plan.get("description", ""),
            "tech_stack":     plan.get("tech_stack", []),
            "setup_commands": plan.get("setup_commands", []),
            "run_command":    plan.get("run_command", ""),
            "file_count":     len(assembled),
        }
        yield f"__FORGE_RESULT__:{_json.dumps(result_meta)}"

    # ── File update ───────────────────────────────────────────────────────────

    async def update_file(
        self,
        file_path: str,
        current_content: str,
        instruction: str,
        full_context: list[dict] = [],
    ) -> AsyncIterator[str]:
        context_block = ""
        if full_context:
            snippets = [
                f"// {f['path']}\n{f['content'][:300]}..."
                for f in full_context[:5] if f["path"] != file_path
            ]
            context_block = "\n\nOther files for context:\n" + "\n\n".join(snippets)

        user_msg = (
            f"File: {file_path}\n\n"
            f"Current content:\n```\n{current_content}\n```\n\n"
            f"Instruction: {instruction}"
            f"{context_block}"
        )

        async for token in self._stream(
            FILE_UPDATE_SYSTEM_PROMPT,
            [{"role": "user", "content": user_msg}],
        ):
            yield token

    # ── Intent classification ─────────────────────────────────────────────────

    async def classify_intent(
        self,
        message: str,
        has_project: bool,
        project_name: str = "",
    ) -> str:
        """
        Returns 'chat', 'build', or 'update'.
        Uses max_tokens=5 + temperature=0 so it's near-instant (single token).
        """
        context_line = (
            f"Existing project: {project_name}" if has_project
            else "No existing project yet."
        )
        user_content = f"{context_line}\n\nUser message: {message}"

        all_messages = [
            {"role": "system", "content": INTENT_SYSTEM_PROMPT},
            {"role": "user",   "content": user_content},
        ]

        loop  = asyncio.get_event_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def _run_sync():
            try:
                resp = self.client.chat.completions.create(
                    model       = self.model,
                    messages    = all_messages,
                    max_tokens  = 5,
                    temperature = 0.0,
                    stream      = False,
                )
                token = (resp.choices[0].message.content or "").strip().lower()
                loop.call_soon_threadsafe(queue.put_nowait, token)
            except Exception:
                loop.call_soon_threadsafe(queue.put_nowait, "chat")

        loop.run_in_executor(None, _run_sync)
        raw = await queue.get()

        if "build"  in raw: return "build"
        if "update" in raw: return "update"
        return "chat"

    # ── Project context summary ───────────────────────────────────────────────

    async def generate_context_summary(self, files: list[dict]) -> str:
        """
        Read ALL project files and write a compact, AI-generated context summary.
        Stored in the DB; prepended to every future update request so the model
        can make design-consistent changes without re-reading every file.

        Returns the summary string, or "" on failure (non-critical).
        """
        import json as _json

        # Build a condensed file listing (cap each file at 4000 chars to stay within context)
        MAX_FILE_CHARS = 4000
        file_blocks = []
        for f in files:
            content = f.get("content", "")
            if len(content) > MAX_FILE_CHARS:
                content = content[:MAX_FILE_CHARS] + f"\n... [{len(content) - MAX_FILE_CHARS} chars truncated]"
            file_blocks.append(f"// FILE: {f['path']}\n{content}")

        files_text = "\n\n---\n\n".join(file_blocks)

        messages = [
            {"role": "system", "content": CONTEXT_SUMMARY_PROMPT},
            {"role": "user",   "content": f"Project files:\n\n{files_text}"},
        ]

        print(
            f"[context] generating summary for {len(files)} files "
            f"({sum(len(f.get('content','')) for f in files)//1024}KB total)",
            flush=True,
        )

        try:
            summary = await self._non_stream_call(
                messages,
                max_tokens=800,
                temperature=0.2,
                label="context_summary",
            )
            print(f"[context] summary generated ({len(summary)} chars)", flush=True)
            return summary.strip()
        except Exception as e:
            print(f"[context] summary generation failed: {e}", flush=True)
            return ""

    # ── Chat ──────────────────────────────────────────────────────────────────

    async def chat(
        self,
        messages: list[dict],
        codebase_context: list[dict] | None = None,
        system_override: str | None = None,
        runtime_context: str | None = None,
    ) -> AsyncIterator[str]:
        system = system_override if system_override else CHAT_SYSTEM_PROMPT
        if codebase_context and not system_override:
            files_summary = "\n".join(f"- {f['path']}" for f in codebase_context)
            system += f"\n\nCurrent project files:\n{files_summary}"
        # Runtime evidence (app state + terminal tail + console errors).
        # Appended last so it doesn't get truncated by the model when the
        # static prompt is long. This is the ground truth the model should
        # consult before claiming a fix or asking the user a redundant question.
        if runtime_context:
            system += f"\n\n{runtime_context}"

        async for token in self._stream(system, messages):
            yield token
