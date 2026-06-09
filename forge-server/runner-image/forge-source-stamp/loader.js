/**
 * forge-source-stamp / loader.js
 * ================================
 * Webpack- and Turbopack-compatible loader. Wraps visitor.js.
 *
 * Wired from the generated app's next.config.ts (Forge-owned, see
 * forge-bootstrap.sh §3). Loader is referenced by absolute path
 * (/usr/local/lib/forge-source-stamp/loader.js) so the babel deps it needs
 * resolve from the runner image's global node_modules — zero per-project
 * install, zero per-project package.json pollution.
 *
 * Dev-only:
 *   The webpack hook in next.config.ts only adds this loader when
 *   `dev === true`. As a belt-and-suspenders check, we also no-op here
 *   when NODE_ENV === "production".
 *
 * Why we don't run on node_modules:
 *   Stamping vendored JSX (e.g. shadcn copies, mui internals) would
 *   point the user at code they didn't write. The webpack rule excludes
 *   node_modules; this loader trusts that exclusion. We also exclude
 *   anything under .next/ and .forge/ defensively.
 */

"use strict"

const path = require("path")
const { stampSource } = require("./visitor")

module.exports = function forgeSourceStampLoader(source) {
  // Synchronous loader — no callback, just return.
  if (process.env.NODE_ENV === "production") return source

  const resourcePath = this.resourcePath || ""
  // Defensive excludes — the rule should have handled these but a stray
  // file slipped through is worse than a missing stamp.
  if (
    resourcePath.includes(`${path.sep}node_modules${path.sep}`) ||
    resourcePath.includes(`${path.sep}.next${path.sep}`) ||
    resourcePath.includes(`${path.sep}.forge${path.sep}`)
  ) {
    return source
  }

  // Compute path relative to the project root. `rootContext` is set by
  // webpack to the compilation's context (= project root for Next). For
  // turbopack, fall back to `cwd()` which is the dev server's cwd.
  const root = this.rootContext || process.cwd()
  let relpath
  try {
    relpath = path.relative(root, resourcePath).split(path.sep).join("/")
  } catch {
    relpath = resourcePath
  }
  if (!relpath || relpath.startsWith("..")) {
    // Outside the project tree — don't stamp, the path would be useless
    // to the picker on click.
    return source
  }

  return stampSource(source, relpath)
}
