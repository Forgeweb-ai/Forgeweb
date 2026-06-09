;(function () {
  // Runs in <head> before React mounts. localStorage keys are Forge-branded
  // (`forge-theme-*`); pre-rename keys (`opencode-theme-*`) are migrated
  // forward on first read so returning users keep their picked theme.
  //
  // The migration helper is intentionally tiny: get-or-fallback, no exception
  // surface that could block the first paint, idempotent on every run.
  function readMigrate(next, legacy) {
    var v = localStorage.getItem(next)
    if (v !== null) return v
    var old = localStorage.getItem(legacy)
    if (old === null) return null
    try {
      localStorage.setItem(next, old)
      localStorage.removeItem(legacy)
    } catch (e) {}
    return old
  }

  var THEME_ID_KEY     = "forge-theme-id"
  var SCHEME_KEY       = "forge-color-scheme"
  var CSS_LIGHT_KEY    = "forge-theme-css-light"
  var CSS_DARK_KEY     = "forge-theme-css-dark"

  var themeId = readMigrate(THEME_ID_KEY, "opencode-theme-id") || "oc-2"

  if (themeId === "oc-1") {
    themeId = "oc-2"
    localStorage.setItem(THEME_ID_KEY, themeId)
    localStorage.removeItem(CSS_LIGHT_KEY)
    localStorage.removeItem(CSS_DARK_KEY)
  }

  var scheme = readMigrate(SCHEME_KEY, "opencode-color-scheme") || "system"
  var isDark = scheme === "dark" || (scheme === "system" && matchMedia("(prefers-color-scheme: dark)").matches)
  var mode = isDark ? "dark" : "light"

  document.documentElement.dataset.theme = themeId
  document.documentElement.dataset.colorScheme = mode

  // Update theme-color meta tag to match app color scheme
  var metas = document.querySelectorAll("meta[name='theme-color']")
  if (metas.length > 0) metas[0].setAttribute("content", isDark ? "#131010" : "#F8F7F7")

  if (themeId === "oc-2") return

  var cssKey       = mode === "dark" ? CSS_DARK_KEY        : CSS_LIGHT_KEY
  var legacyCssKey = mode === "dark" ? "opencode-theme-css-dark" : "opencode-theme-css-light"
  var css = readMigrate(cssKey, legacyCssKey)
  if (css) {
    var style = document.createElement("style")
    style.id = "oc-theme-preload"
    style.textContent =
      ":root{color-scheme:" +
      mode +
      ";--text-mix-blend-mode:" +
      (isDark ? "plus-lighter" : "multiply") +
      ";" +
      css +
      "}"
    document.head.appendChild(style)
  }
})()
