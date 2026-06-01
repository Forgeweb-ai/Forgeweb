/**
 * Forge shell policy — tight allow/deny rules applied to every shell command
 * issued by an agent running inside a Forge project container.
 *
 * Why this exists:
 *   A Forge agent must only see and touch its own project. It must NEVER be
 *   able to inspect the host, the docker daemon, other containers/projects,
 *   or other tenants' data. The container layer is the primary defense
 *   (mount NS, network NS, PID NS, no docker socket, non-root, dropped caps).
 *   This module is defense-in-depth: even if a container spec slips, the
 *   tool-layer deny-list catches the obvious escape commands before they
 *   ever spawn.
 *
 * Scope of enforcement:
 *   - Gated on `ForgeRuntimeState.isForgeProject()` so local opencode usage
 *     is unaffected.
 *   - Applied in `ShellTool.collect()` after tree-sitter parses the command
 *     into individual `command` nodes — so chains (`&&`, `||`, `;`, `|`)
 *     are each checked.
 *
 * Pure / synchronous on purpose — easy to unit test and zero Effect overhead.
 */

/**
 * Commands that reach beyond this container's namespace: docker daemon,
 * other containers, host init system, host network/mounts, or process
 * tracers that can escape sandboxing.
 *
 * Deliberately NOT denied (rely on namespace isolation instead):
 *   - ps / top / htop  → PID namespace scopes them to this container.
 *   - df / free / vmstat → cgroup-aware on modern kernels.
 *   - who / w / last → only this container's logins.
 *   - uname → harmless container info.
 *   - dig / nslookup / host / whois → DNS only, useful for the agent's own
 *     network probing against allowed egress.
 */
const HOST_INSPECTION = new Set<string>([
  "pm2",
  "docker",
  "docker-compose",
  "podman",
  "nerdctl",
  "kubectl",
  "helm",
  "systemctl",
  "service",
  "initctl",
  "rc-service",
  "journalctl",
  "dmesg",
  "lsof",
  "fuser",
  "netstat",
  "ss",
  "ip",
  "ifconfig",
  "route",
  "arp",
  "iptables",
  "nft",
  "ufw",
  "mount",
  "umount",
  "findmnt",
  "nmap",
  "nc",
  "ncat",
  "socat",
  "telnet",
  "traceroute",
  "tracepath",
  "mtr",
  "strace",
  "ltrace",
  "gdb",
  "lldb",
])

/** Privilege escalation and namespace-escape helpers. */
const PRIVILEGE_ESCALATION = new Set<string>([
  "sudo",
  "su",
  "doas",
  "pkexec",
  "chroot",
  "unshare",
  "nsenter",
  "setcap",
  "capsh",
])

/** Commands that mutate the system state of the container or host. */
const SYSTEM_MUTATION = new Set<string>([
  "reboot",
  "shutdown",
  "halt",
  "poweroff",
  "init",
  "telinit",
  "modprobe",
  "insmod",
  "rmmod",
  "sysctl",
  "crontab",
  "at",
  "batch",
])

/**
 * Dynamic-execution wrappers. These would otherwise let the agent smuggle a
 * denied command past tree-sitter (e.g. `bash -c "pm2 list"`,
 * `eval "$denied"`, `xargs sh -c ...`). Deny outright — the agent can issue
 * the same intent as a direct command, which then gets scanned normally.
 */
const SHELL_ESCAPE = new Set<string>(["eval"])

/**
 * Shells invoked with `-c <inline string>` (or PowerShell `-Command`) are an
 * escape hatch: the inline string is not parsed by our tree-sitter pass.
 */
const SHELL_EXEC_WRAPPERS = new Set<string>(["bash", "sh", "zsh", "ash", "dash", "ksh", "fish", "pwsh", "powershell"])
const SHELL_EXEC_FLAGS = new Set<string>(["-c", "-lc", "-ilc", "--command", "-command", "/c"])

/**
 * Absolute paths that leak host or container internals. Read or write to any
 * of these is denied regardless of who owns it.
 */
