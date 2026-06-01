"""
forge/forge/agent/
==================
AI Agent Engine — gives the LLM a real terminal + file system.

Exports:
  run_agent(project_id, task, stack, on_event) → AsyncIterator[AgentEvent]
"""

from forge.agent.loop import run_agent

__all__ = ["run_agent"]
