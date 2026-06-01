"""
tests/test_preview_routing.py
==============================
Verifies that the production-ready preview routing system works correctly:

  1. Two different project IDs get different (non-colliding) ports.
  2. The port registry persists across calls (idempotent allocation).
  3. The workspace assign_ports() is consistent with the port registry.
  4. SandboxRunner per-project state is isolated: stop(A) never affects B.
  5. The proxy port resolver returns the correct port per project.
  6. No hardcoded 5174 fallback anywhere in the allocation chain.

Run with:
  cd forge
  python tests/test_preview_routing.py        # standalone
  python -m pytest tests/test_preview_routing.py -v   # via pytest
"""

import sys
import os
import asyncio
import shutil

# Make the forge package importable from the repo root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _cleanup(port_registry, workspace_manager, *project_ids):
    """Release ports and destroy workspaces for test project IDs."""
    for pid in project_ids:
        try:
            port_registry.release(pid)
        except Exception:
            pass
        try:
            ws = workspace_manager.path(pid)
            if ws.exists():
                shutil.rmtree(ws, ignore_errors=True)
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# 1. Port registry: two projects → two distinct ports
# ─────────────────────────────────────────────────────────────────────────────

def test_port_registry_isolation():
    """Two different project IDs must receive different non-overlapping port blocks."""
    from forge.runner.port_registry import port_registry
    from forge.runner.workspace import workspace_manager

    proj_a = "test-isolation-alpha-001"
    proj_b = "test-isolation-beta-002"
    _cleanup(port_registry, workspace_manager, proj_a, proj_b)

    ports_a = port_registry.allocate(proj_a)
    ports_b = port_registry.allocate(proj_b)

    assert ports_a["fe"] > 0, f"Project A fe_port must be > 0, got {ports_a['fe']}"
    assert ports_b["fe"] > 0, f"Project B fe_port must be > 0, got {ports_b['fe']}"

    # Blocks must not share any port (each block is 5 ports wide)
    set_a = set(ports_a.values())
    set_b = set(ports_b.values())
    collision = set_a & set_b
    assert not collision, f"Port collision between projects: {collision}"

    print(f"  ✓ Project A ports: {ports_a}")
    print(f"  ✓ Project B ports: {ports_b}")
    print(f"  ✓ No port collision between A and B")

    _cleanup(port_registry, workspace_manager, proj_a, proj_b)


# ─────────────────────────────────────────────────────────────────────────────
# 2. Port registry: allocation is idempotent
# ─────────────────────────────────────────────────────────────────────────────

def test_port_registry_idempotent():
    """Calling allocate() twice for the same project returns the same ports."""
    from forge.runner.port_registry import port_registry
    from forge.runner.workspace import workspace_manager

    proj = "test-idempotent-003"
    _cleanup(port_registry, workspace_manager, proj)

    ports_first  = port_registry.allocate(proj)
    ports_second = port_registry.allocate(proj)

    assert ports_first == ports_second, (
        f"allocate() not idempotent: first={ports_first} second={ports_second}"
    )
    print(f"  ✓ Idempotent allocation: {ports_first}")

    _cleanup(port_registry, workspace_manager, proj)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Workspace assign_ports: consistent with registry
# ─────────────────────────────────────────────────────────────────────────────

def test_workspace_ports_match_registry():
    """
    workspace_manager.assign_ports() must return the same fe_port as
    port_registry.allocate() for the same project_id.
    """
    from forge.runner.port_registry import port_registry
    from forge.runner.workspace import workspace_manager

    proj = "test-workspace-004"
    _cleanup(port_registry, workspace_manager, proj)

    # Let assign_ports allocate fresh (workspace + registry both clean)
    ws_ports = workspace_manager.assign_ports(proj)

    # Registry must now also have an entry
    reg_ports = port_registry.get(proj)
    assert reg_ports is not None, "Registry has no allocation after assign_ports()"
    assert ws_ports["fe"] == reg_ports["fe"], (
        f"Workspace fe_port {ws_ports['fe']} != registry fe_port {reg_ports['fe']}"
    )
    print(f"  ✓ Workspace and registry agree: fe_port={ws_ports['fe']}")

    _cleanup(port_registry, workspace_manager, proj)


# ─────────────────────────────────────────────────────────────────────────────
# 4. SandboxRunner: per-project state isolation
# ─────────────────────────────────────────────────────────────────────────────

async def _test_sandbox_isolation_async():
    """
    Verify SandboxRunner tracks processes per project.
    stop(proj_a) must NOT affect proj_b's state entry.
    """
    from forge.runner.sandbox import SandboxRunner, _ProjectState

    runner = SandboxRunner()   # fresh instance — no singleton side-effects

    proj_a = "sandbox-test-A"
    proj_b = "sandbox-test-B"

    # Initially nothing is running
    assert not runner.is_project_running(proj_a), "A should not be running initially"
    assert not runner.is_project_running(proj_b), "B should not be running initially"

    # Inject fake state entries (no real processes — just state dict entries)
    runner._states[proj_a] = _ProjectState(proc=None, tmpdir="/tmp/fake_a")
    runner._states[proj_b] = _ProjectState(proc=None, tmpdir="/tmp/fake_b")

    # Stopping project A must not remove project B's state
    await runner.stop(proj_a)

    assert proj_a not in runner._states, "A's state should be removed after stop(A)"
    assert proj_b in runner._states,     "B's state must survive stopping A"
    print(f"  ✓ stop(proj_a) did not affect proj_b's state")

    # Cleanup
    await runner.stop(proj_b)
    assert proj_b not in runner._states, "B's state should be removed after stop(B)"
    print(f"  ✓ Subsequent stop(proj_b) cleaned up correctly")


