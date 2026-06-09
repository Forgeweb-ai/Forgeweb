/**
 * forge-source-stamp / visitor.js
 * ================================
 * Babel-AST visitor that stamps every intrinsic JSX opening element with a
 * `data-forge-source="relpath:line:col"` attribute. Read by the Forge UI's
 * visual-edit picker to map a clicked DOM node back to its source location.
 *
 * Why only intrinsic (lowercase) elements:
 *   <Button>...</Button> doesn't render DOM itself — its underlying <button>
 *   does. Stamping the wrapper produces a useless attribute on a fragment.
 *   Stamping the intrinsic guarantees the attribute lands on the DOM node
 *   the user actually clicks. Lovable/Bolt follow the same rule.
 *
 * Why we skip <html> / <head> / <body> / <script> / <style>:
 *   Adding unknown attributes to these triggers React hydration warnings
 *   and can confuse Next's streaming SSR. The agent never visual-edits
 *   these elements anyway.
 *
 * Cost shape (per CLAUDE.md §2):
 *   Runs at dev compile time only (loader gates on NODE_ENV). Per file:
 *   one full AST parse + traverse + generate. ~10–20ms for a typical
 *   component. Zero cost in production builds — attribute never ships.
 *   Per container: parse cost is amortised over file-cache lifetime; only
 *   changed files re-run, so HMR stays fast.
 *
 * Idempotency:
 *   If a `data-forge-source` attribute already exists on a node (e.g.
 *   author-written, or a re-transform), we leave it alone. No double-stamp.
 */

"use strict"

const parser    = require("@babel/parser")
const traverse  = require("@babel/traverse").default
const generator = require("@babel/generator").default
const t         = require("@babel/types")

// Intrinsics we never stamp — adding unknown attrs here is either invalid
// HTML or breaks React's hydration / SSR. Lowercase match only.
const SKIP_TAGS = new Set([
  "html", "head", "body", "title", "meta", "link",
  "script", "style", "noscript",
  // SVG group elements where unknown attrs are usually fine to add but
  // produce visual debugger noise on hover — skip for now.
])

const ATTR_NAME = "data-forge-source"

/**
 * Stamp every intrinsic JSX opening element in `code` with the source
 * attribute. `relpath` is the file path relative to the project root —
 * the loader computes it once per file and hands it in here.
 *
 * Returns either the transformed code (string) or the original code if
 * nothing changed / parse failed. NEVER throws — a bad transform must not
 * break the user's dev build.
 */
function stampSource(code, relpath) {
  // Quick reject: if there are no JSX tags, skip the full AST cost.
  // Cheap regex — false positives are fine (we just pay the parse), false
  // negatives (truly missed JSX) are not, so keep the test broad.
  if (!/<[A-Za-z]/.test(code)) return code

  let ast
  try {
    ast = parser.parse(code, {
      sourceType:    "module",
      allowReturnOutsideFunction: true,
      errorRecovery: true,
      plugins: [
        "jsx",
        "typescript",
        "decorators-legacy",
        "classProperties",
        "topLevelAwait",
      ],
    })
  } catch {
    // Parse failed — return original so the user's build still surfaces the
    // real syntax error from Next/SWC instead of our parser's variant.
    return code
  }

  let changed = false

  try {
    traverse(ast, {
      JSXOpeningElement(path) {
        const name = path.node.name
        // Only stamp intrinsic (lowercase) elements. JSXMemberExpression
        // (e.g. <Motion.div>) and JSXNamespacedName are user components.
        if (name.type !== "JSXIdentifier") return
        const tag = name.name
        if (!tag || tag[0] !== tag[0].toLowerCase()) return
        if (SKIP_TAGS.has(tag)) return

        // Idempotency — don't double-stamp.
        for (const attr of path.node.attributes) {
          if (
            attr.type === "JSXAttribute" &&
            attr.name &&
            attr.name.type === "JSXIdentifier" &&
            attr.name.name === ATTR_NAME
          ) {
            return
          }
        }

        const loc = path.node.loc
        if (!loc || !loc.start) return
        const value = `${relpath}:${loc.start.line}:${loc.start.column}`

        path.node.attributes.push(
          t.jsxAttribute(
            t.jsxIdentifier(ATTR_NAME),
            t.stringLiteral(value),
          ),
        )
        changed = true
      },
    })
  } catch {
    // Visitor exploded — fall back to original code rather than ship a
    // half-stamped file.
    return code
  }

  if (!changed) return code

  try {
    const out = generator(ast, {
      retainLines:    true,
      compact:        false,
      // Comments preserved — Next's source maps and the user's editor both
      // rely on line numbers; retainLines keeps everything aligned.
    }, code)
    return out.code
  } catch {
    return code
  }
}

module.exports = { stampSource, ATTR_NAME }
