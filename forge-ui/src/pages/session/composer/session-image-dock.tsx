/**
 * session-image-dock.tsx
 * =======================
 * Compact chip shown in the composer when there are AI image-gen jobs in
 * flight for this project. Renders nothing when no jobs are pending so it
 * costs zero pixels for users who never turn image-gen on.
 *
 * Polling, not SSE: image-gen for a single project completes in seconds
 * (Flux Schnell ~2s; Grok Imagine ~6s). A 3-second poll is fast enough to
 * feel reactive without adding a new server push channel. Loop stops when
 * no jobs are pending so the steady-state cost is 0.
 *
 * Token cost: this only hits forge-server (not opencode), so it never
 * enters the chat context. Flat per session.
 */
import { type Component, createEffect, createSignal, For, onCleanup, Show } from "solid-js"
import { useSDK } from "@/context/sdk"
import { useForgeApi, type ImageJobInfo } from "@/context/forge-api"

const POLL_MS = 3000

function deriveProjectId(directory?: string | null): string | null {
  if (!directory) return null
  const m = directory.match(/\/projects\/([a-f0-9-]{8,}[a-f0-9])\/workspace/)
  return m?.[1] ?? null
}

export const SessionImageDock: Component = () => {
  const forge = useForgeApi()
  const sdk   = useSDK()

  const [jobs,    setJobs]    = createSignal<ImageJobInfo[]>([])
  const [open,    setOpen]    = createSignal(false)
  // ticker drives the polling loop; bumped by the timer or by user action.
  const [ticker,  setTicker]  = createSignal(0)

  let timer: ReturnType<typeof setTimeout> | undefined

  function clearTimer() {
    if (timer !== undefined) {
      clearTimeout(timer)
      timer = undefined
    }
  }

  function scheduleNext(pending: boolean) {
    clearTimer()
    // Only re-poll when something is in flight. Steady-state cost = 0.
    if (!pending) return
    timer = setTimeout(() => setTicker((t) => t + 1), POLL_MS)
  }

  createEffect(() => {
    void ticker()
    const pid = deriveProjectId(sdk.directory)
    if (!pid) {
      setJobs([])
      clearTimer()
      return
    }
    let cancelled = false
    ;(async () => {
      try {
        // Fetch a small recent window — anything more is wasted bytes for
        // the chip view.
        const recent = await forge.listProjectImages(pid, { limit: 20 })
        if (cancelled) return
        setJobs(recent)
        const pending = recent.some((j) => j.status === "queued" || j.status === "running")
        scheduleNext(pending)
      } catch {
        // 401/network: stop polling silently — the rest of the session
        // will surface the auth issue elsewhere.
        if (!cancelled) clearTimer()
      }
    })()
    onCleanup(() => { cancelled = true })
  })

  onCleanup(clearTimer)

  // Derived counters
  const pending = () => jobs().filter((j) => j.status === "queued" || j.status === "running").length
  const ready   = () => jobs().filter((j) => j.status === "done").length
  const failed  = () => jobs().filter((j) => j.status === "failed").length
  const total   = () => jobs().length

  // Don't render unless there's something to say. Don't add chrome to the
  // composer just to say "0 of 0."
  const hasAny = () => total() > 0 && (pending() > 0 || failed() > 0 || open())

  return (
    <Show when={hasAny()}>
      <div class="mx-2 mb-1.5 rounded-md border border-border-weak-base bg-surface-base">
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          class="w-full flex items-center gap-2 px-3 py-1.5 text-13-regular text-left"
        >
          <Show when={pending() > 0}>
            <span class="inline-block size-2 rounded-full bg-text-accent animate-pulse" />
          </Show>
          <Show when={pending() === 0 && failed() > 0}>
            <span class="inline-block size-2 rounded-full bg-text-danger" />
          </Show>
          <Show when={pending() === 0 && failed() === 0}>
            <span class="inline-block size-2 rounded-full bg-text-success" />
          </Show>
          <span class="text-text-strong">
            Images:
          </span>
          <span class="text-text-base">
            {ready()}/{total()} ready
            <Show when={failed() > 0}>
              <span class="text-text-danger"> · {failed()} failed</span>
            </Show>
          </span>
          <span class="ml-auto text-text-weak text-12-regular">{open() ? "Hide" : "Show"}</span>
        </button>
        <Show when={open()}>
          <div class="px-3 pb-2 pt-1 flex flex-col gap-1 max-h-48 overflow-y-auto no-scrollbar">
            <For each={jobs()}>
              {(j) => (
                <div class="flex items-center gap-2 text-12-regular py-0.5">
                  <span
                    class="inline-block size-1.5 rounded-full"
                    classList={{
                      "bg-text-accent":  j.status === "queued" || j.status === "running",
                      "bg-text-success": j.status === "done",
                      "bg-text-danger":  j.status === "failed",
                    }}
                  />
                  <span class="text-text-weak shrink-0 w-12 uppercase">{j.status}</span>
                  <span class="text-text-base truncate flex-1" title={j.prompt}>
                    {j.prompt}
                  </span>
                  <Show when={j.status === "failed" && j.error}>
                    <span class="text-text-danger shrink-0">{j.error}</span>
                  </Show>
                </div>
              )}
            </For>
          </div>
        </Show>
      </div>
    </Show>
  )
}
