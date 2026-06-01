import type { Session } from "@opencode-ai/sdk/v2/client"
import { createEffect, createMemo, createResource, createSignal, For, Match, onCleanup, Show, Switch } from "solid-js"
import { createStore } from "solid-js/store"
import { useQuery } from "@tanstack/solid-query"
import { Button } from "@opencode-ai/ui/button"
import { Logo } from "@opencode-ai/ui/logo"
import { Spinner } from "@opencode-ai/ui/spinner"
import { Avatar as AvatarV2 } from "@opencode-ai/ui/v2/components/avatar-v2.jsx"
import { ButtonV2 } from "@opencode-ai/ui/v2/components/button-v2.jsx"
import { Icon as IconV2 } from "@opencode-ai/ui/v2/components/icon.jsx"
import { IconButtonV2 } from "@opencode-ai/ui/v2/components/icon-button-v2.jsx"
import { getAvatarColors, useLayout, type LocalProject } from "@/context/layout"
import { useNavigate, useSearchParams } from "@solidjs/router"
import { base64Encode } from "@opencode-ai/core/util/encode"
import { Icon } from "@opencode-ai/ui/icon"
import { usePlatform } from "@/context/platform"
import { DateTime } from "luxon"
import { useDialog } from "@opencode-ai/ui/context/dialog"
import { useTheme } from "@opencode-ai/ui/theme/context"
import { DialogSelectDirectory } from "@/components/dialog-select-directory"
import { DialogSelectServer } from "@/components/dialog-select-server"
import { useServer } from "@/context/server"
import {
  currentUserInfo,
  fetchCurrentUser,
  useForgeApi,
  userInitials,
  type CurrentUser,
  type ShowcaseProject,
} from "@/context/forge-api"
import { UserMenu } from "@/components/user-menu"
import { useGlobalSync } from "@/context/global-sync"
import { useLanguage } from "@/context/language"
import { useNotification } from "@/context/notification"
import { usePermission } from "@/context/permission"
import { displayName, getProjectAvatarSource, projectForSession, sortedRootSessions } from "@/pages/layout/helpers"
import { getFilename } from "@opencode-ai/core/util/path"
import { sessionTitle } from "@/utils/session-title"
import { pathKey } from "@/utils/path-key"
import { messageAgentColor } from "@/utils/agent"
import { sessionPermissionRequest } from "@/pages/session/composer/session-request-tree"
import { useModels, type ModelKey } from "@/context/models"
import { useProviders } from "@/hooks/use-providers"
import { ModelSelectorPopover } from "@/components/dialog-select-model"
import { ProviderIcon } from "@opencode-ai/ui/provider-icon"

const USE_HOME_DESIGN = import.meta.env.VITE_OPENCODE_CHANNEL !== "prod"

// When VITE_API_URL is set, the Forge project list is shown instead of the
// generic OpenCode home. Each project routes to /{base64(workspace_path)}/session.
// VITE_API_URL is the same var used by forge-api.tsx — forge-server base URL.
const FORGE_API_URL: string | undefined = import.meta.env.VITE_API_URL || undefined

const HOME_SESSION_LIMIT = 15
const HOME_ROW =
  "flex min-w-0 w-full shrink-0 cursor-default items-center rounded-[6px] border-0 bg-transparent text-left [font-weight:530] text-v2-text-text-muted transition-colors duration-[120ms] ease-in-out hover:bg-v2-overlay-simple-overlay-hover focus-visible:bg-v2-overlay-simple-overlay-hover focus-visible:outline-none"
const HOME_PROJECT_NAV_ROW = `${HOME_ROW} h-8 gap-1.5 px-3 [&>span]:min-w-0 [&>span]:overflow-hidden [&>span]:text-ellipsis [&>span]:whitespace-nowrap`
const HOME_SECTION_LABEL = "text-v2-text-text-muted [font-weight:440]"

type HomeSessionRecord = {
  session: Session
  project: LocalProject
  projectName: string
}

type HomeSessionGroup = {
  id: "today" | "yesterday" | "older"
  title: string
  sessions: HomeSessionRecord[]
}

export default function Home() {
  if (FORGE_API_URL) return <ForgeHome />
  if (USE_HOME_DESIGN) return <HomeDesign />
  return <LegacyHome />
}

// ── Forge project list (used when VITE_FORGE_API_URL is set) ──────────────────

type ForgeProject = {
  id: string
  name: string
  description: string
  workspace_path: string
  stack: string | null
  container_status: string
  created_at: string
  updated_at: string
  showcased_at: string | null
  preview_url: string | null
  thumbnail_url: string | null
  starred_at: string | null
  forked_from_project_id: string | null
}

const STATUS_DOT: Record<string, string> = {
  running: "bg-icon-success-base",
  stopped: "bg-border-weak-base",
  not_found: "bg-border-weak-base",
  starting: "bg-icon-warning-base",
}

// ── Deterministic gradient from project id (shared with session-new-design-view) ───

const SHOWCASE_GRADIENTS = [
  ["#6366f1", "#8b5cf6"],
  ["#f59e0b", "#ef4444"],
  ["#10b981", "#06b6d4"],
  ["#ec4899", "#8b5cf6"],
  ["#3b82f6", "#6366f1"],
  ["#f97316", "#f59e0b"],
  ["#14b8a6", "#3b82f6"],
  ["#a855f7", "#ec4899"],
] as const

function projectGradient(id: string): readonly [string, string] {
  let hash = 0
  for (let i = 0; i < id.length; i++) {
    hash = (hash << 5) - hash + id.charCodeAt(i)
    hash |= 0
  }
  return SHOWCASE_GRADIENTS[Math.abs(hash) % SHOWCASE_GRADIENTS.length]
}

// ── Spinning F loader ─────────────────────────────────────────────────────────

function SpinningF() {
  return (
    <div class="flex flex-col items-center gap-4">
      <div style={{ animation: "forge-spin 1.1s linear infinite" }}>
        <img
          src="/forge-f-light.png"
          alt="Forge"
          width="52"
          height="52"
          style={{ display: "block", "border-radius": "13px" }}
        />
      </div>
      <span class="text-sm text-v2-text-text-muted [font-weight:440]">Starting preview…</span>
      <style>{`
        @keyframes forge-spin {
          from { transform: rotate(0deg); }
          to   { transform: rotate(360deg); }
        }
      `}</style>
    </div>
  )
}

// ── Showcase modal (Lovable-style) ────────────────────────────────────────────

function ShowcaseModal(props: {
  project:  ShowcaseProject
  onClose:  () => void
}) {
  const forge    = useForgeApi()
  const navigate = useNavigate()
  const [starting,  setStarting]  = createSignal(props.project.container_status !== "running")
  const [cloning,   setCloning]   = createSignal(false)
  const [iframeReady, setIframeReady] = createSignal(props.project.container_status === "running")

  const displayName = () => props.project.showcase_name || props.project.name
  const displayDesc = () => props.project.showcase_description || props.project.description

  // preview_url is an absolute http(s) URL from the API
  // (http://{project_id}.{PREVIEW_DOMAIN}/ — Traefik routes by host).
  const previewUrl = () => props.project.preview_url ?? ""

  // Auto-wake container when modal opens
  createEffect(() => {
    if (props.project.container_status !== "running") {
      setStarting(true)
      setIframeReady(false)
      forge.ensure(props.project.id)
        .then(() => { setStarting(false); setIframeReady(true) })
        .catch(() => { setStarting(false); setIframeReady(true) })
    }
  })

  const handleUseTemplate = async () => {
    if (cloning()) return
    setCloning(true)
    try {
      const newProject = await forge.cloneProject(props.project.id)
      recordRecentView(newProject.id)
      props.onClose()
      navigate(`/${base64Encode(newProject.workspace_path)}/session?from=home`)
    } catch (e) {
      console.error("Clone failed", e)
      setCloning(false)
    }
  }

  return (
    <div
      class="fixed inset-0 z-50 flex items-center justify-center"
      style={{ background: "rgba(0,0,0,0.55)", "backdrop-filter": "blur(6px)" }}
      onClick={props.onClose}
    >
      <div
        class="relative flex flex-col overflow-hidden shadow-2xl"
        style={{
          width: "min(92vw, 1160px)",
          height: "min(90vh, 820px)",
          "border-radius": "14px",
          background: "#ffffff",
          border: "1px solid rgba(0,0,0,0.08)",
        }}
        onClick={(e) => e.stopPropagation()}
      >
        {/* ── Lovable-style header ─────────────────────────────────────────── */}
        <div
          class="flex items-center justify-between shrink-0 px-5"
          style={{
            height: "56px",
            background: "#fff",
            "border-bottom": "1px solid rgba(0,0,0,0.08)",
          }}
        >
          {/* Left: name + "by Forge" */}
          <div class="flex items-center gap-2 min-w-0">
            <span class="text-[15px] font-semibold text-gray-900 truncate">
              {displayName()}
            </span>
            <span class="text-[15px] text-gray-400 [font-weight:400] shrink-0">by Forge</span>
            <Show when={props.project.stack}>
              <span
                class="shrink-0 rounded-[5px] px-1.5 py-0.5 text-[11px] text-gray-500 [font-weight:500]"
                style={{ background: "rgba(0,0,0,0.06)" }}
              >
                {props.project.stack}
              </span>
            </Show>
          </div>

          {/* Right: Use template + close */}
          <div class="flex items-center gap-2 shrink-0">
            <button
              class="flex items-center gap-1.5 text-[13px] font-semibold text-white px-4 py-2 rounded-[8px] transition-opacity disabled:opacity-60"
              style={{ background: "#111827" }}
              onClick={() => void handleUseTemplate()}
              disabled={cloning()}
            >
              <Show when={cloning()}
                fallback={
                  <>
                    <svg class="size-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
                      <path d="M12 5v14M5 12h14"/>
                    </svg>
                    Use template
                  </>
                }
              >
                <span class="forge-css-spinner" style={{ width: "14px", height: "14px", "border-width": "2px" }} />
                Creating…
              </Show>
            </button>
            <button
              class="flex items-center justify-center w-8 h-8 rounded-[7px] transition-colors hover:bg-gray-100"
              onClick={props.onClose}
              aria-label="Close"
            >
              <svg class="size-4 text-gray-500" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
                <path d="M18 6L6 18M6 6l12 12"/>
              </svg>
            </button>
          </div>
        </div>

        {/* ── Preview area ─────────────────────────────────────────────────── */}
        <div class="flex-1 min-h-0 relative bg-gray-50">
          <Show
            when={iframeReady()}
            fallback={
              <div class="absolute inset-0 flex items-center justify-center bg-gray-50">
                <SpinningF />
              </div>
            }
          >
            <iframe
              src={previewUrl()}
              class="w-full h-full border-0 bg-white"
              title={displayName()}
              sandbox="allow-scripts allow-same-origin allow-forms allow-modals allow-popups"
            />
          </Show>
        </div>

        {/* ── Footer info strip ────────────────────────────────────────────── */}
        <Show when={displayDesc()}>
          <div
            class="shrink-0 px-5 py-2.5 flex items-center gap-2"
            style={{ "border-top": "1px solid rgba(0,0,0,0.07)", background: "#fff" }}
          >
            <span class="text-[12px] text-gray-500 [font-weight:440] truncate">{displayDesc()}</span>
          </div>
        </Show>
      </div>
    </div>
  )
}

// ── Showcase / template grid (Lovable-style) ──────────────────────────────────

