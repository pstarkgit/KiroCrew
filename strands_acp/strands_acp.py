"""
strands-acp: Strands Harness as a Kiro Crew ACP backend.

Implements ACP protocol v1 over stdio (newline-delimited JSON-RPC), backed by
Strands Harness. Connects to Crew's MCP tools via KIROCREW_STRANDS_MCP_SERVERS
(same format as KIROCREW_PI_MCP_SERVERS).

System prompt: ~415 tokens (HARNESS_CONTRACT only + per-session instructions).
Typical Claude Code harness: 30,000-50,000 tokens.

Install: pip install strands-harness strands-agents-tools
Run (Crew sets this up): strands-acp
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from typing import Any

from strands import Agent
from strands.models.anthropic import AnthropicModel
from strands.tools.mcp import MCPClient, MCPServerConfig
from strands_harness.prompt import HARNESS_CONTRACT

PROTOCOL_VERSION = 1
AGENT_NAME = "strands"
AGENT_VERSION = "0.1.0"
SERVERS_ENV = "KIROCREW_STRANDS_MCP_SERVERS"


def log(*parts: Any) -> None:
    print("[strands-acp]", *parts, file=sys.stderr, flush=True)


def write_msg(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def ok(req_id: Any, result: dict) -> None:
    write_msg({"jsonrpc": "2.0", "id": req_id, "result": result})


def err(req_id: Any, code: int, message: str) -> None:
    write_msg({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})


def notify(method: str, params: dict) -> None:
    write_msg({"jsonrpc": "2.0", "method": method, "params": params})


def build_mcp_tools(servers_env: str) -> tuple[list, list[MCPClient]]:
    """Parse KIROCREW_STRANDS_MCP_SERVERS and return (tools, clients)."""
    raw = os.environ.get(servers_env, "[]")
    try:
        specs = json.loads(raw)
    except json.JSONDecodeError as e:
        log(f"Bad {servers_env}: {e}")
        return [], []

    clients: list[MCPClient] = []
    for spec in specs:
        if spec.get("disabled"):
            continue
        name = spec.get("name", "unnamed")
        cmd = spec.get("command")
        if not cmd:
            log(f"Skipping {name}: no command")
            continue
        env_overrides = spec.get("env", {})
        if isinstance(env_overrides, list):
            env_overrides = {e["name"]: e["value"] for e in env_overrides}
        merged_env = {**os.environ, **env_overrides}
        cfg: MCPServerConfig = {
            "command": cmd,
            "args": spec.get("args", []),
            "env": merged_env,
        }
        clients.append(MCPClient({name: cfg}))
        log(f"Registered: {name} ({cmd})")

    all_tools = []
    for c in clients:
        all_tools.extend(c.list_tools_sync())
    log(f"Tools: {[t.tool_name for t in all_tools]}")
    return all_tools, clients


# ---------------------------------------------------------------------------
# Session store
# ---------------------------------------------------------------------------

class Session:
    def __init__(self, session_id: str, tools: list, model_id: str) -> None:
        self.session_id = session_id
        self.model = AnthropicModel(model_id=model_id, max_tokens=8192)
        self.agent = Agent(
            model=self.model,
            system_prompt=HARNESS_CONTRACT,
            tools=tools,
        )
        self.messages: list[dict] = []


_sessions: dict[str, Session] = {}
_tools: list = []
_clients: list[MCPClient] = []


def _model_id() -> str:
    return os.environ.get("STRANDS_MODEL", "claude-sonnet-4-6")


def _config_options() -> list[dict]:
    return [
        {
            "id": "model",
            "name": "Model",
            "type": "select",
            "currentValue": _model_id(),
            "options": [
                {"value": "claude-sonnet-4-6", "name": "Claude Sonnet 4.6"},
                {"value": "claude-opus-4-8", "name": "Claude Opus 4.8"},
                {"value": "claude-haiku-4-5-20251001", "name": "Claude Haiku 4.5"},
            ],
        }
    ]


def _modes() -> dict:
    return {
        "currentModeId": "auto",
        "availableModes": [
            {"id": "auto", "name": "auto", "description": "Automatically approve tool calls"},
            {"id": "approve", "name": "approve", "description": "Ask before every tool call"},
        ],
    }


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def handle_initialize(req_id: Any, _params: dict) -> None:
    ok(req_id, {
        "protocolVersion": PROTOCOL_VERSION,
        "agentCapabilities": {
            "loadSession": False,
            "promptCapabilities": {"image": False, "audio": False, "embeddedContext": False},
            "mcpCapabilities": {"http": False, "sse": False},
            "sessionCapabilities": {"list": {}, "delete": {}, "close": {}},
            "auth": {},
        },
        "agentInfo": {"name": AGENT_NAME, "version": AGENT_VERSION},
    })


def handle_session_new(req_id: Any, params: dict) -> None:
    session_id = str(uuid.uuid4())
    session = Session(session_id, _tools, _model_id())
    _sessions[session_id] = session
    log(f"New session: {session_id}")
    ok(req_id, {
        "sessionId": session_id,
        "modes": _modes(),
        "configOptions": _config_options(),
    })


def handle_session_load(req_id: Any, params: dict) -> None:
    sid = params.get("sessionId", "")
    if sid not in _sessions:
        err(req_id, -32002, f"Resource not found: {sid}")
    else:
        ok(req_id, {"sessionId": sid, "modes": _modes(), "configOptions": _config_options()})


def handle_session_list(req_id: Any, _params: dict) -> None:
    ok(req_id, {"sessions": [{"sessionId": s} for s in _sessions]})


def handle_session_close(req_id: Any, params: dict) -> None:
    sid = params.get("sessionId", "")
    _sessions.pop(sid, None)
    ok(req_id, {})


def handle_session_prompt(req_id: Any, params: dict) -> None:
    sid = params.get("sessionId", "")
    session = _sessions.get(sid)
    if session is None:
        err(req_id, -32002, f"Session not found: {sid}")
        return

    # Extract text from the prompt
    messages = params.get("messages", [])
    user_text = ""
    for m in messages:
        for part in m.get("content", []):
            if part.get("type") == "text":
                user_text += part["text"]

    # Streaming callback: emit agent_message_chunk notifications
    def on_event(event: Any) -> None:
        if hasattr(event, "data") and hasattr(event.data, "text"):
            notify("session/update", {
                "sessionId": sid,
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": event.data.text},
                },
            })

    try:
        result = session.agent(user_text, event_handler=on_event)
        usage = getattr(result, "usage", None)
        usage_payload = {}
        if usage:
            usage_payload = {
                "totalTokens": getattr(usage, "total_tokens", 0),
                "inputTokens": getattr(usage, "input_tokens", 0),
                "outputTokens": getattr(usage, "output_tokens", 0),
            }
        ok(req_id, {"stopReason": "end_turn", "usage": usage_payload})
    except Exception as e:
        log(f"Agent error: {e}")
        err(req_id, -32603, str(e))


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

HANDLERS = {
    "initialize": handle_initialize,
    "session/new": handle_session_new,
    "session/load": handle_session_load,
    "session/list": handle_session_list,
    "session/close": handle_session_close,
    "session/prompt": handle_session_prompt,
}


def main() -> None:
    global _tools, _clients
    _tools, _clients = build_mcp_tools(SERVERS_ENV)
    log(f"strands-acp ready (model={_model_id()}, tools={len(_tools)})")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            log(f"Bad JSON: {e}")
            continue

        method = msg.get("method", "")
        req_id = msg.get("id")
        params = msg.get("params", {})

        handler = HANDLERS.get(method)
        if handler is None:
            if req_id is not None:
                err(req_id, -32601, f"Method not found: {method}")
            continue

        try:
            handler(req_id, params)
        except Exception as e:
            log(f"Handler error ({method}): {e}")
            if req_id is not None:
                err(req_id, -32603, str(e))


if __name__ == "__main__":
    main()
