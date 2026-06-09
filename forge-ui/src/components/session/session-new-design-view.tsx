import type { JSX } from "solid-js"
import { createEffect, createResource, createSignal, For, Show } from "solid-js"
import { useNavigate } from "@solidjs/router"
import { base64Encode } from "@opencode-ai/core/util/encode"
import { usePrompt } from "@/context/prompt"
import { useForgeApi, type ShowcaseProject } from "@/context/forge-api"

// ── Deterministic placeholder color from project id ───────────────────────────

const PLACEHOLDER_GRADIENTS: [string, string][] = [
  ["#6366f1", "#8b5cf6"],  // indigo → violet
  ["#f59e0b", "#ef4444"],  // amber → red
  ["#10b981", "#06b6d4"],  // emerald → cyan
  ["#ec4899", "#8b5cf6"],  // pink → violet
  ["#3b82f6", "#6366f1"],  // blue → indigo
  ["#f97316", "#f59e0b"],  // orange → amber
  ["#14b8a6", "#3b82f6"],  // teal → blue
  ["#a855f7", "#ec4899"],  // purple → pink
]

function projectGradient(id: string): [string, string] {
  let hash = 0
  for (let i = 0; i < id.length; i++) {
    hash = (hash << 5) - hash + id.charCodeAt(i)
    hash |= 0
  }
  return PLACEHOLDER_GRADIENTS[Math.abs(hash) % PLACEHOLDER_GRADIENTS.length]
}

// ── Showcase preview modal ────────────────────────────────────────────────────

function ShowcasePreviewModal(props: {
  project: ShowcaseProject
  onClose: () => void
  onOpen:  () => void
}) {
  const forge = useForgeApi()
  const [starting, setStarting] = createSignal(props.project.container_status !== "running")

  // preview_url is an absolute http(s) URL from the API
  // (http://{project_id}.{PREVIEW_DOMAIN}/ — Traefik routes by host).
  const previewUrl = () => props.project.preview_url ?? ""

  // Auto-wake the container when modal opens
  createEffect(() => {
    if (props.project.container_status !== "running") {
      setStarting(true)
      forge.ensure(props.project.id).then(() => setStarting(false)).catch(() => setStarting(false))
    } else {
      setStarting(false)
    }
  })

  return (
    <div
      class="fixed inset-0 z-50 flex items-center justify-center"
      style={{ background: "rgba(0,0,0,0.72)", "backdrop-filter": "blur(6px)" }}
      onClick={props.onClose}
    >
      <div
        class="relative flex flex-col overflow-hidden"
        style={{
          width: "min(92vw, 1140px)",
          height: "min(90vh, 800px)",
          "border-radius": "14px",
          background: "var(--bg, #1a1a1a)",
          "box-shadow": "0 32px 80px rgba(0,0,0,0.6)",
        }}
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div
          class="flex items-center justify-between px-5 py-3 shrink-0"
          style={{ "border-bottom": "1px solid rgba(255,255,255,0.08)" }}
        >
          <div class="flex items-center gap-3 min-w-0">
            <div class="text-sm font-semibold truncate" style={{ color: "var(--ink, #e8e8e8)" }}>
              {props.project.name}
            </div>
            <Show when={props.project.stack}>
              <span
                class="px-2 py-0.5 rounded text-xs shrink-0"
                style={{ background: "rgba(255,255,255,0.08)", color: "rgba(255,255,255,0.5)" }}
              >
                {props.project.stack}
              </span>
            </Show>
            <Show when={starting()}>
              <span class="text-xs shrink-0" style={{ color: "rgba(255,255,255,0.35)" }}>
                Starting preview…
              </span>
            </Show>
          </div>
          <div class="flex items-center gap-2 shrink-0">
            {/* Open project button */}
            <button
              class="flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-md transition-colors"
              style={{ background: "rgba(255,255,255,0.08)", color: "rgba(255,255,255,0.7)" }}
              onClick={props.onOpen}
            >
              <svg class="size-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>
                <path d="M18 2h4v4M14 10l6-6"/>
              </svg>
              Open in editor
            </button>
            <button
              class="flex items-center justify-center size-7 rounded-md transition-colors hover:bg-white/10"
              style={{ color: "rgba(255,255,255,0.4)" }}
              onClick={props.onClose}
            >
              <svg class="size-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
                <path d="M18 6L6 18M6 6l12 12"/>
              </svg>
            </button>
          </div>
        </div>
        {/* Preview iframe */}
        <div class="flex-1 min-h-0 bg-white">
          <iframe
            src={previewUrl()}
            class="w-full h-full border-0"
            title={props.project.name}
            sandbox="allow-scripts allow-same-origin allow-forms allow-modals allow-popups"
          />
        </div>
      </div>
    </div>
  )
}

