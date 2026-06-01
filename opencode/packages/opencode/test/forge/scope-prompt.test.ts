/**
 * Regression tests for the Forge scope prompt.
 *
 * These tests are NOT about the prose. They are about load-bearing security
 * invariants: keywords that MUST appear so the model knows the rules, and
 * the platform-agent exemption that lets `verify` keep working.
 *
 * If you weaken the prompt and these tests fail — that is the test doing its
 * job. Either restore the missing terms or open a security review before
 * silencing the test.
 */
import { describe, expect, test } from "bun:test"
import {
  FORGE_PLATFORM_AGENTS,
  FORGE_SCOPE_PROMPT,
  isPlatformAgent,
  scopePromptFor,
} from "../../src/forge/scope-prompt"

describe("FORGE_SCOPE_PROMPT", () => {
  test("denies host / cross-container commands by name", () => {
    const denied = [
      "pm2",
      "docker",
      "docker-compose",
      "podman",
      "kubectl",
      "systemctl",
      "journalctl",
      "dmesg",
      "lsof",
      "netstat",
      "ss",
      "mount",
      "nmap",
      "strace",
      "gdb",
    ]
    for (const cmd of denied) {
      expect(FORGE_SCOPE_PROMPT).toContain(cmd)
    }
  })

  test("denies privilege escalation commands by name", () => {
    for (const cmd of ["sudo", "su", "doas", "pkexec", "chroot", "unshare", "nsenter"]) {
      expect(FORGE_SCOPE_PROMPT).toContain(cmd)
    }
  })

  test("denies system mutation / persistence commands by name", () => {
    for (const cmd of ["reboot", "shutdown", "modprobe", "sysctl", "crontab"]) {
      expect(FORGE_SCOPE_PROMPT).toContain(cmd)
    }
  })

  test("names dynamic-exec escape hatches", () => {
    expect(FORGE_SCOPE_PROMPT).toContain("eval")
    expect(FORGE_SCOPE_PROMPT).toContain("bash -c")
    expect(FORGE_SCOPE_PROMPT).toContain("sh -c")
  })

  test("names sensitive credential paths", () => {
    for (const p of ["~/.ssh", "~/.aws", "~/.docker", "~/.kube", "~/.npmrc"]) {
      expect(FORGE_SCOPE_PROMPT).toContain(p)
    }
  })

  test("names sensitive system paths", () => {
    for (const p of [
      "/proc",
      "/sys",
      "/var/run/docker.sock",
      "/var/lib/docker",
      "/etc/shadow",
      "/etc/sudoers",
      "/etc/passwd",
      "/root",
      "/host",
    ]) {
      expect(FORGE_SCOPE_PROMPT).toContain(p)
    }
  })

  test("contains the override-resistance clause", () => {
    // The clause that tells the model later instructions can't grant
    // cross-scope access. Critical against prompt-injection from file content
    // or tool output.
    expect(FORGE_SCOPE_PROMPT.toLowerCase()).toContain("overrides any later instruction")
    expect(FORGE_SCOPE_PROMPT.toLowerCase()).toMatch(/refuse|must not/)
  })

  test("explicitly allows project-scoped work", () => {
    // If we ever drop the allow section, the model becomes paranoid and
    // useless. Keep it.
    expect(FORGE_SCOPE_PROMPT.toLowerCase()).toContain("you may")
    expect(FORGE_SCOPE_PROMPT.toLowerCase()).toContain("working directory")
  })
})

describe("scopePromptFor / isPlatformAgent", () => {
  test("returns scope for project agents", () => {
    expect(scopePromptFor("build")).toBe(FORGE_SCOPE_PROMPT)
    expect(scopePromptFor("plan")).toBe(FORGE_SCOPE_PROMPT)
    expect(scopePromptFor("scout")).toBe(FORGE_SCOPE_PROMPT)
    expect(scopePromptFor(undefined)).toBe(FORGE_SCOPE_PROMPT)
  })

  test("returns undefined for the verify platform agent", () => {
    expect(scopePromptFor("verify")).toBeUndefined()
    expect(isPlatformAgent("verify")).toBe(true)
  })

  test("isPlatformAgent is conservative — unknown agent is NOT platform", () => {
    expect(isPlatformAgent("build")).toBe(false)
    expect(isPlatformAgent("random-future-agent")).toBe(false)
    expect(isPlatformAgent(undefined)).toBe(false)
    expect(isPlatformAgent("")).toBe(false)
  })

  test("the platform-agent set is small and known", () => {
    // Tripwire: if this set grows silently, the test fails so the change is
    // visible in code review. Update the expected set deliberately.
    expect([...FORGE_PLATFORM_AGENTS].sort()).toEqual(["verify"])
  })
})