function ShowcaseGrid(props: {
  projects: ShowcaseProject[]
  onOpen:   (p: ShowcaseProject) => void
}) {
  const forge = useForgeApi()

  return (
    <>
      <style>{`
        .sg-grid {
          display: grid;
          grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
          gap: 20px;
        }
        .sg-card {
          display: flex; flex-direction: column;
          background: #fff;
          border: 1px solid rgba(0,0,0,0.08);
          border-radius: 14px;
          overflow: hidden;
          cursor: pointer;
          transition: transform 180ms ease, box-shadow 180ms ease;
          text-align: left;
        }
        .sg-card:hover {
          transform: translateY(-3px);
          box-shadow: 0 12px 32px -8px rgba(0,0,0,0.16);
        }
        .sg-thumb {
          position: relative; width: 100%;
          height: 200px; flex: none; overflow: hidden;
        }
        .sg-thumb img {
          position: absolute; inset: 0;
          width: 100%; height: 100%;
          object-fit: cover; object-position: top;
          display: block;
        }
        .sg-gradient {
          position: absolute; inset: 0;
        }
        .sg-dots {
          position: absolute; inset: 0; opacity: 0.08;
          background-image: radial-gradient(circle, white 1px, transparent 1px);
          background-size: 22px 22px;
        }
        .sg-name-on-grad {
          position: absolute; inset: 0;
          display: flex; align-items: flex-end; padding: 14px;
          font-size: 14px; font-weight: 700;
          color: #fff; line-height: 1.3;
          text-shadow: 0 1px 4px rgba(0,0,0,0.35);
        }
        /* Preview pill — explicit colours so nothing inherits */
        .sg-overlay {
          position: absolute; inset: 0;
          display: flex; align-items: center; justify-content: center;
          background: rgba(0,0,0,0.3);
          opacity: 0; transition: opacity 150ms ease;
        }
        .sg-card:hover .sg-overlay { opacity: 1; }
        .sg-pill {
          display: inline-flex; align-items: center; gap: 6px;
          font-size: 12px; font-weight: 700;
          color: #ffffff !important;
          background: rgba(0,0,0,0.65);
          backdrop-filter: blur(4px);
          padding: 7px 14px; border-radius: 999px;
          pointer-events: none;
        }
        .sg-pill svg { color: #ffffff !important; fill: #ffffff; }
        .sg-body { padding: 13px 16px 15px; display: flex; flex-direction: column; gap: 3px; }
        .sg-title {
          font-size: 14px; font-weight: 650;
          color: #111; overflow: hidden;
          text-overflow: ellipsis; white-space: nowrap;
        }
        .sg-desc {
          font-size: 12.5px; color: #6b7280;
          line-height: 1.5;
          overflow: hidden;
          display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
        }
        .sg-tag {
          display: inline-flex; align-items: center;
          font-size: 10px; font-weight: 600;
          color: #6b7280;
          background: #f3f4f6;
          padding: 2px 7px; border-radius: 4px;
          margin-top: 4px; width: fit-content;
        }
        /* Dark mode */
        [data-color-scheme="dark"] .sg-card {
          background: #1d1b15; border-color: #2c2920;
        }
        [data-color-scheme="dark"] .sg-card:hover {
          box-shadow: 0 12px 32px -8px rgba(0,0,0,0.5);
        }
        [data-color-scheme="dark"] .sg-title { color: #f5efe0; }
        [data-color-scheme="dark"] .sg-desc  { color: #a39c8a; }
        [data-color-scheme="dark"] .sg-tag   { background: #2a2720; color: #a39c8a; }
      `}</style>
      <div class="sg-grid">
        <For each={props.projects}>
          {(project) => {
            const colors      = projectGradient(project.id)
            const thumbSrc    = project.thumbnail_url
              ? `${forge.baseUrl}${project.thumbnail_url}`
              : null
            const dName = () => project.showcase_name || project.name
            const dDesc = () => project.showcase_description || project.description

            return (
              <button type="button" class="sg-card" onClick={() => props.onOpen(project)}>
                {/* Thumbnail */}
                <div class="sg-thumb">
                  <Show
                    when={thumbSrc}
                    fallback={
                      <div
                        class="sg-gradient"
                        style={{ background: `linear-gradient(140deg, ${colors[0]} 0%, ${colors[1]} 100%)` }}
                      >
                        <div class="sg-dots" />
                        <div class="sg-name-on-grad">{dName()}</div>
                      </div>
                    }
                  >
                    {(src) => <img src={src()} alt={dName()} loading="lazy" />}
                  </Show>
                  {/* Hover overlay */}
                  <div class="sg-overlay">
                    <span class="sg-pill">
                      <svg width="12" height="12" viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>
                      Preview
                    </span>
                  </div>
                </div>

                {/* Card text */}
                <div class="sg-body">
                  <div class="sg-title">{dName()}</div>
                  <Show when={dDesc()}>
                    <div class="sg-desc">{dDesc()}</div>
                  </Show>
                  <Show when={project.stack}>
                    <span class="sg-tag">{project.stack}</span>
                  </Show>
                </div>
              </button>
            )
          }}
        </For>
      </div>
    </>
  )
}

// ── Gradient palette for project cards without a thumbnail ───────────────────
const CARD_GRADIENTS = [
  ["oklch(0.62 0.155 45)", "oklch(0.55 0.18 30)"],   // terracotta → rust
  ["oklch(0.58 0.16 260)", "oklch(0.50 0.18 290)"],  // indigo → violet
  ["oklch(0.60 0.14 148)", "oklch(0.52 0.16 180)"],  // teal → cyan
  ["oklch(0.65 0.17 80)",  "oklch(0.58 0.19 50)"],   // amber → orange
  ["oklch(0.55 0.18 320)", "oklch(0.48 0.16 280)"],  // pink → purple
  ["oklch(0.60 0.15 220)", "oklch(0.52 0.17 250)"],  // sky → blue
] as const

function cardGradient(id: string): readonly [string, string] {
  let h = 0
  for (let i = 0; i < id.length; i++) { h = (h << 5) - h + id.charCodeAt(i); h |= 0 }
  return CARD_GRADIENTS[Math.abs(h) % CARD_GRADIENTS.length]
}

// ── New ForgeHome — sidebar + chat hero + project tabs ──────────────────────

