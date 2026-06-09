import { readFileSync } from "node:fs"
import solidPlugin from "vite-plugin-solid"
import tailwindcss from "@tailwindcss/vite"
import { fileURLToPath } from "url"
// Resolve the canonical paths for SolidJS packages from THIS package's own
// node_modules. Without this, Vite may pick up duplicate instances from
// workspace dependencies (e.g. opencode's bun-cached copies), which breaks
// SolidJS context sharing — causing errors like:
//   "useLocation can only be used inside a Route"
const solidDir        = fileURLToPath(new URL("./node_modules/solid-js", import.meta.url))
const solidRouterDir  = fileURLToPath(new URL("./node_modules/@solidjs/router", import.meta.url))
const solidMetaDir    = fileURLToPath(new URL("./node_modules/@solidjs/meta", import.meta.url))

const theme = fileURLToPath(new URL("./public/oc-theme-preload.js", import.meta.url))

const channel = (() => {
  const raw = process.env.OPENCODE_CHANNEL
  if (raw === "dev" || raw === "beta" || raw === "prod") return raw
  if (process.env.OPENCODE_CHANNEL === "latest") return "prod"
  return "dev"
})()

/**
 * @type {import("vite").PluginOption}
 */
export default [
  {
    name: "opencode-desktop:config",
    config() {
      return {
        resolve: {
          alias: {
            "@": fileURLToPath(new URL("./src", import.meta.url)),
            // Force every workspace package to share a single instance of these
            // reactive libraries. Two copies = two separate context registries =
            // hooks can't find their provider.
            "solid-js":         solidDir,
            "@solidjs/router":  solidRouterDir,
            "@solidjs/meta":    solidMetaDir,
          },
          // Belt-and-suspenders: also deduplicate by package name so Vite
          // doesn't create a second copy via its own resolution cache.
          dedupe: ["solid-js", "@solidjs/router", "@solidjs/meta", "solid-js/store", "solid-js/web"],
        },
        define: {
          "import.meta.env.VITE_OPENCODE_CHANNEL": JSON.stringify(channel),
        },
        worker: {
          format: "es",
        },
      }
    },
  },
  {
    name: "opencode-desktop:theme-preload",
    transformIndexHtml(html) {
      return html.replace(
        '<script id="oc-theme-preload-script" src="/oc-theme-preload.js"></script>',
        `<script id="oc-theme-preload-script">${readFileSync(theme, "utf8")}</script>`,
      )
    },
  },
  tailwindcss(),
  solidPlugin(),
]
