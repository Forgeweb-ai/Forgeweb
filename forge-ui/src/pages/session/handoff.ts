import type { SelectedLineRange } from "@/context/file"

type HandoffSession = {
  prompt: string
  files: Record<string, SelectedLineRange | null>
}

/**
 * In-memory pending attachments for the home → new-session handoff.
 *
 * When the user attaches files on the home composer and submits, we can't
 * encode raw File objects in the URL or localStorage. Instead we park them
 * here keyed by the project's workspace_path. The session page reads + clears
 * this on mount (when ?from=home is present) and injects the files into the
 * composer's prompt state as ImageAttachmentPart entries.
 *
 * Lifetime: process memory only. If the user reloads between submit and
 * session mount, the attachments are lost — but the prompt text survives via
 * URL, so the navigation isn't broken.
 */
export type PendingAttachment = {
  name: string
  mime: string
  dataUrl: string   // already base64-encoded, ready to attach
  size: number
}

const MAX = 40

const store = {
  session: new Map<string, HandoffSession>(),
  terminal: new Map<string, string[]>(),
  pendingAttachments: new Map<string, PendingAttachment[]>(),
}

const touch = <K, V>(map: Map<K, V>, key: K, value: V) => {
  map.delete(key)
  map.set(key, value)
  while (map.size > MAX) {
    const first = map.keys().next().value
    if (first === undefined) return
    map.delete(first)
  }
}

export const setSessionHandoff = (key: string, patch: Partial<HandoffSession>) => {
  const prev = store.session.get(key) ?? { prompt: "", files: {} }
  touch(store.session, key, { ...prev, ...patch })
}

export const getSessionHandoff = (key: string) => store.session.get(key)

export const setTerminalHandoff = (key: string, value: string[]) => {
  touch(store.terminal, key, value)
}

export const getTerminalHandoff = (key: string) => store.terminal.get(key)

export const setPendingAttachments = (workspaceKey: string, value: PendingAttachment[]) => {
  if (value.length === 0) {
    store.pendingAttachments.delete(workspaceKey)
    return
  }
  touch(store.pendingAttachments, workspaceKey, value)
}

/** Read pending attachments and clear them so they aren't re-applied on remount. */
export const takePendingAttachments = (workspaceKey: string): PendingAttachment[] => {
  const value = store.pendingAttachments.get(workspaceKey)
  store.pendingAttachments.delete(workspaceKey)
  return value ?? []
}