const FORGE_HOME_STYLES = `
  .fh-root {
    height: 100%;
    display: flex;
    background: var(--bg, #f7f3ea);
    color: var(--ink, #15140f);
    font-family: 'Geist', system-ui, sans-serif;
    -webkit-font-smoothing: antialiased;
    overflow: hidden;
  }
  .fh-root * { box-sizing: border-box; }

  /* ── Sidebar ─────────────────────────────────────────────────────────── */
  .fh-side {
    width: 256px; flex: none;
    background: var(--panel, #f1ece1);
    border-right: 1px solid var(--line, #e6dfd0);
    display: flex; flex-direction: column;
    padding: 14px 12px 12px;
    overflow-y: auto;
    overscroll-behavior: contain;
  }
  .fh-side-top {
    display: flex; align-items: center; gap: 10px;
    padding: 4px 8px 16px;
  }
  .fh-side-top .mark {
    width: 28px; height: 28px;
    border-radius: 7px;
    background-image: url('/forge-f-light.png');
    background-size: cover;
    background-position: center;
  }
  .fh-side-top .name {
    font-weight: 700; font-size: 17px; letter-spacing: -0.02em;
    color: var(--ink, #15140f);
  }

  .fh-side-section {
    font-size: 11px; letter-spacing: 0.04em; text-transform: uppercase;
    color: var(--muted, #8a8175);
    padding: 14px 10px 6px;
    font-weight: 600;
  }
  .fh-side-item {
    display: flex; align-items: center; gap: 10px;
    padding: 8px 10px;
    border: 0; background: transparent;
    border-radius: 8px;
    font-family: inherit; font-size: 14px;
    color: var(--ink-2, #2b2a25); font-weight: 500;
    cursor: pointer; text-align: left; width: 100%;
    transition: background .12s ease, color .12s ease;
  }
  .fh-side-item:hover { background: rgba(21,20,15,0.05); color: var(--ink, #15140f); }
  .fh-side-item.active {
    background: rgba(21,20,15,0.08);
    color: var(--ink, #15140f); font-weight: 600;
  }
  .fh-side-item svg { width: 16px; height: 16px; flex: none; color: currentColor; }

  .fh-recents-list {
    display: flex; flex-direction: column; gap: 2px;
    margin-top: 4px;
  }
  .fh-recent {
    display: block; width: 100%;
    padding: 6px 10px;
    border: 0; background: transparent;
    font-family: inherit; font-size: 13px;
    color: var(--muted-2, #6b6358);
    cursor: pointer; text-align: left;
    border-radius: 6px;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .fh-recent:hover { background: rgba(21,20,15,0.05); color: var(--ink, #15140f); }
  .fh-recents-empty {
    padding: 6px 10px;
    font-size: 12.5px; color: var(--muted, #8a8175);
    font-style: italic;
  }

  .fh-side-foot {
    margin-top: auto;
    padding-top: 12px;
    border-top: 1px solid var(--line, #e6dfd0);
  }
  .fh-profile {
    display: flex; align-items: center; gap: 10px;
    padding: 8px 10px;
    border-radius: 8px;
    cursor: pointer;
    transition: background .12s ease;
  }
  .fh-profile:hover { background: rgba(21,20,15,0.05); }
  .fh-profile .avatar {
    width: 32px; height: 32px; border-radius: 999px;
    background: linear-gradient(135deg, oklch(0.78 0.15 55), oklch(0.58 0.21 18));
    color: #fff; font-weight: 700; font-size: 13px;
    display: grid; place-items: center;
    flex: none;
  }
  .fh-profile .who {
    font-size: 13px; color: var(--ink, #15140f);
    font-weight: 600; line-height: 1.2;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    min-width: 0; flex: 1;
  }
  .fh-profile .who .sub {
    display: block; font-weight: 400; color: var(--muted, #8a8175);
    font-size: 11.5px;
  }
  .fh-profile-settings {
    flex: none;
    width: 32px; height: 32px; border-radius: 8px;
    background: transparent; border: 0; cursor: pointer;
    display: grid; place-items: center;
    color: var(--muted-2, #6b6358);
    transition: background .12s ease, color .12s ease;
  }
  .fh-profile-settings:hover { background: rgba(21,20,15,0.08); color: var(--ink, #15140f); }
  .fh-profile-settings svg { width: 16px; height: 16px; }

  /* ── Main area ───────────────────────────────────────────────────────── */
  .fh-main {
    flex: 1; min-width: 0;
    display: flex; flex-direction: column;
    overflow-y: auto;
    overscroll-behavior: contain;
    position: relative;
    background: linear-gradient(180deg,
      var(--bg, #f7f3ea) 0%,
      oklch(0.95 0.04 60) 40%,
      oklch(0.97 0.02 70) 75%,
      #ffffff 100%);
  }
  /* Warm wash sits behind the topbar + hero, fades from cream at top into
     ember at the bottom — and ends exactly where .fh-projects-wrap begins. */
  .fh-hero-area {
    position: relative;
    display: flex; flex-direction: column;
    min-height: 70vh;
    background: transparent;
  }

  .fh-topbar {
    display: flex; align-items: center; justify-content: space-between;
    padding: 14px 24px;
    gap: 12px;
  }
  .fh-hamburger {
    display: none;
    width: 38px; height: 38px;
    border-radius: 10px;
    background: transparent; border: 0;
    cursor: pointer;
    place-items: center;
    color: var(--ink, #15140f);
  }
  .fh-hamburger:hover { background: rgba(21,20,15,0.06); }
  .fh-hamburger svg { width: 20px; height: 20px; }

  .fh-mobile-title {
    display: none;
    font-weight: 700; font-size: 16px; letter-spacing: -0.02em;
    align-items: center; gap: 8px;
  }
  .fh-mobile-title .mark {
    width: 22px; height: 22px; border-radius: 5px;
    background-image: url('/forge-f-light.png');
    background-size: cover; background-position: center;
  }

  .fh-hero {
    flex: 1;
    padding: 32px 24px 40px;
    display: flex; flex-direction: column;
    align-items: center;
    justify-content: center;
    max-width: 880px;
    width: 100%;
    margin: 0 auto;
  }
  .fh-banner {
    display: inline-flex; align-items: center; gap: 10px;
    padding: 6px 14px 6px 6px;
    background: #fff;
    border: 1px solid var(--line, #e6dfd0);
    border-radius: 999px;
    font-size: 13px;
    color: var(--ink-2, #2b2a25);
    box-shadow: 0 1px 2px rgba(40,30,15,.04), 0 6px 18px -8px rgba(40,30,15,.12);
    margin-top: 16px;
    cursor: pointer;
    transition: transform .15s ease;
  }
  .fh-banner:hover { transform: translateY(-1px); }
  .fh-banner-glyphs {
    display: inline-flex; gap: 4px;
  }
  .fh-banner-glyphs span {
    width: 20px; height: 20px; border-radius: 5px;
    display: grid; place-items: center;
    color: #fff;
    font-size: 11px;
  }
  .fh-banner-arrow {
    width: 16px; height: 16px;
    color: var(--muted-2, #6b6358);
  }

  .fh-hero h1 {
    font-size: clamp(28px, 4vw, 44px);
    font-weight: 700;
    letter-spacing: -0.03em;
    margin: 28px 0 28px;
    text-align: center;
    color: var(--ink, #15140f);
  }

  .fh-prompt {
    width: 100%;
    background: #fff;
    border: 1px solid var(--line, #e6dfd0);
    border-radius: 18px;
    padding: 16px 18px 12px;
    box-shadow: 0 1px 2px rgba(40,30,15,.04), 0 14px 30px -14px rgba(40,30,15,.12);
  }
  .fh-prompt textarea {
    width: 100%;
    border: 0; outline: 0; resize: none;
    background: transparent;
    font: inherit;
    font-size: 16px;
    color: var(--ink, #15140f);
    line-height: 1.45;
    padding: 4px 0;
    min-height: 56px;
    font-family: inherit;
  }
  .fh-prompt textarea::placeholder { color: var(--muted, #8a8175); }
  .fh-prompt-row {
    display: flex; align-items: center; gap: 10px;
    padding-top: 10px; margin-top: 6px;
    border-top: 1px solid var(--line-2, #f0ead9);
  }
  .fh-prompt-chip {
    display: inline-flex; align-items: center; gap: 6px;
    height: 28px; padding: 0 10px;
    border-radius: 999px;
    background: var(--surface-3, #faf6ec);
    border: 1px solid var(--line-3, #efe7d4);
    font-size: 12.5px; color: var(--muted-2, #6b6358);
    font-family: inherit;
    cursor: default;
    white-space: nowrap;
  }
  .fh-prompt-chip.interactive {
    cursor: pointer;
    transition: background-color .12s ease, border-color .12s ease;
  }
  .fh-prompt-chip.interactive:hover {
    background: var(--line-3, #efe7d4);
    color: var(--ink, #15140f);
  }
  [data-color-scheme="dark"] .fh-prompt-chip.interactive:hover {
    background: #2a2720;
    color: #f5efe0;
  }
  .fh-prompt-chip.icon { width: 28px; padding: 0; justify-content: center; }
  .fh-prompt-send {
    margin-left: auto;
    width: 36px; height: 36px;
    border-radius: 999px;
    background: var(--ink, #15140f); color: #fff;
    border: 0;
    display: grid; place-items: center;
    cursor: pointer;
    transition: opacity .15s, transform .12s;
  }
  .fh-prompt-send:disabled { opacity: .35; cursor: not-allowed; }
  .fh-prompt-send:not(:disabled):hover { transform: translateY(-1px); }
  .fh-prompt-send svg { width: 16px; height: 16px; }
  .fh-prompt-hint {
    font-size: 12px; color: var(--muted, #8a8175);
    margin-top: 12px;
    text-align: center;
  }

  /* ── Project tabs section — sits on a clean surface that visually
     ends the warm hero wash above. Fills the bottom half of the screen. */
  .fh-projects-wrap {
    background: #ffffff;
    border-radius: 24px;
    border: 1px solid var(--line, #e6dfd0);
    box-shadow: 
      0 -1px 0 0 rgba(251, 146, 60, 0.12),
      0 -6px 20px -2px rgba(251, 146, 60, 0.06),
      0 -12px 32px -4px rgba(21, 20, 15, 0.03), 
      0 -4px 12px -2px rgba(21, 20, 15, 0.02);
    padding: 32px 24px 48px;
    flex: none;
    display: flex;
    flex-direction: column;
    min-height: 50vh;
    margin: -32px 24px 24px 24px;
    position: relative;
    z-index: 10;
  }
  .fh-projects {
    width: 100%;
    max-width: 1280px;
    margin: 0 auto;
    flex: 1;
    display: flex;
    flex-direction: column;
  }
  .fh-projects-body {
    flex: 1;
    display: flex;
    flex-direction: column;
  }
  .fh-tabs-row {
    display: flex; align-items: center;
    justify-content: space-between;
    gap: 12px;
    margin-bottom: 20px;
  }
  .fh-browse-all {
    background: none; border: 0; cursor: pointer;
    font-family: inherit;
    font-size: 13px; color: var(--ink-2, #2b2a25);
    display: inline-flex; align-items: center; gap: 6px;
    padding: 6px 8px;
    border-radius: 8px;
    transition: background .12s ease;
  }
  .fh-browse-all:hover { background: rgba(21,20,15,0.06); }
  .fh-browse-all svg { width: 14px; height: 14px; }

  .fh-tabs {
    display: flex; gap: 4px;
    background: var(--surface-3, #f5f2eb);
    padding: 4px;
    border-radius: 999px;
    width: fit-content;
    border: 1px solid var(--line-3, #efe7d4);
  }
  .fh-tab {
    background: transparent; border: 0;
    padding: 7px 16px;
    border-radius: 999px;
    font-family: inherit;
    font-size: 13.5px; font-weight: 500;
    color: var(--muted-2, #6b6358);
    cursor: pointer;
    transition: background .15s, color .15s;
  }
  .fh-tab:hover { color: var(--ink, #15140f); }
  .fh-tab.active {
    background: #fff;
    border: 1px solid var(--line, #e6dfd0);
    color: var(--ink, #15140f); font-weight: 600;
    box-shadow: 0 1px 3px rgba(40,30,15,.08);
  }

  .fh-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
    gap: 16px;
  }
  .fh-card {
    position: relative;
    background: #fff;
    border: 1px solid var(--line, #e6dfd0);
    border-radius: 12px;
    overflow: hidden;
    cursor: pointer;
    transition: transform .15s ease, box-shadow .15s ease;
  }
  .fh-card:hover {
    transform: translateY(-2px);
    box-shadow: 0 10px 26px -10px rgba(40,30,15,.18);
  }
  /* Star + kebab overlay — sit on the thumbnail, only show on hover */
  .fh-card-actions {
    position: absolute;
    top: 8px; right: 8px;
    display: flex; gap: 4px;
    opacity: 0;
    transition: opacity .15s ease;
    z-index: 2;
  }
  .fh-card:hover .fh-card-actions,
  .fh-card-actions.always-on { opacity: 1; }
  .fh-icon-btn {
    width: 28px; height: 28px;
    border-radius: 7px;
    background: rgba(255,255,255,.92);
    backdrop-filter: blur(6px);
    border: 1px solid var(--line, #e6dfd0);
    display: grid; place-items: center;
    cursor: pointer;
    color: var(--ink-2, #2b2a25);
    transition: background .12s ease, color .12s ease;
  }
  .fh-icon-btn:hover { background: #fff; color: var(--ink, #15140f); }
  .fh-icon-btn svg { width: 14px; height: 14px; }
  .fh-icon-btn.starred {
    color: oklch(0.72 0.18 70);
  }
  .fh-icon-btn.starred svg { fill: currentColor; }
  [data-color-scheme="dark"] .fh-icon-btn {
    background: rgba(29,27,21,.88);
    border-color: #2c2920;
    color: #d8d0bd;
  }
  [data-color-scheme="dark"] .fh-icon-btn:hover { background: #1d1b15; color: #f5efe0; }

  /* Card kebab menu — anchored to card, simple absolute panel */
  .fh-menu {
    position: absolute;
    top: 42px; right: 8px;
    background: #fff;
    border: 1px solid var(--line, #e6dfd0);
    border-radius: 10px;
    box-shadow: 0 12px 28px -8px rgba(40,30,15,.22);
    padding: 4px;
    min-width: 160px;
    z-index: 3;
    display: flex; flex-direction: column;
  }
  .fh-menu button {
    display: flex; align-items: center; gap: 8px;
    border: 0; background: transparent;
    text-align: left; padding: 7px 10px;
    font-family: inherit; font-size: 13px;
    color: var(--ink-2, #2b2a25);
    border-radius: 6px;
    cursor: pointer;
  }
  .fh-menu button:hover { background: var(--surface-3, #faf6ec); }
  .fh-menu button.danger { color: oklch(0.55 0.21 25); }
  .fh-menu button.danger:hover { background: oklch(0.96 0.04 25); }
  .fh-menu button svg { width: 14px; height: 14px; flex: none; }
  [data-color-scheme="dark"] .fh-menu {
    background: #1d1b15;
    border-color: #2c2920;
  }
  [data-color-scheme="dark"] .fh-menu button { color: #d8d0bd; }
  [data-color-scheme="dark"] .fh-menu button:hover { background: #2a2720; }
  [data-color-scheme="dark"] .fh-menu button.danger { color: oklch(0.72 0.18 25); }
  [data-color-scheme="dark"] .fh-menu button.danger:hover { background: oklch(0.30 0.12 25); }

  /* View header shown on All / Starred / By me */
  .fh-view-header {
    max-width: 1280px;
    margin: 0 auto;
    padding: 28px 24px 16px;
    width: 100%;
  }
  .fh-view-title {
    font-size: 22px;
    font-weight: 700;
    letter-spacing: -0.02em;
    color: var(--ink, #15140f);
    margin: 0 0 4px;
  }
  .fh-view-sub {
    font-size: 13.5px;
    color: var(--muted-2, #6b6358);
    margin: 0;
  }
  [data-color-scheme="dark"] .fh-view-title { color: #f5efe0; }
  [data-color-scheme="dark"] .fh-view-sub { color: #a39c8a; }
  .fh-view-grid {
    max-width: 1280px;
    margin: 0 auto;
    padding: 0 24px 48px;
    width: 100%;
  }

  /* Confirm dialog — minimal, no extra deps */
  .fh-confirm-backdrop {
    position: fixed; inset: 0;
    background: rgba(0,0,0,.5);
    z-index: 60;
    display: grid; place-items: center;
    backdrop-filter: blur(4px);
  }
  .fh-confirm {
    background: #fff;
    border-radius: 14px;
    width: min(420px, 92vw);
    padding: 20px;
    box-shadow: 0 24px 60px -16px rgba(0,0,0,.4);
    border: 1px solid var(--line, #e6dfd0);
  }
  .fh-confirm h3 {
    margin: 0 0 8px;
    font-size: 17px; font-weight: 700;
    letter-spacing: -0.01em;
    color: var(--ink, #15140f);
  }
  .fh-confirm p {
    margin: 0 0 18px;
    font-size: 13.5px; line-height: 1.5;
    color: var(--muted-2, #6b6358);
  }
  .fh-confirm-row {
    display: flex; justify-content: flex-end; gap: 8px;
  }
  .fh-confirm-row button {
    font-family: inherit;
    font-size: 13px; font-weight: 600;
    padding: 8px 14px;
    border-radius: 8px;
    border: 1px solid var(--line, #e6dfd0);
    background: #fff;
    color: var(--ink, #15140f);
    cursor: pointer;
  }
  .fh-confirm-row button.danger {
    background: oklch(0.55 0.21 25);
    border-color: oklch(0.55 0.21 25);
    color: #fff;
  }
  .fh-confirm-row button.danger:hover { background: oklch(0.48 0.23 25); }
  [data-color-scheme="dark"] .fh-confirm {
    background: #1d1b15;
    border-color: #2c2920;
  }
  [data-color-scheme="dark"] .fh-confirm h3 { color: #f5efe0; }
  [data-color-scheme="dark"] .fh-confirm p { color: #a39c8a; }
  [data-color-scheme="dark"] .fh-confirm-row button {
    background: #1d1b15;
    border-color: #2c2920;
    color: #f5efe0;
  }
  .fh-thumb {
    aspect-ratio: 16 / 10;
    background: var(--panel, #f1ece1);
    position: relative;
    overflow: hidden;
  }
  .fh-card-foot { padding: 10px 12px 12px; }
  .fh-card-name {
    font-size: 13.5px; font-weight: 600; color: var(--ink, #15140f);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .fh-card-meta {
    font-size: 11.5px; color: var(--muted, #8a8175);
    margin-top: 2px;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }

  .fh-empty {
    flex: 1;
    min-height: 320px;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    border: 1.5px dashed var(--line-strong, #d4cab2);
    background: var(--surface-2, #fbf8f1);
    border-radius: 16px;
    padding: 56px 24px;
    text-align: center;
    color: var(--muted-2, #6b6358);
  }
  .fh-empty .icon {
    width: 56px; height: 56px;
    margin: 0 auto 14px;
    border-radius: 14px;
    background-image: url('/forge-f-light.png');
    background-size: cover;
    background-position: center;
    opacity: 0.35;
  }
  .fh-empty .t {
    font-weight: 600; color: var(--ink, #15140f);
    font-size: 15px;
    margin-bottom: 4px;
  }
  .fh-empty .s { font-size: 13.5px; line-height: 1.55; max-width: 36ch; margin: 0 auto; }

  /* ── Theme toggle (top-right) ────────────────────────────────────────── */
  .fh-theme-btn {
    width: 38px; height: 38px;
    border-radius: 10px;
    background: transparent; border: 0;
    cursor: pointer;
    display: grid; place-items: center;
    color: var(--ink, #15140f);
  }
  .fh-theme-btn:hover { background: rgba(21,20,15,0.06); }
  .fh-theme-btn svg { width: 18px; height: 18px; }

  /* ── Mobile (≤ 900px) — hamburger drawer ─────────────────────────────── */
  @media (max-width: 900px) {
    .fh-side {
      position: fixed; inset: 0 auto 0 0;
      z-index: 50;
      transform: translateX(-100%);
      transition: transform .25s ease;
      box-shadow: 0 12px 30px -8px rgba(40,30,15,.18);
    }
    .fh-side.open { transform: translateX(0); }
    .fh-hamburger { display: grid; }
    .fh-mobile-title { display: inline-flex; }
    .fh-backdrop {
      position: fixed; inset: 0;
      background: rgba(0,0,0,.32);
      z-index: 40;
      opacity: 0; pointer-events: none;
      transition: opacity .2s ease;
    }
    .fh-backdrop.on { opacity: 1; pointer-events: auto; }
    .fh-hero { padding: 12px 18px 28px; }
    .fh-hero h1 { margin: 24px 0 22px; }
    .fh-projects { padding: 0 18px; margin: 24px auto 36px; }
    .fh-topbar { padding: 10px 16px; }
  }
  @media (max-width: 480px) {
    .fh-grid { grid-template-columns: 1fr; }
    .fh-tabs { width: 100%; justify-content: stretch; }
    .fh-tab { flex: 1; padding: 7px 8px; font-size: 12.5px; }
  }

  /* ── Dark mode ───────────────────────────────────────────────────────
     The app drives :root[data-color-scheme="dark"]. Override surfaces
     that were hard-coded to cream/white in light mode. */
  [data-color-scheme="dark"] .fh-root {
    background: #0f0e0a;
    color: #f5efe0;
  }
  [data-color-scheme="dark"] .fh-side {
    background: #1a1813;
    border-right-color: #2c2920;
  }
  [data-color-scheme="dark"] .fh-side-top .name { color: #f5efe0; }
  [data-color-scheme="dark"] .fh-side-top .mark { background-image: url('/forge-f-dark.png'); }
  [data-color-scheme="dark"] .fh-side-section { color: #807968; }
  [data-color-scheme="dark"] .fh-side-item { color: #d8d0bd; }
  [data-color-scheme="dark"] .fh-side-item:hover { background: rgba(255,255,255,0.05); color: #f5efe0; }
  [data-color-scheme="dark"] .fh-side-item.active { background: rgba(255,255,255,0.10); color: #f5efe0; }
  [data-color-scheme="dark"] .fh-recent { color: #a39c8a; }
  [data-color-scheme="dark"] .fh-recent:hover { background: rgba(255,255,255,0.06); color: #f5efe0; }
  [data-color-scheme="dark"] .fh-recents-empty { color: #807968; }
  [data-color-scheme="dark"] .fh-side-foot { border-top-color: #2c2920; }
  [data-color-scheme="dark"] .fh-profile:hover { background: rgba(255,255,255,0.05); }
  [data-color-scheme="dark"] .fh-profile .who { color: #f5efe0; }
  [data-color-scheme="dark"] .fh-profile .who .sub { color: #807968; }
  [data-color-scheme="dark"] .fh-profile-settings { color: #a39c8a; }
  [data-color-scheme="dark"] .fh-profile-settings:hover { background: rgba(255,255,255,0.08); color: #f5efe0; }

  [data-color-scheme="dark"] .fh-hamburger { color: #f5efe0; }
  [data-color-scheme="dark"] .fh-hamburger:hover { background: rgba(255,255,255,0.06); }
  [data-color-scheme="dark"] .fh-theme-btn { color: #f5efe0; }
  [data-color-scheme="dark"] .fh-theme-btn:hover { background: rgba(255,255,255,0.06); }
  [data-color-scheme="dark"] .fh-mobile-title { color: #f5efe0; }
  [data-color-scheme="dark"] .fh-mobile-title .mark { background-image: url('/forge-f-dark.png'); }

  [data-color-scheme="dark"] .fh-hero h1 { color: #f5efe0; }
  [data-color-scheme="dark"] .fh-banner {
    background: #1d1b15;
    border-color: #2c2920;
    color: #d8d0bd;
  }
  [data-color-scheme="dark"] .fh-banner-arrow { color: #a39c8a; }
  [data-color-scheme="dark"] .fh-prompt {
    background: #1d1b15;
    border-color: #2c2920;
    box-shadow: 0 1px 2px rgba(0,0,0,.4), 0 14px 30px -14px rgba(0,0,0,.6);
  }
  [data-color-scheme="dark"] .fh-prompt textarea { color: #f5efe0; }
  [data-color-scheme="dark"] .fh-prompt textarea::placeholder { color: #807968; }
  [data-color-scheme="dark"] .fh-prompt-row { border-top-color: #221f18; }
  [data-color-scheme="dark"] .fh-prompt-chip {
    background: #221f18;
    border-color: #2a2720;
    color: #a39c8a;
  }
  [data-color-scheme="dark"] .fh-prompt-send { background: #f5efe0; color: #15140f; }
  [data-color-scheme="dark"] .fh-prompt-hint { color: #807968; }

  [data-color-scheme="dark"] .fh-main {
    background: linear-gradient(180deg,
      #2c2a24 0%,
      #22211c 40%,
      #1b1a15 75%,
      #15140f 100%);
  }

  [data-color-scheme="dark"] .fh-hero-area {
    background: transparent;
  }

  [data-color-scheme="dark"] .fh-projects-wrap {
    background: #15140f;
    border-top-color: transparent;
    box-shadow: 
      0 -1px 0 0 rgba(255, 255, 255, 0.08),
      0 -6px 20px -2px rgba(255, 255, 255, 0.03),
      0 -12px 32px -4px rgba(0, 0, 0, 0.25), 
      0 -4px 12px -2px rgba(0, 0, 0, 0.15);
  }
  [data-color-scheme="dark"] .fh-tabs { background: #1d1b15; }
  [data-color-scheme="dark"] .fh-tab { color: #a39c8a; }
  [data-color-scheme="dark"] .fh-tab:hover { color: #f5efe0; }
  [data-color-scheme="dark"] .fh-tab.active {
    background: #2c2920; color: #f5efe0;
    box-shadow: 0 1px 3px rgba(0,0,0,.4);
  }
  [data-color-scheme="dark"] .fh-browse-all { color: #d8d0bd; }
  [data-color-scheme="dark"] .fh-browse-all:hover { background: rgba(255,255,255,0.06); }
  [data-color-scheme="dark"] .fh-card {
    background: #1d1b15;
    border-color: #2c2920;
  }
  [data-color-scheme="dark"] .fh-card:hover { box-shadow: 0 10px 26px -10px rgba(0,0,0,.6); }
  [data-color-scheme="dark"] .fh-thumb { background: #221f18; }
  [data-color-scheme="dark"] .fh-card-name { color: #f5efe0; }
  [data-color-scheme="dark"] .fh-card-meta { color: #807968; }
  [data-color-scheme="dark"] .fh-empty {
    background: #1a1813;
    border-color: #2c2920;
    color: #a39c8a;
  }
  [data-color-scheme="dark"] .fh-empty .t { color: #f5efe0; }
`

