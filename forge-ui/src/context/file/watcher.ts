import type { FileNode } from "@opencode-ai/sdk/v2"

type WatcherEvent = {
  type: string
  properties: unknown
}

type WatcherOps = {
  normalize: (input: string) => string
  hasFile: (path: string) => boolean
  isOpen?: (path: string) => boolean
  loadFile: (path: string) => void
  node: (path: string) => FileNode | undefined
  isDirLoaded: (path: string) => boolean
  refreshDir: (path: string) => void
}

/**
 * Walk up the path segments from the direct parent to the root ("").
 * Refresh the first ancestor directory that is already loaded — this ensures
 * newly-created directories (like "app/") appear in the tree even when they
 * don't exist yet as tree nodes.
 */
function refreshNearestLoadedAncestor(path: string, ops: WatcherOps) {
  const parts = path.split("/")
  for (let i = parts.length - 1; i >= 0; i--) {
    const ancestor = parts.slice(0, i).join("/")
    if (ops.isDirLoaded(ancestor)) {
      ops.refreshDir(ancestor)
      return
    }
  }
}

export function invalidateFromWatcher(event: WatcherEvent, ops: WatcherOps) {
  // Handle AI file-edit events (emitted when opencode writes a file)
  if (event.type === "file.edited") {
    const props =
      typeof event.properties === "object" && event.properties
        ? (event.properties as Record<string, unknown>)
        : undefined
    const rawPath = typeof props?.file === "string" ? props.file : undefined
    if (!rawPath) return

    const path = ops.normalize(rawPath)
    if (!path) return
    if (path.startsWith(".git/")) return

    // Reload the file if it's open or known, so content updates immediately
    if (ops.hasFile(path) || ops.isOpen?.(path)) {
      ops.loadFile(path)
    }

    // Refresh the nearest loaded ancestor directory so new files/dirs appear in the tree.
    // e.g. when "app/page.tsx" is written and "app" is a brand-new dir, parent="app" won't
    // be loaded yet — walk up until we reach "" (root) which is always loaded.
    refreshNearestLoadedAncestor(path, ops)
    return
  }

  if (event.type !== "file.watcher.updated") return
  const props =
    typeof event.properties === "object" && event.properties ? (event.properties as Record<string, unknown>) : undefined
  const rawPath = typeof props?.file === "string" ? props.file : undefined
  const kind = typeof props?.event === "string" ? props.event : undefined
  if (!rawPath) return
  if (!kind) return

  const path = ops.normalize(rawPath)
  if (!path) return
  if (path.startsWith(".git/")) return

  if (ops.hasFile(path) || ops.isOpen?.(path)) {
    ops.loadFile(path)
  }

  if (kind === "change") {
    const dir = (() => {
      if (path === "") return ""
      const node = ops.node(path)
      if (node?.type !== "directory") return
      return path
    })()
    if (dir === undefined) return
    if (!ops.isDirLoaded(dir)) return
    ops.refreshDir(dir)
    return
  }
  if (kind !== "add" && kind !== "unlink") return

  refreshNearestLoadedAncestor(path, ops)
}