const SENSITIVE_PATH_PREFIXES = [
  "/proc",
  "/sys",
  "/var/run/docker.sock",
  "/run/docker.sock",
  "/var/lib/docker",
  "/var/lib/containerd",
  "/var/lib/kubelet",
  "/etc/shadow",
  "/etc/sudoers",
  "/etc/passwd",
  "/etc/group",
  "/root",
  "/host",
]

/** Home-relative paths likely to contain credentials. */
const SENSITIVE_HOME_SUFFIXES = [".ssh", ".aws", ".docker", ".kube", ".npmrc", ".pypirc", ".gnupg", ".netrc"]

export type PolicyViolationReason =
  | "host-inspection"
  | "privilege-escalation"
  | "system-mutation"
  | "shell-escape"
  | "sensitive-path"

export type PolicyViolation = {
  reason: PolicyViolationReason
  detail: string
}

/**
 * Inspect the tokens of a single parsed command. Returns a violation if the
 * command is denied; otherwise `undefined`.
 *
 * `tokens[0]` is the command name; later tokens are args/flags. Flag matching
 * is case-insensitive so PowerShell `-Command` and Unix `-c` both trip.
 */
export function checkCommand(tokens: ReadonlyArray<string>): PolicyViolation | undefined {
  if (tokens.length === 0) return
  const head = tokens[0]
  if (!head) return
  const cmd = head.toLowerCase()
  // Strip a leading path so e.g. `/usr/bin/pm2` is still recognized as `pm2`.
  const base = cmd.split(/[\\/]/).pop() ?? cmd

  if (HOST_INSPECTION.has(base))
    return {
      reason: "host-inspection",
      detail: `'${base}' is not allowed: it inspects the host, the docker daemon, or other containers. You may only operate inside this project's container — its own files, its own processes, its own ports. Use the file tools (Read/Grep/Glob) for project files, and project-scoped commands (npm/pnpm/git/etc.) for project work.`,
    }

  if (PRIVILEGE_ESCALATION.has(base))
    return {
      reason: "privilege-escalation",
      detail: `'${base}' is not allowed: privilege escalation and namespace changes are blocked.`,
    }

  if (SYSTEM_MUTATION.has(base))
    return {
      reason: "system-mutation",
      detail: `'${base}' is not allowed: system-level mutation or scheduling is blocked.`,
    }

  if (SHELL_ESCAPE.has(base))
    return {
      reason: "shell-escape",
      detail: `'${base}' is not allowed: dynamic command execution would bypass the scope checks. Issue the command directly instead.`,
    }

  if (SHELL_EXEC_WRAPPERS.has(base)) {
    for (let i = 1; i < tokens.length; i++) {
      const flag = tokens[i]?.toLowerCase()
      if (flag && SHELL_EXEC_FLAGS.has(flag))
        return {
          reason: "shell-escape",
          detail: `'${base} ${flag} "<inline>"' is not allowed: inline shell strings bypass the parser-level scope checks. Issue the commands directly (one per shell call) instead.`,
        }
    }
  }

  return
}

/**
 * Check a resolved absolute path against the sensitive-path list. Caller is
 * responsible for resolving the path first (the shell tool already does this
 * via `argPath`/`resolvePath`).
 */
export function checkPath(absolutePath: string, homeDir: string | undefined): PolicyViolation | undefined {
  if (!absolutePath) return
  const normalized = absolutePath.replace(/\\/g, "/")

  for (const prefix of SENSITIVE_PATH_PREFIXES) {
    if (normalized === prefix || normalized.startsWith(prefix + "/"))
      return {
        reason: "sensitive-path",
        detail: `Access to '${prefix}' is not allowed: this path exposes host or container internals.`,
      }
  }

  if (homeDir) {
    const home = homeDir.replace(/\\/g, "/")
    if (normalized.startsWith(home + "/")) {
      const rel = normalized.slice(home.length + 1)
      const first = rel.split("/")[0]
      if (first && SENSITIVE_HOME_SUFFIXES.includes(first))
        return {
          reason: "sensitive-path",
          detail: `Access to '~/${first}' is not allowed: this path may contain credentials.`,
        }
    }
  }

  return
}

export const ShellPolicy = {
  checkCommand,
  checkPath,
} as const
