# Forge verify subagent — runbook

Owner: ronak. Files touched by the verify pipeline:

```
opencode/packages/opencode/src/agent/prompt/verify.txt    # system prompt
opencode/packages/opencode/src/agent/agent.ts             # native agent registration
opencode/packages/opencode/src/plugin/forge-verify.ts     # orchestration plugin
opencode/packages/opencode/src/plugin/index.ts            # INTERNAL_PLUGINS entry
opencode/packages/opencode/src/forge/runtime-state.ts     # shared agent tracker + SSE predicate
opencode/packages/opencode/src/server/routes/instance/httpapi/handlers/global.ts   # SSE filter
```

## How it works (end to end)

1. User prompts Forge. Main `build` agent does its thing.
2. Each LLM step the build agent runs emits a `session.next.step.started` event with `agent: "build"`. `forge-verify.ts` records this in `ForgeRuntimeState.activeAgent`.
3. When the build agent finishes its **final** step (`session.next.step.ended` with `finish: "stop"`), the plugin spawns the verify subagent via `session.promptAsync({ agent: "verify", ... })`.
4. The verify agent (system prompt at `agent/prompt/verify.txt`) runs `docker_info.sh` and `fix_loop.sh`, parses errors with `parse_errors.py`, and either:
   - Fixes mechanical errors itself (missing import, port collision, etc.), loops until clean, emits `"Fixed it — the app is up and running."`
   - Detects a non-mechanical error and emits an `escalate: true` JSON handoff
5. The plugin polls `session.messages` for verify's final text. If it finds an escalation, it re-prompts the **build** agent with the error and suspected files. Build addresses it → step.ended → verify re-runs → loop continues until clean or budget exhausted.
6. Throughout, the SSE handler at `handlers/global.ts` drops `tool.*`, `reasoning.*`, `shell.*`, and `retried` events whose session's active agent is `verify`. FE only sees text and step lifecycle.

## Budgets (must agree between verify.txt and forge-verify.ts)

- 5 attempts per identical error signature (first 80 chars of error message)
- 8 total attempts per session
- 10 minutes wall clock per session
- Verify itself self-enforces in its system prompt; the plugin also enforces. Either tripping ends the loop cleanly.

## Hard rules baked in

- Plugin is a **no-op unless `FORGE_PROJECT_ID` env is set**. Non-Forge opencode users are unaffected.
- Verify agent's scope rules (verify.txt): never leaves project workspace, never reads platform code, never touches sibling projects, never `docker ps` other containers.
- SSE filter is conservative: any unknown state → don't hide. Worst case is a tool event leaking, not a real user message being swallowed.
- Verify never invokes the main agent itself. The plugin routes the escalation. This keeps "main AI not involved in checks" — main only gets called for fixes that need its context.

## End-to-end test plan

Run from the Forge dev env (`./dev.sh`):

1. **Sanity — clean project, no verify trigger.**
   Create a fresh Forge project. Prompt: "Just say hello in App.tsx."
   Expected: Build agent edits file. Verify spawns. Container probe finds nothing broken. Verify exits silently. FE shows only main agent's response — no extra status line.

2. **Mechanical fix — verify auto-recovers.**
   Prompt: "Add a books page that fetches /api/books."
   Then manually rename `src/lib/db/schema.ts`'s `items` export to something the route file doesn't import. Trigger a rebuild.
   Expected: Container logs show import error. FE shows one line: `"There's a missing import in the books route. Solving it now."` Verify fixes import. FE shows: `"Fixed it — the app is up and running."` No docker/container/log talk visible.

3. **Escalation — handoff to build agent.**
   Prompt: "Build a contact form that posts to /api/contacts." After build agent finishes, manually edit the schema column name so the route's INSERT will 500.
   Expected: Verify detects 5xx on /api/contacts endpoint, can't classify as mechanical, emits escalate JSON. Plugin re-prompts build agent. Build fixes the schema/route mismatch. Verify re-runs, passes. User sees only sanitized status lines, no JSON.

4. **Budget exhaustion — clean fail.**
   Intentionally introduce a circular dep that verify can't fix in 5 attempts.
   Expected: After 5th attempt on same signature, plugin stops re-spawning. Verify emits: `"I couldn't auto-fix this — <plain English summary>. Want me to try a different approach?"` No infinite loop. No container logs in FE.

5. **No FE leaks audit.**
   In all of the above, tail the SSE stream (`/api/event` or whatever the FE endpoint is) and grep for: `docker`, `container`, `forge-proj-`, `/app/`, `/forge-data/`, `cat `, `bash`, `EADDRINUSE`. Should find zero hits.

6. **Non-Forge regression.**
   Run opencode outside Forge (no `FORGE_PROJECT_ID`). Verify the plugin logs "not a Forge project, plugin idle" and contributes no hooks. Existing opencode flows must be byte-identical to before.

## Known risk surfaces (worth a second look during review)

- **30s polling window in plugin.** If verify takes >30s, escalation parsing is skipped that round. Increase the deadline or switch to event-driven (watch `Text.Ended` for the verify agent) if integration shows this is an issue.
- **promptAsync queuing.** Plugin assumes calling `promptAsync` mid-event-handler queues cleanly without double-prompting. Confirm during integration test #1.
- **`finish === "stop"` on multi-step turns.** Build agent's intermediate steps should finish with `"tool-calls"`, only the final one with `"stop"`. If a real run shows verify firing mid-turn, the guard needs tightening.

## Rollback

Revert these files only (no DB migrations, no config changes):
- `agent/prompt/verify.txt` — delete
- `agent/agent.ts` — drop the `verify:` entry
- `plugin/forge-verify.ts` — delete
- `plugin/index.ts` — remove ForgeVerifyPlugin import + INTERNAL_PLUGINS entry
- `forge/runtime-state.ts` — delete
- `server/routes/instance/httpapi/handlers/global.ts` — remove the Stream.filter line + import

The SSE filter and plugin are both inert without `FORGE_PROJECT_ID` set, so even partial rollback leaves non-Forge users unaffected.