// ── Showcase card ─────────────────────────────────────────────────────────────

function ShowcaseCard(props: {
  project:  ShowcaseProject
  baseUrl:  string
  onPreview: () => void
}) {
  const [colors] = createSignal(projectGradient(props.project.id))
  const thumbnailSrc = () =>
    props.project.thumbnail_url
      ? `${props.baseUrl}${props.project.thumbnail_url}`
      : null

  return (
    <button
      type="button"
      class="group relative flex flex-col overflow-hidden text-left cursor-pointer transition-all duration-150 hover:-translate-y-0.5"
      style={{ "border-radius": "10px", border: "1px solid rgba(255,255,255,0.07)" }}
      onClick={props.onPreview}
    >
      {/* Thumbnail or gradient placeholder */}
      <div class="relative shrink-0 overflow-hidden" style={{ height: "120px" }}>
        <Show
          when={thumbnailSrc()}
          fallback={
            <div
              class="absolute inset-0"
              style={{
                background: `linear-gradient(135deg, ${colors()[0]}, ${colors()[1]})`,
                opacity: "0.85",
              }}
            >
              <div class="absolute inset-0 flex items-end p-3">
                <span
                  class="text-sm font-semibold leading-tight text-white/90 line-clamp-2"
                  style={{ "text-shadow": "0 1px 4px rgba(0,0,0,0.4)" }}
                >
                  {props.project.name}
                </span>
              </div>
            </div>
          }
        >
          {(src) => (
            <img
              src={src()}
              alt={props.project.name}
              class="absolute inset-0 w-full h-full object-cover object-top"
              loading="lazy"
            />
          )}
        </Show>

        {/* Hover play overlay */}
        <div
          class="absolute inset-0 flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity duration-150"
          style={{ background: "rgba(0,0,0,0.4)" }}
        >
          <div
            class="flex items-center gap-1.5 px-3 py-1.5 rounded-full text-xs font-semibold text-white"
            style={{ background: "rgba(0,0,0,0.6)", "backdrop-filter": "blur(4px)" }}
          >
            <svg class="size-3" viewBox="0 0 24 24" fill="currentColor">
              <path d="M8 5v14l11-7z"/>
            </svg>
            Preview
          </div>
        </div>
      </div>

      {/* Card footer */}
      <div class="px-3 py-2.5" style={{ background: "rgba(255,255,255,0.03)" }}>
        <div class="flex items-center gap-2 min-w-0">
          <span class="text-xs font-[530] truncate" style={{ color: "rgba(255,255,255,0.75)" }}>
            {props.project.name}
          </span>
          <Show when={props.project.stack}>
            <span
              class="shrink-0 text-[10px] px-1.5 py-px rounded"
              style={{ background: "rgba(255,255,255,0.07)", color: "rgba(255,255,255,0.4)" }}
            >
              {props.project.stack}
            </span>
          </Show>
        </div>
        <Show when={props.project.description}>
          <div class="mt-0.5 text-[11px] truncate" style={{ color: "rgba(255,255,255,0.35)" }}>
            {props.project.description}
          </div>
        </Show>
      </div>
    </button>
  )
}

// ── Main component ────────────────────────────────────────────────────────────

