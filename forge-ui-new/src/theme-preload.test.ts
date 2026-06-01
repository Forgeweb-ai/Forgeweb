import { beforeEach, describe, expect, test } from "bun:test"

const src = await Bun.file(new URL("../public/oc-theme-preload.js", import.meta.url)).text()

const run = () => Function(src)()

beforeEach(() => {
  document.head.innerHTML = ""
  document.documentElement.removeAttribute("data-theme")
  document.documentElement.removeAttribute("data-color-scheme")
  localStorage.clear()
  Object.defineProperty(window, "matchMedia", {
    value: () =>
      ({
        matches: false,
      }) as MediaQueryList,
    configurable: true,
  })
})

describe("theme preload", () => {
  test("migrates legacy oc-1 to oc-2 and forward-copies legacy keys", () => {
    // Pre-rename storage from an older session — preload should migrate to
    // the new `forge-theme-*` namespace AND apply the oc-1 → oc-2 normalize.
    localStorage.setItem("opencode-theme-id", "oc-1")
    localStorage.setItem("opencode-theme-css-light", "--background-base:#fff;")
    localStorage.setItem("opencode-theme-css-dark", "--background-base:#000;")

    run()

    expect(document.documentElement.dataset.theme).toBe("oc-2")
    expect(document.documentElement.dataset.colorScheme).toBe("light")
    // Normalized value lives on the new key.
    expect(localStorage.getItem("forge-theme-id")).toBe("oc-2")
    // Legacy theme-id key was forward-migrated then cleared by the oc-2 reset.
    expect(localStorage.getItem("opencode-theme-id")).toBeNull()
    // oc-2 reset wipes cached css under BOTH namespaces.
    expect(localStorage.getItem("forge-theme-css-light")).toBeNull()
    expect(localStorage.getItem("forge-theme-css-dark")).toBeNull()
    expect(document.getElementById("oc-theme-preload")).toBeNull()
  })

  test("keeps cached css for non-default themes (legacy keys)", () => {
    localStorage.setItem("opencode-theme-id", "nightowl")
    localStorage.setItem("opencode-theme-css-light", "--background-base:#fff;")

    run()

    expect(document.documentElement.dataset.theme).toBe("nightowl")
    expect(document.getElementById("oc-theme-preload")?.textContent).toContain("--background-base:#fff;")
    // Forward-migrated to forge-* on first read.
    expect(localStorage.getItem("forge-theme-id")).toBe("nightowl")
    expect(localStorage.getItem("forge-theme-css-light")).toBe("--background-base:#fff;")
    expect(localStorage.getItem("opencode-theme-id")).toBeNull()
    expect(localStorage.getItem("opencode-theme-css-light")).toBeNull()
  })

  test("uses forge-* keys directly, ignores legacy when both present", () => {
    // New key wins — no migration overwrite of a fresh value.
    localStorage.setItem("forge-theme-id", "dracula")
    localStorage.setItem("forge-theme-css-light", "--background-base:#abc;")
    // Stale legacy data — must be ignored.
    localStorage.setItem("opencode-theme-id", "matrix")
    localStorage.setItem("opencode-theme-css-light", "--background-base:#xyz;")

    run()

    expect(document.documentElement.dataset.theme).toBe("dracula")
    expect(document.getElementById("oc-theme-preload")?.textContent).toContain("--background-base:#abc;")
    // Legacy keys untouched (no migration when new value already exists).
    expect(localStorage.getItem("opencode-theme-id")).toBe("matrix")
  })
})
