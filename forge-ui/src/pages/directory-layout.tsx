import { DataProvider } from "@opencode-ai/ui/context"
import { showToast } from "@opencode-ai/ui/toast"
import { base64Encode } from "@opencode-ai/core/util/encode"
import { useLocation, useNavigate, useParams } from "@solidjs/router"
import { createEffect, createMemo, createResource, type ParentProps, Show } from "solid-js"
import { useLanguage } from "@/context/language"
import { LocalProvider } from "@/context/local"
import { SDKProvider } from "@/context/sdk"
import { useSync } from "@/context/sync"
import { decode64 } from "@/utils/base64"
import { Schema } from "effect"

// When running in Forge mode (VITE_API_URL set), validate that the URL
// directory belongs to a known Forge project. If it doesn't — e.g. a
// stale browser tab pointing at a random local path — redirect to home.
const FORGE_API_URL: string | undefined = import.meta.env.VITE_API_URL || undefined

async function isForgeProject(directory: string): Promise<boolean> {
  if (!FORGE_API_URL) return true   // not in Forge mode — allow any directory
  try {
    const res = await fetch(`${FORGE_API_URL}/api/projects`)
    if (!res.ok) return true         // forge-server down — don't block the user
    const projects: Array<{ workspace_path: string }> = await res.json()
    return projects.some((p) => p.workspace_path === directory)
  } catch {
    return true                      // network error — don't block the user
  }
}

function DirectoryDataProvider(props: ParentProps<{ directory: string }>) {
  const location = useLocation()
  const navigate = useNavigate()
  const params = useParams()
  const sync = useSync()
  const slug = createMemo(() => base64Encode(props.directory))

  createEffect(() => {
    const next = sync.data.path.directory
    if (!next || next === props.directory) return
    const path = location.pathname.slice(slug().length + 1)
    navigate(`/${base64Encode(next)}${path}${location.search}${location.hash}`, { replace: true })
  })

  createResource(
    () => params.id,
    (id) => sync.session.sync(id),
  )

  return (
    <DataProvider
      data={sync.data}
      directory={props.directory}
      onNavigateToSession={(sessionID: string) => navigate(`/${slug()}/session/${sessionID}`)}
      onSessionHref={(sessionID: string) => `/${slug()}/session/${sessionID}`}
    >
      <LocalProvider>{props.children}</LocalProvider>
    </DataProvider>
  )
}

export const ProjectDirString = Schema.String.pipe(Schema.brand("ProjectDirString"))
export type ProjectDirString = Schema.Schema.Type<typeof ProjectDirString>

export function decodeDirectory(dir: string): ProjectDirString | undefined {
  const decoded = decode64(dir)
  if (!decoded) return
  return ProjectDirString.make(decoded)
}

export default function Layout(props: ParentProps) {
  const params = useParams()
  const language = useLanguage()
  const navigate = useNavigate()
  let invalid = ""

  const resolved = createMemo(() => {
    if (!params.dir) return ""
    return decodeDirectory(params.dir) ?? ""
  })

  // Invalid base64 URL → redirect home
  createEffect(() => {
    const dir = params.dir
    if (!dir) return
    if (resolved()) {
      invalid = ""
      return
    }
    if (invalid === dir) return
    invalid = dir
    showToast({
      variant: "error",
      title: language.t("common.requestFailed"),
      description: language.t("directory.error.invalidUrl"),
    })
    navigate("/", { replace: true })
  })

  // Forge-mode guard: if the directory isn't a known forge project, kick back home
  createEffect(() => {
    const dir = resolved()
    if (!dir || !FORGE_API_URL) return
    void isForgeProject(dir).then((valid) => {
      if (!valid) {
        showToast({
          variant: "error",
          title: "Invalid project",
          description: "This session URL doesn't match a Forge project. Redirecting home…",
        })
        navigate("/", { replace: true })
      }
    })
  })

  return (
    <Show when={resolved()} keyed>
      {(resolved) => (
        <SDKProvider directory={resolved}>
          <DirectoryDataProvider directory={resolved}>{props.children}</DirectoryDataProvider>
        </SDKProvider>
      )}
    </Show>
  )
}