export function NewSessionDesignView(props: { worktree: string; children: JSX.Element }) {
  const promptCtx = usePrompt()
  const forge     = useForgeApi()
  const navigate  = useNavigate()

  const [showcases] = createResource(() => forge.listShowcases().catch(() => []))
  const [previewProject, setPreviewProject] = createSignal<ShowcaseProject | null>(null)

  const handleSuggestionClick = (title: string) => {
    promptCtx.set([{ type: "text", content: title, start: 0, end: title.length }], title.length)
  }

  const fallbackSuggestions = [
    {
      icon: () => (
        <svg class="w-[17px] h-[17px]" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
          <path d="M12 3 L13.5 9 L19 10.5 L13.5 12 L12 18 L10.5 12 L5 10.5 L10.5 9 Z"/>
          <path d="M19 17 L19.7 19.3 L22 20 L19.7 20.7 L19 23 L18.3 20.7 L16 20 L18.3 19.3 Z"/>
        </svg>
      ),
      title: "A landing for a craft brand",
      sub: "Editorial, warm tones, considered type. With a press marquee and proof grid.",
    },
    {
      icon: () => (
        <svg class="w-[17px] h-[17px]" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
          <rect x="4" y="4" width="16" height="16" rx="2"/>
        </svg>
      ),
      title: "Dashboard for a fintech SaaS",
      sub: "Cards, sparkline metrics, segmented controls, and a search-everything bar.",
    },
    {
      icon: () => (
        <svg class="w-[17px] h-[17px]" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
          <circle cx="12" cy="8" r="4"/><path d="M4 21c0-4 4-7 8-7s8 3 8 7"/>
        </svg>
      ),
      title: "Personal portfolio · journal style",
      sub: "Long-form writing first. Three index pages, tagged archives, RSS-ready.",
    },
    {
      icon: () => (
        <svg class="w-[17px] h-[17px]" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
          <circle cx="9" cy="20" r="1.5"/><circle cx="18" cy="20" r="1.5"/>
          <path d="M2 3h3l3 13h12l2-8H6"/>
        </svg>
      ),
      title: "Storefront for a small bakery",
      sub: "Six SKUs, pickup windows, Stripe checkout, photography-led layout.",
    },
    {
      icon: () => (
        <svg class="w-[17px] h-[17px]" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
          <rect x="3" y="5" width="18" height="16" rx="2"/>
          <path d="M3 9h18M8 3v4M16 3v4"/>
        </svg>
      ),
      title: "Booking flow for a salon",
      sub: "Service picker → stylist → time → confirmation. With SMS reminders.",
    },
    {
      icon: () => (
        <svg class="w-[17px] h-[17px]" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
          <path d="M13 2L4 14h7l-1 8 9-12h-7z"/>
        </svg>
      ),
      title: "Status page for an API",
      sub: "Live uptime, incident timeline, subscriber list, and an RSS feed.",
    },
  ]

  return (
    <div class="empty">
      <div class="empty-inner">
        <div class="kicker">
          <span class="line"/> A studio for taking ideas to the web
        </div>
        <h1 class="hero">
          What shall we <span class="italic">weave</span> today?
        </h1>
        <p class="hero-sub">
          Describe the site, the feeling, the audience.
          Forge drafts the structure, picks the type, and renders a working prototype —
          ready for you to taste and tune.
        </p>

        <div>
          {props.children}
        </div>

        {/* Showcase section — replaces "Or start from a thread" when there are showcased projects */}
        <Show
          when={(showcases() ?? []).length > 0}
          fallback={
            <>
              <div class="suggest-label">
                <h3>Or start from a thread</h3>
                <span class="meta">06 · curated this week</span>
              </div>
              <div class="suggest-grid">
                <For each={fallbackSuggestions}>
                  {(s) => (
                    <button class="suggest-card" onClick={() => handleSuggestionClick(s.title)}>
                      <div class="glyph">{s.icon()}</div>
                      <div class="title">{s.title}</div>
                      <div class="sub">{s.sub}</div>
                    </button>
                  )}
                </For>
              </div>
            </>
          }
        >
          <div class="suggest-label">
            <h3>Your showcase</h3>
            <span class="meta">{showcases()!.length} project{showcases()!.length !== 1 ? "s" : ""}</span>
          </div>
          <div class="suggest-grid" style={{ "grid-template-columns": "repeat(auto-fill, minmax(180px, 1fr))" }}>
            <For each={showcases()!}>
              {(project) => (
                <ShowcaseCard
                  project={project}
                  baseUrl={forge.baseUrl}
                  onPreview={() => setPreviewProject(project)}
                />
              )}
            </For>
          </div>
        </Show>
      </div>

      {/* Preview modal */}
      <Show when={previewProject()}>
        {(project) => (
          <ShowcasePreviewModal
            project={project()}
            onClose={() => setPreviewProject(null)}
            onOpen={() => {
              setPreviewProject(null)
              navigate(`/${base64Encode(project().workspace_path)}/session`)
            }}
          />
        )}
      </Show>
    </div>
  )
}
