/**
 * settings-profile.tsx
 * =====================
 * Settings tab for editing the user's profile (full_name + theme_pref).
 *
 * The full /me payload is fetched once when the tab mounts (same endpoint
 * the home page already uses, so it's typically warm in HTTP cache). We
 * patch via PATCH /api/auth/me — narrow on purpose:
 *   - email/username/password rotation belong behind re-auth, not here
 *   - role/company_size are onboarding-only
 *
 * Sized for the lone settings dialog tab, not the whole window.
 */
import { type Component, createResource, createSignal, Show } from "solid-js"
import { TextField } from "@opencode-ai/ui/text-field"
import { Button } from "@opencode-ai/ui/button"
import { showToast } from "@opencode-ai/ui/toast"
import { fetchCurrentUser, updateCurrentUser, userInitials, type CurrentUser } from "@/context/forge-api"
import { SettingsList } from "./settings-list"

export const SettingsProfile: Component = () => {
  const [user, { refetch }] = createResource<CurrentUser | null>(() => fetchCurrentUser())

  const [fullName, setFullName] = createSignal("")
  const [saving,   setSaving]   = createSignal(false)
  const [dirty,    setDirty]    = createSignal(false)

  // Hydrate the input when the user resource resolves. Only run once per
  // load — don't clobber user edits on background refetches.
  let hydrated = false
  function maybeHydrate(u: CurrentUser | null | undefined) {
    if (hydrated || !u) return
    setFullName(u.full_name ?? "")
    hydrated = true
  }

  async function save() {
    const next = fullName().trim()
    if (!next) {
      showToast({ title: "Name cannot be empty", variant: "error" })
      return
    }
    setSaving(true)
    const result = await updateCurrentUser({ full_name: next })
    setSaving(false)
    if ("error" in result) {
      showToast({ title: "Could not save profile", description: result.error, variant: "error" })
      return
    }
    setDirty(false)
    showToast({ title: "Profile updated", variant: "success" })
    void refetch()
  }

  return (
    <div class="flex flex-col gap-6 p-6 max-w-[640px]">
      <div>
        <h2 class="text-18-semibold text-text-strong mb-1">Profile</h2>
        <p class="text-13-regular text-text-weak">
          Update how you appear across Forge.
        </p>
      </div>

      <Show
        when={user.state === "ready"}
        fallback={
          <div class="text-13-regular text-text-weak">Loading…</div>
        }
      >
        {(() => {
          const u = user()
          maybeHydrate(u)
          return (
            <>
              {/* Avatar preview + email (read-only) */}
              <div class="flex items-center gap-4">
                <div
                  class="size-14 rounded-full flex items-center justify-center text-white font-bold text-[18px] select-none shrink-0"
                  style={{
                    background: "linear-gradient(135deg, oklch(0.78 0.15 55), oklch(0.58 0.21 18))",
                  }}
                >
                  {userInitials(fullName() || u?.full_name, u?.username)}
                </div>
                <div class="flex flex-col min-w-0">
                  <div class="text-14-medium text-text-strong truncate">
                    {fullName() || u?.full_name || u?.username || "—"}
                  </div>
                  <div class="text-12-regular text-text-weak truncate">{u?.email}</div>
                </div>
              </div>

              <SettingsList>
                <div class="flex flex-col gap-2 py-4 border-b border-border-weak-base last:border-none">
                  <label class="text-14-medium text-text-strong">Full name</label>
                  <TextField
                    value={fullName()}
                    onChange={(value) => {
                      setFullName(value)
                      setDirty(true)
                    }}
                    placeholder="Your name"
                    size="large"
                  />
                  <span class="text-12-regular text-text-weak">
                    Shown on your project cards and in conversation headers.
                  </span>
                </div>

                <div class="flex flex-col gap-2 py-4 border-b border-border-weak-base last:border-none">
                  <label class="text-14-medium text-text-strong">Email</label>
                  <TextField value={u?.email ?? ""} disabled size="large" />
                  <span class="text-12-regular text-text-weak">
                    Email changes aren't supported yet — contact support if you need this.
                  </span>
                </div>

                <div class="flex flex-col gap-2 py-4 border-b border-border-weak-base last:border-none">
                  <label class="text-14-medium text-text-strong">Username</label>
                  <TextField value={u?.username ?? ""} disabled size="large" />
                </div>
              </SettingsList>

              <div class="flex justify-end gap-2">
                <Button
                  variant="ghost"
                  disabled={!dirty() || saving()}
                  onClick={() => {
                    setFullName(u?.full_name ?? "")
                    setDirty(false)
                  }}
                >
                  Reset
                </Button>
                <Button
                  variant="primary"
                  disabled={!dirty() || saving()}
                  onClick={() => void save()}
                >
                  {saving() ? "Saving…" : "Save changes"}
                </Button>
              </div>
            </>
          )
        })()}
      </Show>
    </div>
  )
}
