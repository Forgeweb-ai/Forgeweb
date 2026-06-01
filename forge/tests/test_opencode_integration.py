"""
tests/test_opencode_integration.py
==================================
E2E integration test script to verify Forge Backend's OpenCode chat integration.

It simulates a local development session for user 'ronakdarji' by:
  1. Creating a JWT token.
  2. Creating a test project.
  3. Sending a simple greeting chat ("Hello") and streaming the SSE responses.
  4. Sending a coding request ("Create a simple HTML page") and streaming the code generation.
  5. Verifying that the generated files are physically created in the user's workspace.
  6. Cleaning up the project and workspace.

Run with:
  cd forge
  python tests/test_opencode_integration.py
"""

import sys
import os
import json
import time
from pathlib import Path
import httpx

# Resolve path to include the forge package
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Color codes for terminal logging
CYAN = '\033[0;36m'
GREEN = '\033[0;32m'
YELLOW = '\033[1;33m'
RED = '\033[0;31m'
RESET = '\033[0m'
BOLD = '\033[1m'

def log_info(msg: str):
    print(f"{CYAN}[info]{RESET} {msg}")

def log_success(msg: str):
    print(f"{GREEN}[success]{RESET} {msg}")

def log_warn(msg: str):
    print(f"{YELLOW}[warning]{RESET} {msg}")

def log_error(msg: str):
    print(f"{RED}[error]{RESET} {msg}")