def test_sandbox_isolation():
    asyncio.run(_test_sandbox_isolation_async())


# ─────────────────────────────────────────────────────────────────────────────
# 5. Proxy port resolution: correct port per project (no FastAPI needed)
# ─────────────────────────────────────────────────────────────────────────────

def test_proxy_port_resolution():
    """
    The proxy's port lookup (via workspace/registry fallback) must return
    different, non-zero ports for two distinct project IDs.

    This test exercises the registry+workspace path directly (mimicking what
    _resolve_fe_port() does when no process is running) so we don't need
    FastAPI or httpx to be importable in the test environment.
    """
    from forge.runner.port_registry import port_registry
    from forge.runner.workspace import workspace_manager

    proj_a = "proxy-test-A-005"
    proj_b = "proxy-test-B-006"
    _cleanup(port_registry, workspace_manager, proj_a, proj_b)

    # Allocate ports (mimics what happens when a project workspace is created)
    ports_a = workspace_manager.assign_ports(proj_a)
    ports_b = workspace_manager.assign_ports(proj_b)

    fe_a = ports_a["fe"]
    fe_b = ports_b["fe"]

    assert fe_a > 0,      f"Project A fe_port must be > 0, got {fe_a}"
    assert fe_b > 0,      f"Project B fe_port must be > 0, got {fe_b}"
    assert fe_a != fe_b,  f"Both projects got the same fe_port: {fe_a}"

    print(f"  ✓ Proxy would route project A → port {fe_a}")
    print(f"  ✓ Proxy would route project B → port {fe_b}")
    print(f"  ✓ Different ports: {fe_a} ≠ {fe_b}")

    _cleanup(port_registry, workspace_manager, proj_a, proj_b)


# ─────────────────────────────────────────────────────────────────────────────
# 6. No hardcoded 5174 fallback
# ─────────────────────────────────────────────────────────────────────────────

def test_no_5174_fallback():
    """
    The port registry must never allocate port 5174 (the old hard-coded default).
    All fe_ports must be within the registry's legitimate range [PORT_MIN, PORT_MAX].
    """
    from forge.runner.port_registry import port_registry, PORT_MIN, PORT_MAX
    from forge.runner.workspace import workspace_manager

    proj = "test-no-5174-007"
    _cleanup(port_registry, workspace_manager, proj)

    ports   = port_registry.allocate(proj)
    fe_port = ports["fe"]

    assert fe_port != 5174, "Allocated fe_port must NOT be the old hardcoded 5174"
    assert PORT_MIN <= fe_port <= PORT_MAX, (
        f"Allocated fe_port {fe_port} outside range [{PORT_MIN}, {PORT_MAX}]"
    )
    print(f"  ✓ fe_port={fe_port} (not 5174, within [{PORT_MIN}, {PORT_MAX}])")

    _cleanup(port_registry, workspace_manager, proj)


# ─────────────────────────────────────────────────────────────────────────────
# 7. Ten distinct projects → ten distinct fe_ports
# ─────────────────────────────────────────────────────────────────────────────

def test_many_projects_no_collision():
    """
    Simulate 10 concurrent projects and verify every one gets a unique fe_port.
    This stress-tests the registry's block-allocation scan.
    """
    from forge.runner.port_registry import port_registry
    from forge.runner.workspace import workspace_manager

    project_ids = [f"stress-test-project-{i:03d}" for i in range(10)]
    _cleanup(port_registry, workspace_manager, *project_ids)

    fe_ports = []
    for pid in project_ids:
        ports = port_registry.allocate(pid)
        fe_ports.append(ports["fe"])

    # All fe_ports must be unique
    assert len(fe_ports) == len(set(fe_ports)), (
        f"fe_port collision among {len(project_ids)} projects: {fe_ports}"
    )
    print(f"  ✓ 10 projects allocated: fe_ports = {fe_ports}")

    _cleanup(port_registry, workspace_manager, *project_ids)


# ─────────────────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [
        ("Port registry: two projects → different ports",      test_port_registry_isolation),
        ("Port registry: allocation is idempotent",            test_port_registry_idempotent),
        ("Workspace assign_ports consistent with registry",    test_workspace_ports_match_registry),
        ("SandboxRunner: per-project state isolation",         test_sandbox_isolation),
        ("Proxy port resolution: correct port per project",    test_proxy_port_resolution),
        ("No hardcoded 5174 fallback",                         test_no_5174_fallback),
        ("10 concurrent projects: no port collision",          test_many_projects_no_collision),
    ]

    passed = 0
    failed = 0
    for name, fn in tests:
        print(f"\n▶ {name}")
        try:
            fn()
            passed += 1
        except Exception as e:
            print(f"  ✗ FAILED: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print(f"\n{'='*60}")
    if failed:
        print(f"Results: {passed}/{passed+failed} passed  ({failed} FAILED)")
        sys.exit(1)
    else:
        print(f"Results: {passed}/{passed+failed} passed  ✓ All tests passed!")
        sys.exit(0)