type FhTab = "my" | "recent" | "templates"

function formatTimeAgo(isoString: string | undefined) {
  if (!isoString) return "Edited recently"
  const dt = DateTime.fromISO(isoString)
  if (!dt.isValid) return "Edited recently"
  const diff = DateTime.now().diff(dt, ["years", "months", "days", "hours", "minutes", "seconds"]).toObject()
  
  if (diff.years && diff.years >= 1) return `Edited ${Math.floor(diff.years)} year${Math.floor(diff.years) > 1 ? "s" : ""} ago`
  if (diff.months && diff.months >= 1) return `Edited ${Math.floor(diff.months)} month${Math.floor(diff.months) > 1 ? "s" : ""} ago`
  if (diff.days && diff.days >= 1) {
    if (diff.days >= 7) {
      const weeks = Math.floor(diff.days / 7)
      return `Edited ${weeks} week${weeks > 1 ? "s" : ""} ago`
    }
    return `Edited ${Math.floor(diff.days)} day${Math.floor(diff.days) > 1 ? "s" : ""} ago`
  }
  if (diff.hours && diff.hours >= 1) return `Edited ${Math.floor(diff.hours)} hour${Math.floor(diff.hours) > 1 ? "s" : ""} ago`
  if (diff.minutes && diff.minutes >= 1) return `Edited ${Math.floor(diff.minutes)} minute${Math.floor(diff.minutes) > 1 ? "s" : ""} ago`
  return "Edited seconds ago"
}

// ── Recently viewed (client-side, localStorage) ───────────────────────────────
// Why client-side: cross-device "recents" isn't worth a per-open DB write at
// 100k+ containers. Storing locally keeps the open-flow latency at zero extra
// network round-trips, and the list is bounded to RECENT_LIMIT entries (~1.4KB
// total). If we ever want cross-device, swap this for a server PATCH and a
// last_viewed_at column — interface here stays the same.
const RECENT_KEY   = "forge_recently_viewed_v1"
const RECENT_LIMIT = 20

type RecentEntry = { id: string; viewed_at: number }

function loadRecentIds(): RecentEntry[] {
  try {
    const raw = localStorage.getItem(RECENT_KEY)
    if (!raw) return []
    const parsed = JSON.parse(raw)
    if (!Array.isArray(parsed)) return []
    // Defensive filter — drop malformed entries from older versions.
    return parsed.filter(
      (e): e is RecentEntry =>
        e && typeof e.id === "string" && typeof e.viewed_at === "number",
    )
  } catch {
    return []
  }
}

function recordRecentView(projectId: string) {
  if (!projectId) return
  const current = loadRecentIds().filter((e) => e.id !== projectId)
  current.unshift({ id: projectId, viewed_at: Date.now() })
  const trimmed = current.slice(0, RECENT_LIMIT)
  try {
    localStorage.setItem(RECENT_KEY, JSON.stringify(trimmed))
    // Notify the home page reactively without a full refetch.
    window.dispatchEvent(new CustomEvent("forge-recent-updated"))
  } catch {
    // Quota exceeded or storage disabled — silently no-op. The next render
    // simply shows whatever is already persisted.
  }
}

