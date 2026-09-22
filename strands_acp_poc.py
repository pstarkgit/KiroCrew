"""
Proof-of-concept: Strands Harness as a Crew ACP backend.

This script demonstrates that Strands can consume the same KIROCREW_PI_MCP_SERVERS
env var the Pi bridge uses, connect to Crew's stdio MCP tools, and run with the
lean 415-token HARNESS_CONTRACT in place of the heavy Claude Code / Crew system
prompt.

Usage:
    ANTHROPIC_API_KEY=<key> KIROCREW_PI_MCP_SERVERS='<json>' python strands_acp_poc.py "your task"

Where KIROCREW_PI_MCP_SERVERS is the same JSON array Crew injects for Pi:
    [{"name": "kirocrew-core", "command": "kirocrew", "args": ["mcp"]}]

Token savings vs Claude Code harness: ~72x on system prompt alone.
"""

from __future__ import annotations

import json
import os
import sys

from strands import Agent
from strands.models.anthropic import AnthropicModel
from strands.tools.mcp import MCPClient, MCPServerConfig
from strands_harness.prompt import HARNESS_CONTRACT


def build_mcp_clients() -> list[MCPClient]:
    """Parse KIROCREW_PI_MCP_SERVERS and return connected MCPClient instances."""
    raw = os.environ.get("KIROCREW_PI_MCP_SERVERS", "[]")
    try:
        specs = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"[strands-acp] Bad KIROCREW_PI_MCP_SERVERS: {e}", file=sys.stderr)
        return []

    clients = []
    for spec in specs:
        if spec.get("disabled"):
            continue
        name = spec.get("name", "unnamed")
        cmd = spec.get("command")
        if not cmd:
            print(f"[strands-acp] Skipping {name}: no command", file=sys.stderr)
            continue

        env_overrides = spec.get("env", {})
        if isinstance(env_overrides, list):
            env_overrides = {e["name"]: e["value"] for e in env_overrides}
        merged_env = {**os.environ, **env_overrides}

        server_config: MCPServerConfig = {
            "command": cmd,
            "args": spec.get("args", []),
            "env": merged_env,
        }
        client = MCPClient({name: server_config})
        clients.append(client)
        print(f"[strands-acp] Registered MCP server: {name} ({cmd})", file=sys.stderr)

    return clients


def run(task: str) -> None:
    model = AnthropicModel(
        model_id=os.environ.get("STRANDS_MODEL", "claude-sonnet-4-6"),
        max_tokens=8192,
    )

    clients = build_mcp_clients()

    # Gather tools from all MCP clients
    all_tools = []
    context_managers = []
    for client in clients:
        context_managers.append(client)
        all_tools.extend(client.list_tools_sync())

    print(f"[strands-acp] MCP tools loaded: {[t.tool_name for t in all_tools]}", file=sys.stderr)

    agent = Agent(
        model=model,
        system_prompt=HARNESS_CONTRACT,
        tools=all_tools,
    )

    print(f"\n{'='*60}", file=sys.stderr)
    print(f"System prompt tokens (approx): ~{len(HARNESS_CONTRACT) // 4}", file=sys.stderr)
    print(f"{'='*60}\n", file=sys.stderr)

    result = agent(task)

    # Report usage
    usage = getattr(result, "usage", None)
    if usage:
        print(f"\n[strands-acp] Token usage:", file=sys.stderr)
        print(f"  Input:  {getattr(usage, 'input_tokens', '?')}", file=sys.stderr)
        print(f"  Output: {getattr(usage, 'output_tokens', '?')}", file=sys.stderr)

    print(result.message)


if __name__ == "__main__":
    task = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "What tools do you have available?"
    run(task)
