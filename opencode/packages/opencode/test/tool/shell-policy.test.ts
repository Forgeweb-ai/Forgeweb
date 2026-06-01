import { describe, expect, test } from "bun:test"
import { ShellPolicy, checkCommand, checkPath } from "../../src/tool/shell/policy"
import { FORGE_SCOPE_PROMPT } from "../../src/forge/scope-prompt"

describe("ShellPolicy.checkCommand", () => {
  describe("denies host / cross-container inspection", () => {
    const samples = [
      ["pm2"],
      ["pm2", "list"],
      ["docker", "ps"],
      ["docker-compose", "logs"],
      ["podman", "ps"],
      ["kubectl", "get", "pods"],
      ["helm", "list"],
      ["systemctl", "status", "nginx"],
      ["journalctl", "-u", "ssh"],
      ["dmesg"],
      ["lsof", "-iTCP"],
      ["netstat", "-tlnp"],
      ["ss", "-tlnp"],
      ["ip", "addr"],
      ["ifconfig"],
      ["mount"],
      ["findmnt"],
      ["nmap", "127.0.0.1"],
      ["nc", "-l", "8080"],
      ["socat", "-", "TCP:host:80"],
      ["strace", "-p", "1"],
      ["gdb", "-p", "1"],
    ]
    test.each(samples)("rejects: %j", (...tokens) => {
      const v = checkCommand(tokens)
      expect(v?.reason).toBe("host-inspection")
    })
  })

  test("recognizes denied commands even with an absolute path", () => {
    expect(checkCommand(["/usr/bin/pm2", "list"])?.reason).toBe("host-inspection")
    expect(checkCommand(["/usr/local/bin/docker", "ps"])?.reason).toBe("host-inspection")
  })

  describe("denies privilege escalation", () => {
    const samples = [
      ["sudo", "ls"],
      ["su", "-"],
      ["doas", "ls"],
      ["pkexec", "ls"],
      ["chroot", "/host"],
      ["unshare", "-r"],
      ["nsenter", "-t", "1", "-m"],
      ["setcap", "cap_sys_admin+ep", "/bin/foo"],
    ]
    test.each(samples)("rejects: %j", (...tokens) => {
      expect(checkCommand(tokens)?.reason).toBe("privilege-escalation")
    })
  })

  describe("denies system mutation and persistence", () => {
    const samples = [
      ["reboot"],
      ["shutdown", "-h", "now"],
      ["halt"],
      ["init", "0"],
      ["modprobe", "kvm"],
      ["sysctl", "-a"],
      ["crontab", "-e"],
      ["at", "now"],
    ]
    test.each(samples)("rejects: %j", (...tokens) => {
      expect(checkCommand(tokens)?.reason).toBe("system-mutation")
    })
  })

  describe("denies shell-escape patterns", () => {
    test("eval", () => {
      expect(checkCommand(["eval", "pm2 list"])?.reason).toBe("shell-escape")
    })
    test("bash -c", () => {
      expect(checkCommand(["bash", "-c", "pm2 list"])?.reason).toBe("shell-escape")
    })
    test("sh -lc", () => {
      expect(checkCommand(["sh", "-lc", "lsof"])?.reason).toBe("shell-escape")
    })
    test("zsh -c", () => {
      expect(checkCommand(["zsh", "-c", "docker ps"])?.reason).toBe("shell-escape")
    })
    test("pwsh -Command (any case)", () => {
      expect(checkCommand(["pwsh", "-Command", "Get-Process"])?.reason).toBe("shell-escape")
    })
    test("plain bash is fine (only -c form is the escape hatch)", () => {
      expect(checkCommand(["bash", "script.sh"])).toBeUndefined()
    })
  })

  describe("allows expected project commands", () => {
    const samples = [
      ["npm", "install"],
      ["pnpm", "run", "build"],
      ["yarn", "dev"],
      ["bun", "run", "test"],
      ["git", "status"],
      ["git", "log"],
      ["node", "script.js"],
      ["python", "manage.py", "migrate"],
      ["tsc", "--noEmit"],
      ["ls", "-la"],
      ["cat", "package.json"],
      ["ps"], // PID namespace scopes this to container procs
      ["df", "-h"],
      ["free", "-m"],
      ["uname", "-a"],
      ["dig", "example.com"],
      ["nslookup", "example.com"],
    ]
    test.each(samples)("allows: %j", (...tokens) => {
      expect(checkCommand(tokens)).toBeUndefined()
    })
  })

  test("empty input is a no-op", () => {
    expect(checkCommand([])).toBeUndefined()
  })
})

describe("ShellPolicy.checkPath", () => {
  test("blocks /proc, /sys, docker socket, host bind-mount, secret files", () => {
    const sensitive = [
      "/proc/1/environ",
      "/sys/class/net",
      "/var/run/docker.sock",
      "/run/docker.sock",
      "/var/lib/docker/containers/abc",
      "/etc/shadow",
      "/etc/sudoers",
      "/etc/passwd",
      "/root/.bashrc",
      "/host/var/log/syslog",
    ]
    for (const p of sensitive) {
      expect(checkPath(p, "/home/agent")?.reason).toBe("sensitive-path")
    }
  })

  test("blocks credential dirs under home", () => {
    const home = "/home/agent"
    const sensitive = [
      `${home}/.ssh/id_rsa`,
      `${home}/.aws/credentials`,
      `${home}/.docker/config.json`,
      `${home}/.kube/config`,
      `${home}/.npmrc`,
      `${home}/.netrc`,
      `${home}/.gnupg/secring.gpg`,
    ]
    for (const p of sensitive) {
      expect(checkPath(p, home)?.reason).toBe("sensitive-path")
    }
  })

  test("does not block ordinary project paths", () => {
    expect(checkPath("/workspace/forge-data/proj-1/src/index.ts", "/home/agent")).toBeUndefined()
    expect(checkPath("/home/agent/project/foo.txt", "/home/agent")).toBeUndefined()
  })

  test("handles undefined home gracefully", () => {
    expect(checkPath("/workspace/foo", undefined)).toBeUndefined()
    expect(checkPath("/etc/shadow", undefined)?.reason).toBe("sensitive-path")
  })
})

describe("ShellPolicy default export", () => {
  test("exposes the same functions", () => {
    expect(ShellPolicy.checkCommand).toBe(checkCommand)
    expect(ShellPolicy.checkPath).toBe(checkPath)
  })
})

describe("scope-prompt and policy stay in sync", () => {
  // If a command is rejected by the tool layer, the model must be told about
  // it in the scope prompt. Otherwise the model burns BYOK tokens hitting
  // refusals it could have avoided. This test enforces the alignment.
  const denyListedCommands = [
    "pm2",
    "docker",
    "docker-compose",
    "podman",
    "kubectl",
    "helm",
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
    "sudo",
    "su",
    "chroot",
    "nsenter",
    "reboot",
    "modprobe",
    "sysctl",
    "crontab",
    "eval",
  ]
  test.each(denyListedCommands)("scope prompt warns the model about %s", (cmd) => {
    expect(FORGE_SCOPE_PROMPT).toContain(cmd)
  })
})