function ForgeHome() {
  const navigate  = useNavigate()
  const forge     = useForgeApi()
  const dialog    = useDialog()
  const theme     = useTheme()
  const globalSync = useGlobalSync()

  function openSettings() {
    void import("@/components/dialog-settings").then((x) => {
      dialog.show(() => <x.DialogSettings />)
    })
  }

  const userInfo  = currentUserInfo()

  // Full profile (full_name, theme_pref, etc.) — loaded once on mount.
  // Used for the avatar initials ("FL" / "F") and the dropdown header.
  // userInfo (JWT-decoded) is kept as a sync fallback for the first paint
  // before /me resolves.
  const [me, setMe] = createSignal<CurrentUser | null>(null)
  void fetchCurrentUser().then((m) => { if (m) setMe(m) })

  const greeting = () => {
    const full = me()?.full_name?.trim()
    if (full) return full.split(/\s+/)[0]   // first name
    const u = userInfo?.username
    return u ? u.charAt(0).toUpperCase() + u.slice(1) : "there"
  }
  const initial = () => userInitials(me()?.full_name, userInfo?.username ?? me()?.username)

  // ── Projects ────────────────────────────────────────────────────────────
  const [projects, { refetch: refetchProjects, mutate: mutateProjects }] = createResource<ForgeProject[]>(async () => {
    const res = await fetch(`${FORGE_API_URL}/api/projects`, {
      headers: { Authorization: `Bearer ${localStorage.getItem("forge_jwt") ?? ""}` },
    })
    if (!res.ok) throw new Error(`forge-server error: ${res.status}`)
    return res.json() as Promise<ForgeProject[]>
  })

  // One-time wipe removed — projects are never auto-deleted on page load.

  // ── Card actions: star toggle + delete ────────────────────────────────
  // Kept lean on context: a single openMenuId/confirmDelete pair drives every
  // card. We optimistically splice the list and roll back on error — avoids a
  // full refetch on each click (flat cost, regardless of project count).
  const [openMenuId,    setOpenMenuId]    = createSignal<string | null>(null)
  const [confirmDelete, setConfirmDelete] = createSignal<ForgeProject | null>(null)
  const [deleting,      setDeleting]      = createSignal(false)

  async function handleStar(project: ForgeProject) {
    const wasStarred = !!project.starred_at
    const optimistic = {
      ...project,
      starred_at: wasStarred ? null : new Date().toISOString(),
    }
    mutateProjects((list) => (list ?? []).map((p) => (p.id === project.id ? optimistic : p)))
    try {
      const updated = await forge.toggleProjectStar(project.id, !wasStarred)
      mutateProjects((list) =>
        (list ?? []).map((p) =>
          p.id === project.id ? { ...p, starred_at: updated.starred_at ?? null } : p,
        ),
      )
    } catch (err) {
      console.error("toggleProjectStar failed", err)
      // Roll back
      mutateProjects((list) => (list ?? []).map((p) => (p.id === project.id ? project : p)))
    }
  }

  async function handleDeleteConfirmed() {
    const target = confirmDelete()
    if (!target || deleting()) return
    setDeleting(true)
    const previous = projects() ?? []
    mutateProjects(previous.filter((p) => p.id !== target.id))
    try {
      await forge.deleteProject(target.id)
      setConfirmDelete(null)
    } catch (err) {
      console.error("deleteProject failed", err)
      mutateProjects(previous)
      setConfirmDelete(null)
    } finally {
      setDeleting(false)
    }
  }

  // Close the kebab menu on any outside click — one document listener for the
  // whole page, not per-card.
  createEffect(() => {
    if (!openMenuId()) return
    const onDoc = () => setOpenMenuId(null)
    document.addEventListener("click", onDoc)
    onCleanup(() => document.removeEventListener("click", onDoc))
  })

  // ── Prompt ─────────────────────────────────────────────────────────────
  const [prompt,   setPrompt]   = createSignal("")
  const [busy,     setBusy]     = createSignal(false)

  // ── Model selector ──────────────────────────────────────────────────────
  const models = useModels()
  const providers = useProviders()
  const [selectedModelKey, setSelectedModelKey] = createSignal<ModelKey | undefined>(() => {
    try {
      const saved = localStorage.getItem("forge_home_model_v2")
      if (saved) return JSON.parse(saved) as ModelKey
    } catch {}
    return undefined
  })

  const homeModelState = {
    ready: () => models.ready(),
    current: () => {
      const validModel = (model: ModelKey) => {
        const provider = providers.all().get(model.providerID)
        return !!provider?.models[model.modelID] && providers.connected().some((p) => p.id === model.providerID)
      }

      // Global opencode config is the single source of truth — check it first
      // so that changing the model in any session is reflected here too.
      const configuredModel = () => {
        if (!globalSync.data.config.model) return
        const [providerID, modelID] = globalSync.data.config.model.split("/")
        const model = { providerID, modelID }
        if (validModel(model)) return model
      }

      const cfg = configuredModel()
      if (cfg) {
        return models.find(cfg)
      }

      // Fall back to the locally-cached key (e.g. before the global config
      // has loaded, or when running without network)
      const key = selectedModelKey()
      if (key && validModel(key)) {
        return models.find(key)
      }

      const recentModel = () => {
        for (const item of models.recent.list()) {
          if (validModel(item)) return item
        }
      }

      const defaultModel = () => {
        const defaults = providers.default()
        for (const provider of providers.connected()) {
          const configured = defaults[provider.id]
          if (configured) {
            const model = { providerID: provider.id, modelID: configured }
            if (validModel(model)) return model
          }

          const first = Object.values(provider.models)[0]
          if (!first) continue
          const model = { providerID: provider.id, modelID: first.id }
          if (validModel(model)) return model
        }
      }

      const fallback = recentModel() ?? defaultModel()
      if (fallback) {
        const found = models.find(fallback)
        if (found) return found
      }

      const list = models.list().filter(m => models.visible({ providerID: m.provider.id, modelID: m.id }))
      if (list.length > 0) return list[0]
      return undefined
    },
    recent: () => models.recent.list().map(models.find).filter(Boolean),
    list: () => models.list(),
    cycle(direction: 1 | -1) {
      const items = this.recent()
      const item = this.current()
      if (!item) return
      const idx = items.findIndex((entry) => entry?.provider.id === item.provider.id && entry?.id === item.id)
      if (idx === -1) return
      let next = idx + direction
      if (next < 0) next = items.length - 1
      if (next >= items.length) next = 0
      const entry = items[next]
      if (entry) this.set({ providerID: entry.provider.id, modelID: entry.id })
    },
    set(item: ModelKey | undefined, options?: { recent?: boolean }) {
      if (!item) return
      setSelectedModelKey(item)
      localStorage.setItem("forge_home_model_v2", JSON.stringify(item))
      models.setVisibility(item, true)
      if (options?.recent) {
        models.recent.push(item)
      }
      // Persist to the global opencode config so the session composer stays in sync
      const globalModelStr = `${item.providerID}/${item.modelID}`
      globalSync.updateConfig({ model: globalModelStr }).catch(() => {
        // non-fatal — local state is still updated
      })
    },
    visible: (item: ModelKey) => models.visible(item),
    setVisibility: (item: ModelKey, visible: boolean) => models.setVisibility(item, visible),
    variant: {
      configured: () => undefined,
      selected: () => undefined,
      current: () => undefined,
      list: () => [],
      set: () => {},
      cycle: () => {},
    }
  }

  async function createFromPrompt() {
    const text = prompt().trim()
    if (!text || busy()) return
    setBusy(true)
    try {
      const res = await fetch(`${FORGE_API_URL}/api/projects`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${localStorage.getItem("forge_jwt") ?? ""}`,
        },
        body: JSON.stringify({ name: text, description: "" }),
      })
      if (!res.ok) throw new Error(`Failed: ${res.status}`)
      const project: ForgeProject = await res.json()
      
      let modelParam = ""
      try {
        const selectedModel = homeModelState.current()
        if (selectedModel && selectedModel.provider && selectedModel.provider.id && selectedModel.id) {
          modelParam = `&model=${selectedModel.provider.id}/${selectedModel.id}`
        }
      } catch (modelErr) {
        console.error("Failed to resolve selected model:", modelErr)
      }
      
      recordRecentView(project.id)
      navigate(`/${base64Encode(project.workspace_path)}/session?prompt=${encodeURIComponent(text)}${modelParam}&from=home`)
    } catch (err) {
      console.error(err)
      setBusy(false)
    }
  }

  function openProject(project: ForgeProject) {
    recordRecentView(project.id)
    navigate(`/${base64Encode(project.workspace_path)}/session?from=home`)
  }

  // ── Sidebar nav state — synced to ?view= so refresh preserves position ──
  type SideKey = "home" | "resources" | "all" | "starred" | "by-me"
  const [searchParams, setSearchParams] = useSearchParams()
  const side = (): SideKey => (searchParams.view as SideKey) || "home"
  const setSide = (key: SideKey) => {
    setSearchParams({ view: key === "home" ? undefined : key }, { replace: true })
  }
  const [drawer, setDrawer] = createSignal(false)

  // ── Tabs ───────────────────────────────────────────────────────────────
  const [tab, setTab] = createSignal<FhTab>("my")

  // ── Theme toggle — drives the app-wide [data-color-scheme] via useTheme.
  function toggleTheme() {
    const next = theme.colorScheme() === "dark" ? "light" : "dark"
    theme.setColorScheme(next)
  }
  const isDark = () => theme.colorScheme() === "dark"

  // ── Public gallery (Templates tab + Resources panel) ──────────────────────
  const [gallery] = createResource<ShowcaseProject[]>(async () => {
    try { return await forge.listAllShowcases() }
    catch { return [] }
  })
  const [showcaseOpen, setShowcaseOpen] = createSignal<ShowcaseProject | null>(null)

  const sortedRecents = createMemo(() =>
    [...(projects() ?? [])]
      .sort((a, b) => (b.updated_at ?? "").localeCompare(a.updated_at ?? ""))
      .slice(0, 8)
  )

  // ── Recently viewed — reactive read of the localStorage list ─────────────
  // We don't use createResource here because there's no async work: just a
  // signal that re-reads on the "forge-recent-updated" event we dispatch
  // inside recordRecentView(). One window listener for the whole page.
  const [recentTick, setRecentTick] = createSignal(0)
  createEffect(() => {
    const onUpdated = () => setRecentTick((n) => n + 1)
    window.addEventListener("forge-recent-updated", onUpdated)
    onCleanup(() => window.removeEventListener("forge-recent-updated", onUpdated))
  })

  // Join localStorage IDs against the current project list — so renames /
  // thumbnails always show fresh data, and deleted projects silently fall
  // out of "recents" without us tracking deletions separately.
  const recentlyViewedProjects = createMemo(() => {
    recentTick() // dependency: re-run when storage changes
    const ids = loadRecentIds()
    if (ids.length === 0) return []
    const byId = new Map((projects() ?? []).map((p) => [p.id, p] as const))
    const out: ForgeProject[] = []
    for (const entry of ids) {
      const project = byId.get(entry.id)
      if (project) out.push(project)
    }
    return out
  })

  // Filtered project lists for sidebar views. Filtering happens in-memory on
  // the already-fetched list — at ~hundreds of projects per user even at the
  // upper bound this is well under 1ms, and avoids a per-view network round
  // trip. If the typical-user count ever exceeds ~5k, push the filter to the
  // server with `?starred=1` / `?by_me=1`.
  const starredProjects = createMemo(() =>
    (projects() ?? [])
      .filter((p) => !!p.starred_at)
      .sort((a, b) => (b.starred_at ?? "").localeCompare(a.starred_at ?? "")),
  )
  // "By me" = projects the user created themselves (not cloned from a template).
  // forked_from_project_id is set on clones; null on originals.
  const byMeProjects = createMemo(() =>
    (projects() ?? []).filter((p) => !p.forked_from_project_id),
  )

  // Inline render helper for a single project card. Returns the JSX; doing it
  // as a function (not a component) keeps SolidJS reactivity scoped to the
  // outer `<For>` and avoids creating new components per row.
  function renderProjectCard(p: ForgeProject) {
    const isStarred = () => !!p.starred_at
    const isMenuOpen = () => openMenuId() === p.id
    return (
      <div
        class="fh-card"
        onClick={() => {
          if (isMenuOpen()) {
            setOpenMenuId(null)
            return
          }
          openProject(p)
        }}
      >
        <div
          class="fh-thumb"
          style={{
            background: "#ffffff",
            display: "flex",
            "align-items": "center",
            "justify-content": "center",
          }}
        >
          <Show
            when={p.thumbnail_url}
            fallback={
              <div class="flex items-center justify-center size-full bg-[#fdfdfd] rounded-t-[12px] border-b border-border-weak-base">
                <img
                  src="/forge-f-light.png"
                  alt="Forge Logo"
                  class="size-10 opacity-[0.25] select-none pointer-events-none"
                />
              </div>
            }
          >
            <img
              src={`${FORGE_API_URL}${p.thumbnail_url}`}
              alt={p.name}
              loading="lazy"
              style={{
                width: "100%", height: "100%",
                "object-fit": "cover", "object-position": "top",
                display: "block",
              }}
            />
          </Show>
        </div>

        {/* Hover-revealed actions: star + kebab */}
        <div
          class="fh-card-actions"
          classList={{ "always-on": isStarred() || isMenuOpen() }}
          onClick={(e) => e.stopPropagation()}
        >
          <button
            class="fh-icon-btn"
            classList={{ starred: isStarred() }}
            aria-label={isStarred() ? "Unstar" : "Star"}
            title={isStarred() ? "Unstar" : "Star"}
            onClick={(e) => {
              e.stopPropagation()
              void handleStar(p)
            }}
          >
            <Show
              when={isStarred()}
              fallback={
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2" />
                </svg>
              }
            >
              <svg viewBox="0 0 24 24" fill="currentColor" stroke="none">
                <polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2" />
              </svg>
            </Show>
          </button>
          <button
            class="fh-icon-btn"
            aria-label="More options"
            onClick={(e) => {
              e.stopPropagation()
              setOpenMenuId(isMenuOpen() ? null : p.id)
            }}
          >
            <svg viewBox="0 0 24 24" fill="currentColor">
              <circle cx="5"  cy="12" r="1.7" />
              <circle cx="12" cy="12" r="1.7" />
              <circle cx="19" cy="12" r="1.7" />
            </svg>
          </button>
        </div>

        <Show when={isMenuOpen()}>
          <div class="fh-menu" onClick={(e) => e.stopPropagation()}>
            <button
              onClick={() => {
                setOpenMenuId(null)
                void handleStar(p)
              }}
            >
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2" />
              </svg>
              {isStarred() ? "Unstar" : "Star"}
            </button>
            <button
              class="danger"
              onClick={() => {
                setOpenMenuId(null)
                setConfirmDelete(p)
              }}
            >
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <polyline points="3 6 5 6 21 6" />
                <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6" />
                <path d="M10 11v6M14 11v6" />
              </svg>
              Delete
            </button>
          </div>
        </Show>

        <div class="fh-card-foot flex items-center gap-3 p-3">
          <div
            class="avatar size-8 rounded-full flex items-center justify-center text-white font-bold text-[13px] shrink-0 select-none"
            style={{
              background: "linear-gradient(135deg, oklch(0.78 0.15 55), oklch(0.58 0.21 18))",
            }}
          >
            {initial()}
          </div>
          <div class="flex flex-col min-w-0 flex-1">
            <div class="fh-card-name truncate text-[14px] font-semibold text-v2-text-text-strong leading-tight">
              {p.name}
            </div>
            <div class="fh-card-meta truncate text-[11.5px] text-v2-text-text-muted mt-0.5">
              {formatTimeAgo(p.updated_at)}
            </div>
          </div>
        </div>
      </div>
    )
  }

  function renderViewHeader(title: string, sub: string) {
    return (
      <div class="fh-view-header">
        <h2 class="fh-view-title">{title}</h2>
        <p class="fh-view-sub">{sub}</p>
      </div>
    )
  }

  function renderViewEmpty(title: string, sub: string) {
    return (
      <div class="fh-view-grid">
        <div class="fh-empty">
          <div class="icon" aria-hidden />
          <div class="t">{title}</div>
          <div class="s">{sub}</div>
        </div>
      </div>
    )
  }

  return (
    <>
      <style innerHTML={FORGE_HOME_STYLES} />
      <div class="fh-root">

        {/* ── Sidebar ─────────────────────────────────────────────────── */}
        <aside class={`fh-side ${drawer() ? "open" : ""}`}>
          <div class="fh-side-top">
            <span class="mark" />
            <span class="name">Forge</span>
          </div>

          <button
            class={`fh-side-item ${side() === "home" ? "active" : ""}`}
            onClick={() => { setSide("home"); setDrawer(false) }}
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
              <path d="M3 11l9-8 9 8" /><path d="M5 10v10h14V10" />
            </svg>
            Home
          </button>
          <button
            class={`fh-side-item ${side() === "resources" ? "active" : ""}`}
            onClick={() => { setSide("resources"); setDrawer(false) }}
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
              <path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z" />
              <path d="M3.27 6.96L12 12.01l8.73-5.05M12 22.08V12" />
            </svg>
            Resources
          </button>

          <div class="fh-side-section">Projects</div>
          <button
            class={`fh-side-item ${side() === "all" ? "active" : ""}`}
            onClick={() => { setSide("all"); setTab("my"); setDrawer(false) }}
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
              <rect x="3" y="3" width="7" height="7" /><rect x="14" y="3" width="7" height="7" />
              <rect x="3" y="14" width="7" height="7" /><rect x="14" y="14" width="7" height="7" />
            </svg>
            All
          </button>
          <button
            class={`fh-side-item ${side() === "starred" ? "active" : ""}`}
            onClick={() => { setSide("starred"); setDrawer(false) }}
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
              <polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2" />
            </svg>
            Starred
          </button>
          <button
            class={`fh-side-item ${side() === "by-me" ? "active" : ""}`}
            onClick={() => { setSide("by-me"); setDrawer(false) }}
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
              <circle cx="12" cy="8" r="4" />
              <path d="M4 21v-1a8 8 0 0 1 16 0v1" />
            </svg>
            By me
          </button>

          <div class="fh-side-section">Recents</div>
          <div class="fh-recents-list">
            <Show
              when={sortedRecents().length > 0}
              fallback={<div class="fh-recents-empty">No recent projects</div>}
            >
              <For each={sortedRecents()}>
                {(p) => (
                  <button class="fh-recent" onClick={() => { openProject(p); setDrawer(false) }} title={p.name}>
                    {p.name}
                  </button>
                )}
              </For>
            </Show>
          </div>

          <div class="fh-side-foot" style={{ position: "relative" }}>
            <UserMenu
              user={me() ?? (userInfo
                ? {
                    id:                   userInfo.sub,
                    email:                userInfo.email,
                    username:             userInfo.username,
                    created_at:           "",
                    email_verified:       true,
                    onboarding_completed: true,
                    full_name:            null,
                    role:                 null,
                    company_size:         null,
                    theme_pref:           null,
                  }
                : null)}
              onOpenSettings={openSettings}
            />
          </div>
        </aside>

        {/* Mobile backdrop */}
        <div class={`fh-backdrop ${drawer() ? "on" : ""}`} onClick={() => setDrawer(false)} />

        {/* ── Main ─────────────────────────────────────────────────────── */}
        <main class="fh-main">

          {/* ── Resources panel ── shown when side() === "resources" ── */}
          <Show when={side() === "resources"}>
            <div style={{ padding: "32px 24px 48px", "max-width": "1280px", margin: "0 auto", width: "100%" }}>
              <div style={{ "margin-bottom": "28px" }}>
                <h2 style={{
                  "font-size": "22px",
                  "font-weight": "700",
                  "letter-spacing": "-0.02em",
                  color: "var(--ink, #15140f)",
                  margin: "0 0 6px",
                }}>
                  Community Gallery
                </h2>
                <p style={{
                  "font-size": "14px",
                  color: "var(--muted-2, #6b6358)",
                  margin: "0",
                }}>
                  Browse apps built by the Forge community — click any card to preview, then clone it as your own.
                </p>
              </div>
              <Show
                when={(gallery() ?? []).length > 0}
                fallback={
                  <div class="fh-empty">
                    <div class="icon" aria-hidden />
                    <div class="t">No public apps yet</div>
                    <div class="s">When users showcase their apps, they'll appear here for the community to explore.</div>
                  </div>
                }
              >
                <ShowcaseGrid projects={gallery() ?? []} onOpen={(p) => setShowcaseOpen(p)} />
              </Show>
            </div>
          </Show>

          {/* ── All / Starred / By me — full-bleed grid views ── */}
          <Show when={side() === "all"}>
            {renderViewHeader("All projects", "Everything in your workspace.")}
            <Show
              when={(projects() ?? []).length > 0}
              fallback={renderViewEmpty(
                "No projects yet",
                "Head back home and type an idea to build your first app.",
              )}
            >
              <div class="fh-view-grid">
                <div class="fh-grid">
                  <For each={projects() ?? []}>{(p) => renderProjectCard(p)}</For>
                </div>
              </div>
            </Show>
          </Show>

          <Show when={side() === "starred"}>
            {renderViewHeader("Starred", "Projects you've starred for quick access.")}
            <Show
              when={starredProjects().length > 0}
              fallback={renderViewEmpty(
                "No starred projects",
                "Click the star icon on any project card to add it here.",
              )}
            >
              <div class="fh-view-grid">
                <div class="fh-grid">
                  <For each={starredProjects()}>{(p) => renderProjectCard(p)}</For>
                </div>
              </div>
            </Show>
          </Show>

          <Show when={side() === "by-me"}>
            {renderViewHeader("By me", "Projects you created — clones from templates are hidden.")}
            <Show
              when={byMeProjects().length > 0}
              fallback={renderViewEmpty(
                "No projects by you yet",
                "Anything you create from scratch (not cloned from a template) shows up here.",
              )}
            >
              <div class="fh-view-grid">
                <div class="fh-grid">
                  <For each={byMeProjects()}>{(p) => renderProjectCard(p)}</For>
                </div>
              </div>
            </Show>
          </Show>

          {/* ── Home hero + projects ── only on Home ── */}
          <Show when={side() === "home"}>
          <div class="fh-hero-area">
          <div class="fh-topbar">
            <button class="fh-hamburger" aria-label="Open menu" onClick={() => setDrawer(true)}>
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
                <path d="M3 6h18M3 12h18M3 18h18" />
              </svg>
            </button>
            <div class="fh-mobile-title">
              <span class="mark" />
              Forge
            </div>
            <div style={{ flex: 1 }} />
            <button class="fh-theme-btn" aria-label="Toggle theme" onClick={toggleTheme}>
              <Show
                when={isDark()}
                fallback={
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <circle cx="12" cy="12" r="4" />
                    <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41" />
                  </svg>
                }
              >
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
                </svg>
              </Show>
            </button>
          </div>

          <div class="fh-hero">

            <h1>What should we build, {greeting()}?</h1>

            <div class="fh-prompt">
              <textarea
                placeholder="Ask Forge to create a prototype…"
                value={prompt()}
                rows={2}
                onInput={(e) => setPrompt(e.currentTarget.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault()
                    void createFromPrompt()
                  }
                }}
              />
              <div class="fh-prompt-row">
                <span class="fh-prompt-chip icon" title="Attach">＋</span>
                {/* Model selector */}
                <ModelSelectorPopover
                  model={homeModelState}
                  triggerAs={Button}
                  triggerProps={{
                    variant: "ghost",
                    class: "fh-prompt-chip interactive flex items-center gap-1.5 px-3",
                    style: { height: "28px" },
                    "data-action": "prompt-model",
                  }}
                >
                  <Show when={homeModelState.current()?.provider?.id}>
                    <ProviderIcon
                      id={homeModelState.current()?.provider?.id ?? ""}
                      class="size-3.5 shrink-0 opacity-60"
                    />
                  </Show>
                  <span class="truncate max-w-[140px]">{homeModelState.current()?.name ?? "Select Model"}</span>
                  <Icon name="chevron-down" size="small" class="shrink-0 text-v2-icon-icon-muted opacity-60" />
                </ModelSelectorPopover>
                <button
                  class="fh-prompt-send"
                  disabled={busy() || !prompt().trim()}
                  onClick={() => void createFromPrompt()}
                  aria-label="Send"
                >
                  <Show
                    when={!busy()}
                    fallback={
                      <span class="forge-css-spinner" style={{ width: "18px", height: "18px", "border-width": "2.5px" }} />
                    }
                  >
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">
                      <path d="M12 19V5M5 12l7-7 7 7" />
                    </svg>
                  </Show>
                </button>
              </div>
            </div>
            <div class="fh-prompt-hint">Press Enter to build · Shift+Enter for new line</div>
          </div>
          </div>{/* /fh-hero-area */}

          <div class="fh-projects-wrap">
          <div class="fh-projects">
            <div class="fh-tabs-row">
              <div class="fh-tabs" role="tablist">
                <button class={`fh-tab ${tab() === "my" ? "active" : ""}`} onClick={() => setTab("my")}>My projects</button>
                <button class={`fh-tab ${tab() === "recent" ? "active" : ""}`} onClick={() => setTab("recent")}>Recently viewed</button>
                <button class={`fh-tab ${tab() === "templates" ? "active" : ""}`} onClick={() => setTab("templates")}>Forge Templates</button>
              </div>
              <button class="fh-browse-all" type="button">
                Browse all
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M5 12h14M13 5l7 7-7 7" />
                </svg>
              </button>
            </div>

            <div class="fh-projects-body">
            <Switch>
              <Match when={tab() === "my"}>
                <Show
                  when={(projects() ?? []).length > 0}
                  fallback={
                    <div class="fh-empty">
                      <div class="icon" aria-hidden />
                      <div class="t">No projects yet</div>
                      <div class="s">Type an idea above and press Enter to build your first app.</div>
                    </div>
                  }
                >
                  <div class="fh-grid">
                    <For each={projects() ?? []}>
                      {(p) => renderProjectCard(p)}
                    </For>
                  </div>
                </Show>
              </Match>

              <Match when={tab() === "recent"}>
                <Show
                  when={recentlyViewedProjects().length > 0}
                  fallback={
                    <div class="fh-empty">
                      <div class="icon" aria-hidden />
                      <div class="t">No recently viewed projects</div>
                      <div class="s">Projects you open will show up here for quick access.</div>
                    </div>
                  }
                >
                  <div class="fh-grid">
                    <For each={recentlyViewedProjects()}>{(p) => renderProjectCard(p)}</For>
                  </div>
                </Show>
              </Match>

              <Match when={tab() === "templates"}>
                <Show
                  when={(gallery() ?? []).length > 0}
                  fallback={
                    <div class="fh-empty">
                      <div class="icon" aria-hidden />
                      <div class="t">No templates yet</div>
                      <div class="s">Showcased apps from the community will appear here as templates.</div>
                    </div>
                  }
                >
                  <ShowcaseGrid projects={gallery() ?? []} onOpen={(p) => setShowcaseOpen(p)} />
                </Show>
              </Match>
            </Switch>
            </div>{/* /fh-projects-body */}
          </div>
          </div>
          </Show>{/* /side() === "home" */}

        </main>

        {/* ── Showcase modal ─────────────────────────────────────────────── */}
        <Show when={showcaseOpen()}>
          {(p) => <ShowcaseModal project={p()} onClose={() => setShowcaseOpen(null)} />}
        </Show>

        {/* ── Delete confirm dialog ──────────────────────────────────────── */}
        <Show when={confirmDelete()}>
          {(target) => (
            <div
              class="fh-confirm-backdrop"
              onClick={() => !deleting() && setConfirmDelete(null)}
            >
              <div class="fh-confirm" onClick={(e) => e.stopPropagation()}>
                <h3>Delete this project?</h3>
                <p>
                  "{target().name}" and its container will be permanently removed.
                  This can't be undone.
                </p>
                <div class="fh-confirm-row">
                  <button
                    onClick={() => setConfirmDelete(null)}
                    disabled={deleting()}
                  >
                    Cancel
                  </button>
                  <button
                    class="danger"
                    disabled={deleting()}
                    onClick={() => void handleDeleteConfirmed()}
                  >
                    {deleting() ? "Deleting…" : "Delete"}
                  </button>
                </div>
              </div>
            </div>
          )}
        </Show>

        <style>{`
          @keyframes fh-spin { to { transform: rotate(360deg); } }
        `}</style>
      </div>
    </>
  )
}

function HomeDesign() {
  const sync = useGlobalSync()
  const layout = useLayout()
  const platform = usePlatform()
  const dialog = useDialog()
  const navigate = useNavigate()
  const server = useServer()
  const language = useLanguage()
  const [state, setState] = createStore({ search: "", project: undefined as string | undefined })

  const projects = createMemo(() => layout.projects.list())
  const selectedProject = createMemo(
    () => projects().find((project) => project.worktree === state.project) ?? projects()[0],
  )
  const projectDirectories = createMemo(() => {
    const project = selectedProject()
    if (!project) return []
    return [project.worktree, ...(project.sandboxes ?? [])]
  })
  const search = createMemo(() => state.search.trim())
  const sessionLoad = useQuery(() => ({
    queryKey: ["home", "sessions", ...projectDirectories()] as const,
    queryFn: async () => {
      await Promise.all(projectDirectories().map((directory) => sync.project.loadSessions(directory)))
      return null
    },
  }))

  const projectByID = createMemo(
    () => new Map(projects().flatMap((project) => (project.id ? [[project.id, project] as const] : []))),
  )
  const records = createMemo(() =>
    [
      ...new Map(
        projectDirectories()
          .flatMap((directory) => sortedRootSessions(sync.child(directory, { bootstrap: false })[0], Date.now()))
          .map((session) => [`${pathKey(session.directory)}:${session.id}`, session] as const),
      ).values(),
    ]
      .sort((a, b) => (b.time.updated ?? b.time.created) - (a.time.updated ?? a.time.created))
      .flatMap((session) => {
        const project = projectForSession(session, projects(), projectByID())
        if (!project) return []
        return {
          session,
          project,
          projectName: displayName(project),
        }
      })
      .filter((record) => {
        const value = search().toLowerCase()
        if (!value) return true
        return `${record.session.title} ${record.projectName}`.toLowerCase().includes(value)
      })
      .slice(0, HOME_SESSION_LIMIT),
  )
  const groups = createMemo(() => groupSessions(records(), language))

  function selectProject(directory: string) {
    if (!projects().some((project) => project.worktree === directory)) return
    setState("project", directory)
  }

  function addProject(directory: string) {
    layout.projects.open(directory)
    server.projects.touch(directory)
    setState("project", directory)
  }

  function openNewSession() {
    const project = selectedProject()
    if (!project) {
      void chooseProject()
      return
    }
    layout.projects.open(project.worktree)
    server.projects.touch(project.worktree)
    navigate(`/${base64Encode(project.worktree)}/session?from=home`)
  }

  function openSession(session: Session) {
    const project = projectForSession(session, projects(), projectByID())
    layout.projects.open(project?.worktree ?? session.directory)
    server.projects.touch(project?.worktree ?? session.directory)
    navigate(`/${base64Encode(session.directory)}/session/${session.id}`)
  }

  async function chooseProject() {
    function resolve(result: string | string[] | null) {
      if (Array.isArray(result)) {
        result.forEach(addProject)
        if (result[0]) setState("project", result[0])
        return
      }
      if (result) addProject(result)
    }

    if (platform.openDirectoryPickerDialog && server.isLocal()) {
      const result = await platform.openDirectoryPickerDialog?.({
        title: language.t("command.project.open"),
        multiple: true,
      })
      resolve(result)
      return
    }

    dialog.show(
      () => <DialogSelectDirectory multiple={true} onSelect={resolve} />,
      () => resolve(null),
    )
  }

  function openSettings() {
    void import("@/components/dialog-settings").then((x) => {
      dialog.show(() => <x.DialogSettings />)
    })
  }

  return (
    <div class="mx-auto grid w-full h-full max-w-[1080px] gap-8 px-6 pb-16 lg:grid-cols-[280px_minmax(0,720px)]">
      <HomeProjectColumn
        projects={projects()}
        selected={selectedProject()?.worktree}
        selectProject={selectProject}
        chooseProject={() => void chooseProject()}
        openSettings={openSettings}
        openHelp={() => platform.openLink("https://opencode.ai/desktop-feedback")}
        language={language}
      />

      <section
        class="min-w-0 flex-1 flex flex-col overflow-y-hidden pt-12"
        aria-label={language.t("sidebar.project.recentSessions")}
      >
        <HomeSessionSearch
          value={state.search}
          placeholder={language.t("home.sessions.search.placeholder")}
          onInput={(value) => setState("search", value)}
        />
        <div class="mt-3 overflow-auto flex-1">
          <div class="pt-3 flex flex-col gap-6">
            <Show when={!sessionLoad.isLoading} fallback={<HomeSessionSkeleton label={language.t("common.loading")} />}>
              <Show
                when={groups().length > 0}
                fallback={
                  <div class="flex min-w-0 flex-col gap-4">
                    <HomeSessionGroupHeader title={language.t("home.sessions.empty")} onNewSession={openNewSession} />
                  </div>
                }
              >
                <For each={groups()}>
                  {(group, index) => (
                    <div class="flex min-w-0 flex-col gap-4">
                      <HomeSessionGroupHeader
                        title={group.title}
                        onNewSession={index() === 0 ? openNewSession : undefined}
                      />
                      <div class="flex min-w-0 flex-col gap-px">
                        <For each={group.sessions}>
                          {(record) => <HomeSessionRow record={record} openSession={openSession} />}
                        </For>
                      </div>
                    </div>
                  )}
                </For>
              </Show>
            </Show>
          </div>
        </div>
      </section>
    </div>
  )
}

function HomeProjectColumn(props: {
  projects: LocalProject[]
  selected?: string
  selectProject: (directory: string) => void
  chooseProject: () => void
  openSettings: () => void
  openHelp: () => void
  language: ReturnType<typeof useLanguage>
}) {
  return (
    <aside class="flex min-w-0 flex-col lg:pt-[52px]" aria-label={props.language.t("home.projects")}>
      <div class="flex h-7 min-w-0 items-center justify-between pl-3">
        <div class={HOME_SECTION_LABEL}>{props.language.t("home.projects")}</div>
        <IconButtonV2
          data-action="home-add-project"
          variant="ghost-muted"
          size="large"
          class="titlebar-icon [&_[data-slot=icon-svg]]:text-v2-icon-icon-muted"
          icon={<IconV2 name="folder-add-left" />}
          onClick={props.chooseProject}
          aria-label={props.language.t("home.project.add")}
        />
      </div>
      <div class="mt-4 flex max-h-[min(572px,calc(100vh_-_300px))] min-w-0 flex-col gap-1 overflow-y-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
        <Show
          when={props.projects.length > 0}
          fallback={
            <button
              type="button"
              class={`${HOME_PROJECT_NAV_ROW} text-v2-text-text-faint [&>[data-slot=icon-svg]]:text-v2-icon-icon-muted`}
              onClick={props.chooseProject}
            >
              <IconV2 name="folder-add-left" size="small" />
              <span>{props.language.t("home.project.add")}</span>
            </button>
          }
        >
          <For each={props.projects}>
            {(project) => (
              <button
                type="button"
                data-component="home-project-row"
                class={HOME_PROJECT_NAV_ROW}
                classList={{ "bg-v2-overlay-simple-overlay-hover": props.selected === project.worktree }}
                data-selected={props.selected === project.worktree ? "" : undefined}
                aria-current={props.selected === project.worktree ? "page" : undefined}
                onClick={() => props.selectProject(project.worktree)}
              >
                <HomeProjectAvatar project={project} />
                <span>{displayName(project)}</span>
              </button>
            )}
          </For>
        </Show>
      </div>
      <div class="mt-4 flex min-w-0 flex-col gap-1">
        <button
          type="button"
          class={`${HOME_PROJECT_NAV_ROW} text-v2-text-text-faint [&>[data-slot=icon-svg]]:text-v2-icon-icon-muted`}
          onClick={props.openSettings}
        >
          <IconV2 name="settings-gear" size="small" />
          <span>{props.language.t("sidebar.settings")}</span>
        </button>
        <button
          type="button"
          class={`${HOME_PROJECT_NAV_ROW} text-v2-text-text-faint [&>[data-slot=icon-svg]]:text-v2-icon-icon-muted`}
          onClick={props.openHelp}
        >
          <IconV2 name="help" size="small" />
          <span>{props.language.t("sidebar.help")}</span>
        </button>
      </div>
    </aside>
  )
}

function HomeProjectAvatar(props: { project: LocalProject }) {
  const name = createMemo(() => displayName(props.project))
  return (
    <AvatarV2
      fallback={name()}
      src={getProjectAvatarSource(props.project.id, props.project.icon)}
      kind="org"
      size="small"
      {...getAvatarColors(props.project.icon?.color)}
      class="size-4 rounded"
    />
  )
}

function HomeSessionSearch(props: { value: string; placeholder: string; onInput: (value: string) => void }) {
  return (
    <label class="ml-4 flex h-9 w-[calc(100%_-_48px)] sticky top-0 inset-x-0 items-center gap-2 rounded-[6px] bg-v2-background-bg-deep px-3 py-1 text-v2-icon-icon-muted transition-[background-color,box-shadow] duration-[120ms] ease-in-out focus-within:bg-v2-background-bg-base focus-within:shadow-[0_0_0_0.5px_var(--v2-border-border-focus),var(--v2-elevation-raised)]">
      <IconV2 name="magnifying-glass" size="small" />
      <input
        class="min-w-0 flex-1 border-0 bg-transparent text-v2-text-text-base outline-0 [font-weight:440] placeholder:text-v2-text-text-faint"
        value={props.value}
        placeholder={props.placeholder}
        aria-label={props.placeholder}
        onInput={(event) => props.onInput(event.currentTarget.value)}
      />
    </label>
  )
}

function HomeSessionGroupHeader(props: { title: string; onNewSession?: () => void }) {
  const language = useLanguage()
  return (
    <div class="flex h-7 min-w-0 items-center justify-between px-4">
      <div class={HOME_SECTION_LABEL}>{props.title}</div>
      <Show when={props.onNewSession}>
        {(onNewSession) => (
          <ButtonV2
            data-action="home-new-session"
            variant="ghost"
            size="normal"
            icon="edit"
            class="h-7 px-2 text-v2-text-text-muted [font-weight:530]"
            onClick={onNewSession()}
          >
            {language.t("command.session.new")}
          </ButtonV2>
        )}
      </Show>
    </div>
  )
}

function HomeSessionRow(props: { record: HomeSessionRecord; openSession: (session: Session) => void }) {
  const globalSync = useGlobalSync()
  const notification = useNotification()
  const permission = usePermission()
  const [sessionStore] = globalSync.child(props.record.session.directory, { bootstrap: false })
  const title = createMemo(() => sessionTitle(props.record.session.title) || props.record.session.id)
  const unseenCount = createMemo(() => notification.session.unseenCount(props.record.session.id))
  const hasError = createMemo(() => notification.session.unseenHasError(props.record.session.id))
  const hasPermissions = createMemo(
    () =>
      !!sessionPermissionRequest(sessionStore.session, sessionStore.permission, props.record.session.id, (item) => {
        return !permission.autoResponds(item, props.record.session.directory)
      }),
  )
  const isWorking = createMemo(() => {
    if (hasPermissions()) return false
    return sessionStore.session_working(props.record.session.id)
  })
  const tint = createMemo(() => messageAgentColor(sessionStore.message[props.record.session.id], sessionStore.agent))
  const showStatus = createMemo(() => isWorking() || hasPermissions() || hasError() || unseenCount() > 0)

  return (
    <button
      type="button"
      data-component="home-session-row"
      class={`${HOME_ROW} h-10 gap-2 px-6 py-3 pl-4`}
      onClick={() => props.openSession(props.record.session)}
    >
      <Show when={showStatus()}>
        <div
          class="flex size-4 shrink-0 items-center justify-center"
          style={{ color: tint() ?? "var(--icon-interactive-base)" }}
        >
          <Switch>
            <Match when={isWorking()}>
              <Spinner class="size-[15px]" />
            </Match>
            <Match when={hasPermissions()}>
              <div class="size-1.5 rounded-full bg-surface-warning-strong" />
            </Match>
            <Match when={hasError()}>
              <div class="size-1.5 rounded-full bg-text-diff-delete-base" />
            </Match>
            <Match when={unseenCount() > 0}>
              <div class="size-1.5 rounded-full bg-text-interactive-base" />
            </Match>
          </Switch>
        </div>
      </Show>
      <span
        class={`min-w-0 overflow-hidden text-ellipsis whitespace-nowrap text-v2-text-text-base [font-weight:530] ${props.record.projectName ? "max-w-[min(70%,480px)] flex-[0_1_auto]" : "flex-[1_1_auto]"}`}
      >
        {title()}
      </span>
      <Show when={props.record.projectName}>
        <span class="min-w-0 flex-[1_1_auto] overflow-hidden text-ellipsis whitespace-nowrap text-v2-text-text-muted [font-weight:440]">
          {props.record.projectName}
        </span>
      </Show>
    </button>
  )
}

function HomeSessionSkeleton(props: { label: string }) {
  return (
    <div class="flex min-w-0 flex-col gap-4">
      <div class="flex h-7 min-w-0 items-center justify-between px-4">
        <div class={HOME_SECTION_LABEL}>{props.label}</div>
      </div>
      <div class="flex min-w-0 flex-col gap-px" aria-hidden="true">
        <For each={[0, 1, 2, 3]}>{() => <div class="h-10 rounded-[6px] bg-v2-background-bg-deep opacity-70" />}</For>
      </div>
    </div>
  )
}

function groupSessions(records: HomeSessionRecord[], language: ReturnType<typeof useLanguage>): HomeSessionGroup[] {
  const now = DateTime.local()
  const yesterday = now.minus({ days: 1 })
  const todaySessions = records.filter((record) =>
    DateTime.fromMillis(record.session.time.updated ?? record.session.time.created).hasSame(now, "day"),
  )
  const yesterdaySessions = records.filter((record) =>
    DateTime.fromMillis(record.session.time.updated ?? record.session.time.created).hasSame(yesterday, "day"),
  )
  const olderSessions = records.filter((record) => {
    const time = DateTime.fromMillis(record.session.time.updated ?? record.session.time.created)
    return !time.hasSame(now, "day") && !time.hasSame(yesterday, "day")
  })
  const olderTitle =
    todaySessions.length === 0 && yesterdaySessions.length === 0
      ? language.t("sidebar.project.recentSessions")
      : language.t("home.sessions.group.older")

  return [
    { id: "today" as const, title: language.t("home.sessions.group.today"), sessions: todaySessions },
    { id: "yesterday" as const, title: language.t("home.sessions.group.yesterday"), sessions: yesterdaySessions },
    { id: "older" as const, title: olderTitle, sessions: olderSessions },
  ].filter((group) => group.sessions.length > 0)
}

function LegacyHome() {
  const sync = useGlobalSync()
  const layout = useLayout()
  const platform = usePlatform()
  const dialog = useDialog()
  const navigate = useNavigate()
  const server = useServer()
  const language = useLanguage()
  const homedir = createMemo(() => sync.data.path.home)
  const recent = createMemo(() => {
    return sync.data.project
      .slice()
      .sort((a, b) => (b.time.updated ?? b.time.created) - (a.time.updated ?? a.time.created))
      .slice(0, 5)
  })

  const serverDotClass = createMemo(() => {
    const healthy = server.healthy()
    if (healthy === true) return "bg-icon-success-base"
    if (healthy === false) return "bg-icon-critical-base"
    return "bg-border-weak-base"
  })

  function openProject(directory: string) {
    layout.projects.open(directory)
    server.projects.touch(directory)
    navigate(`/${base64Encode(directory)}`)
  }

  async function chooseProject() {
    function resolve(result: string | string[] | null) {
      if (Array.isArray(result)) {
        for (const directory of result) {
          openProject(directory)
        }
      } else if (result) {
        openProject(result)
      }
    }

    if (platform.openDirectoryPickerDialog && server.isLocal()) {
      const result = await platform.openDirectoryPickerDialog?.({
        title: language.t("command.project.open"),
        multiple: true,
      })
      resolve(result)
    } else {
      dialog.show(
        () => <DialogSelectDirectory multiple={true} onSelect={resolve} />,
        () => resolve(null),
      )
    }
  }

  return (
    <div class="mx-auto mt-55 w-full md:w-auto px-4">
      <Logo class="md:w-xl opacity-12" />
      <Button
        size="large"
        variant="ghost"
        class="mt-4 mx-auto text-14-regular text-text-weak"
        onClick={() => dialog.show(() => <DialogSelectServer />)}
      >
        <div
          classList={{
            "size-2 rounded-full": true,
            [serverDotClass()]: true,
          }}
        />
        {server.name}
      </Button>
      <Switch>
        <Match when={sync.data.project.length > 0}>
          <div class="mt-20 w-full flex flex-col gap-4">
            <div class="flex gap-2 items-center justify-between pl-3">
              <div class="text-14-medium text-text-strong">{language.t("home.recentProjects")}</div>
              <Button icon="folder-add-left" size="normal" class="pl-2 pr-3" onClick={chooseProject}>
                {language.t("command.project.open")}
              </Button>
            </div>
            <ul class="flex flex-col gap-2">
              <For each={recent()}>
                {(project) => (
                  <Button
                    size="large"
                    variant="ghost"
                    class="text-14-mono text-left justify-between px-3"
                    onClick={() => openProject(project.worktree)}
                  >
                    {project.worktree.replace(homedir(), "~")}
                    <div class="text-14-regular text-text-weak">
                      {DateTime.fromMillis(project.time.updated ?? project.time.created).toRelative()}
                    </div>
                  </Button>
                )}
              </For>
            </ul>
          </div>
        </Match>
        <Match when={!sync.ready}>
          <div class="mt-30 mx-auto flex flex-col items-center gap-3">
            <div class="text-12-regular text-text-weak">{language.t("common.loading")}</div>
            <Button class="px-3" onClick={chooseProject}>
              {language.t("command.project.open")}
            </Button>
          </div>
        </Match>
        <Match when={true}>
          <div class="mt-30 mx-auto flex flex-col items-center gap-3">
            <Icon name="folder-add-left" size="large" />
            <div class="flex flex-col gap-1 items-center justify-center">
              <div class="text-14-medium text-text-strong">{language.t("home.empty.title")}</div>
              <div class="text-12-regular text-text-weak">{language.t("home.empty.description")}</div>
            </div>
            <Button class="px-3 mt-1" onClick={chooseProject}>
              {language.t("command.project.open")}
            </Button>
          </div>
        </Match>
      </Switch>
    </div>
  )
}