def test_runner():
    log_info("Starting E2E OpenCode Integration Test...")

    # 1. Resolve User and Create JWT Token
    user_id = "c32bb45a-07e7-4b4c-926a-727aeb46f205"
    email = "ronakdarji1997@gmail.com"

    try:
        from forge.auth import create_token
        token = create_token(user_id, email)
        log_success(f"Generated JWT token for user: {email} ({user_id[:8]})")
    except Exception as e:
        log_error(f"Failed to generate auth token: {e}")
        sys.exit(1)

    headers = {"Authorization": f"Bearer {token}"}
    client = httpx.Client(timeout=30.0)

    # 2. Create a temporary Test Project
    project_id = None
    try:
        log_info("Initializing a temporary project...")
        res = client.post(
            "http://localhost:8000/api/projects",
            json={
                "name": "E2E-OpenCode-Integration-Test-Project",
                "description": "A temporary project to test chat & code generation streams",
                "tech_stack": ["nextjs"],
                "setup_commands": [],
                "run_command": "",
                "files": []
            },
            headers=headers
        )
        res.raise_for_status()
        project_id = res.json()["id"]
        log_success(f"Project created with ID: {project_id}")
    except Exception as e:
        log_error(f"Failed to create project: {e}")
        sys.exit(1)

    # Resolve local workspace path on disk
    workspace_path = Path(__file__).parent.parent / "local-data" / "users" / user_id / "projects" / project_id / "workspace"

    try:
        # 3. Conversational Chat Test ("hello")
        log_info("Stage 1: Testing conversational chat ('hello')...")
        res = client.post(
            f"http://localhost:8000/api/projects/{project_id}/chat",
            json={"message": "hello"},
            headers=headers
        )
        res.raise_for_status()
        log_success("Conversational chat prompt sent successfully.")

        # Stream SSE events
        log_info("Subscribing to events stream to capture greeting response...")
        greeting_text = ""
        has_reasoning = False
        
        # Read the event stream line-by-line
        with httpx.stream(
            "GET",
            f"http://localhost:8000/api/projects/{project_id}/events",
            params={"token": token},
            timeout=httpx.Timeout(60.0, read=60.0)
        ) as r:
            event_name = ""
            for line in r.iter_lines():
                if line.startswith("event: "):
                    event_name = line[7:].strip()
                elif line.startswith("data: "):
                    data = json.loads(line[6:].strip())
                    if event_name == "message.part":
                        part_type = data.get("type")
                        if part_type == "text":
                            chunk = data.get("text", "")
                            greeting_text += chunk
                        elif part_type == "reasoning":
                            has_reasoning = True
                    elif event_name == "message.done":
                        log_success("Greeting stream finished successfully.")
                        break

        log_success(f"Received reasoning/thinking block: {has_reasoning}")
        log_success(f"Assistant Greeting Response:\n{BOLD}{greeting_text.strip()}{RESET}")
        assert len(greeting_text.strip()) > 0, "Expected a conversational response but got empty text"

        # 4. HTML Codegen Stream Test
        log_info("Stage 2: Testing HTML code generation stream...")
        prompt = (
            "Create a single simple file named 'index.html' that displays a welcomed header "
            "and a counter button styled nicely with basic CSS. No other files needed."
        )
        res = client.post(
            f"http://localhost:8000/api/projects/{project_id}/chat",
            json={"message": prompt},
            headers=headers
        )
        res.raise_for_status()
        log_success("HTML codegen prompt sent successfully.")

        log_info("Streaming code generation events (this may take up to 30s)...")
        file_touched = []
        is_done = False

        with httpx.stream(
            "GET",
            f"http://localhost:8000/api/projects/{project_id}/events",
            params={"token": token},
            timeout=httpx.Timeout(90.0, read=90.0)
        ) as r:
            event_name = ""
            for line in r.iter_lines():
                if line.startswith("event: "):
                    event_name = line[7:].strip()
                elif line.startswith("data: "):
                    data = json.loads(line[6:].strip())
                    if event_name == "message.part":
                        part_type = data.get("type")
                        if part_type == "tool":
                            tool = data.get("tool")
                            status = data.get("state", {}).get("status")
                            log_info(f"  → Agent Tool: {tool} ({status})")
                        elif part_type == "patch":
                            touched = data.get("files", [])
                            file_touched.extend(touched)
                            log_info(f"  → Files written: {touched}")
                    elif event_name == "message.done":
                        is_done = True
                        log_success("Codegen stream finished successfully.")
                        break

        assert is_done, "Stream did not fire complete done signal"
        log_success("HTML codegen pipeline processed without errors.")

        # 5. Verify Workspace File Updates on Disk
        log_info("Stage 3: Verifying workspace file updates on disk...")
        index_file = workspace_path / "index.html"
        
        # Verify folder existence
        assert workspace_path.exists(), "Workspace folder was not physically created on disk"
        log_success(f"Workspace directory verified: {workspace_path}")

        # Verify index.html existence and content
        assert index_file.exists(), "index.html was not written to the workspace directory"
        log_success("index.html successfully located in local workspace folder.")
        
        content = index_file.read_text(encoding="utf-8")
        log_success(f"index.html size: {len(content)} bytes")
        assert "Welcome" in content or "header" in content or "button" in content or "<html" in content, \
            "index.html content was empty or didn't contain generated HTML"
        
        log_success(f"index.html Content Preview:\n{BOLD}{content[:350]}...{RESET}")

    except Exception as e:
        log_error(f"Test failed with error: {e}")
        import traceback
        traceback.print_exc()
        cleanup_project(client, project_id, headers, workspace_path)
        sys.exit(1)

    # 6. Teardown and Cleanup
    cleanup_project(client, project_id, headers, workspace_path)
    log_success("E2E Integration Test passed with 100% success!")

def cleanup_project(client: httpx.Client, project_id: str, headers: dict, workspace_path: Path):
    if not project_id:
        return
    log_info(f"Cleaning up test project {project_id}...")
    try:
        # Call DELETE project endpoint
        res = client.delete(f"http://localhost:8000/api/projects/{project_id}", headers=headers)
        res.raise_for_status()
        log_success("Database project record deleted.")
        
        # Verify workspace path is deleted
        if workspace_path.parent.exists():
            log_warn("Workspace parent directory still exists, attempting local cleanup...")
            import shutil
            shutil.rmtree(workspace_path.parent, ignore_errors=True)
            log_success("Workspace files cleanly purged.")
        else:
            log_success("Local workspace directory verified to be cleanly deleted.")
    except Exception as e:
        log_warn(f"Failed to cleanup cleanly: {e}")

if __name__ == "__main__":
    test_runner()
