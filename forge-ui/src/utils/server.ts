import { createOpencodeClient } from "@opencode-ai/sdk/v2/client"
import type { ServerConnection } from "@/context/server"
import { decode64 } from "@/utils/base64"

// Forge mode: opencode is reached via /opencode on forge-server, which gates
// the proxy with the same JWT the rest of the Forge UI uses. Pulling the
// token from localStorage at request time (via a getter) means rotated /
// logged-in tokens take effect on the next call without rebuilding the SDK.
const FORGE_JWT_KEY = "forge_jwt"
function readForgeJwt(): string | null {
  if (typeof localStorage === "undefined") return null
  try { return localStorage.getItem(FORGE_JWT_KEY) } catch { return null }
}

export function authTokenFromCredentials(input: { username?: string; password: string }) {
  return btoa(`${input.username ?? "opencode"}:${input.password}`)
}

export function authFromToken(token: string | null) {
  const decoded = decode64(token ?? undefined)
  if (!decoded) return
  const separator = decoded.indexOf(":")
  if (separator === -1) return
  return {
    username: decoded.slice(0, separator) || "opencode",
    password: decoded.slice(separator + 1),
  }
}

export function createSdkForServer({
  server,
  ...config
}: Omit<NonNullable<Parameters<typeof createOpencodeClient>[0]>, "baseUrl"> & {
  server: ServerConnection.HttpBase
}) {
  const basicAuth = (() => {
    if (!server.password) return
    return {
      Authorization: `Basic ${authTokenFromCredentials({ username: server.username, password: server.password })}`,
    }
  })()

  // In Forge mode the SDK targets `${VITE_API_URL}/opencode`; the proxy on
  // forge-server validates a Bearer JWT and injects the per-user provider
  // keys before forwarding to opencode. We always wrap fetch (rather than
  // baking the header in once at SDK construction) so the latest token from
  // localStorage is used per request — survives logout/login without a
  // refresh.
  const inForgeMode = !!(import.meta.env.VITE_API_URL as string | undefined)
  // Cast: Bun's global `fetch` type carries an extra `preconnect` member that
  // a config-provided fetch won't have. We only ever CALL it, so the plain
  // callable shape is all we rely on.
  const baseFetch = ((config as { fetch?: typeof fetch }).fetch ?? fetch) as typeof fetch
  // Cast (not annotation): Bun's `typeof fetch` requires a `preconnect`
  // member our wrapper doesn't (and needn't) have — the SDK only CALLS it.
  const wrappedFetch = (inForgeMode
    ? (input: RequestInfo | URL, init?: RequestInit) => {
        const jwt = readForgeJwt()
        if (!jwt) return baseFetch(input, init)

        // The SDK calls fetch in two shapes:
        //   1. regular requests → fetch(urlString, init)
        //   2. SSE (sse/createSseClient.gen.ts:122) → fetch(new Request(url, init))
        // For (2), init is usually undefined and the headers live on the
        // Request object — we must clone the Request with merged headers,
        // otherwise the Bearer token never lands on /global/event and the
        // event stream 401s, which is what made chat responses look stuck
        // until refresh.
        if (input instanceof Request) {
          const headers = new Headers(input.headers)
          if (!headers.has("Authorization")) headers.set("Authorization", `Bearer ${jwt}`)
          const cloned = new Request(input, { headers })
          return baseFetch(cloned, init)
        }

        const headers = new Headers((init as RequestInit | undefined)?.headers ?? {})
        if (!headers.has("Authorization")) headers.set("Authorization", `Bearer ${jwt}`)
        return baseFetch(input, { ...(init ?? {}), headers })
      }
    : baseFetch) as typeof fetch

  return createOpencodeClient({
    ...config,
    fetch: wrappedFetch,
    headers: {
      ...(config.headers instanceof Headers ? Object.fromEntries(config.headers.entries()) : config.headers),
      ...basicAuth,
    },
    baseUrl: server.url,
  })
}
