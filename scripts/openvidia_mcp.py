#!/usr/bin/env python3
"""Standard MCP (Model Context Protocol) JSON-RPC stdio server for OpenVidia."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

OPENVIDIA_BASE = os.environ.get("OPENVIDIA_BASE_URL", "http://127.0.0.1:1919")

TOOLS = [
    {
        "name": "openvidia_chat",
        "description": "Send a prompt to NVIDIA NIM models via OpenVidia proxy (Nemotron, Mistral, Kimi, Step 5, etc.) and receive the completion.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "The user prompt or instruction to send to the model.",
                },
                "model": {
                    "type": "string",
                    "description": "Optional model ID (e.g. 'openvidia' for active model, 'nvidia/nemotron-3-ultra-550b-a55b', 'mistralai/mistral-large-4-1t-a52b'). Defaults to 'openvidia'.",
                    "default": "openvidia",
                },
                "system": {
                    "type": "string",
                    "description": "Optional system prompt.",
                },
                "max_tokens": {
                    "type": "integer",
                    "description": "Max tokens to generate (default: 2048).",
                    "default": 2048,
                },
            },
            "required": ["prompt"],
            "additionalProperties": False,
        },
    },
    {
        "name": "openvidia_list_models",
        "description": "List all active, tested, and available NVIDIA models exposed by OpenVidia.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "openvidia_status",
        "description": "Get live OpenVidia proxy status, healthy keys count, and currently active model.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
]


def send(data: dict) -> None:
    sys.stdout.write(json.dumps(data) + "\n")
    sys.stdout.flush()


def call_chat(
    prompt: str, model: str = "openvidia", system: str | None = None, max_tokens: int = 2048
) -> str:
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": model or "openvidia",
        "messages": messages,
        "max_tokens": max_tokens,
        "stream": False,
    }
    req = urllib.request.Request(
        f"{OPENVIDIA_BASE}/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")
        return f"OpenVidia HTTP {e.code} error: {err}"
    except Exception as e:
        return f"OpenVidia request error: {e}"


def list_models() -> str:
    req = urllib.request.Request(f"{OPENVIDIA_BASE}/v1/models")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            models = [m.get("id") for m in data.get("data", [])]
            return json.dumps(models, indent=2)
    except Exception as e:
        return f"Could not list models: {e}"


def get_status() -> str:
    req = urllib.request.Request(f"{OPENVIDIA_BASE}/health")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return json.dumps(data, indent=2)
    except Exception as e:
        return f"Could not fetch status: {e}"


def handle_request(msg: dict) -> None:
    msg_id = msg.get("id")
    method = msg.get("method")
    base = {"jsonrpc": "2.0", "id": msg_id}

    if method == "initialize":
        send(
            {
                **base,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "openvidia", "version": "1.0.0"},
                },
            }
        )
        return

    if method == "ping":
        send({**base, "result": {}})
        return

    if method == "tools/list":
        send({**base, "result": {"tools": TOOLS}})
        return

    if method == "tools/call":
        params = msg.get("params", {})
        tool_name = params.get("name")
        args = params.get("arguments", {})

        if tool_name == "openvidia_chat":
            prompt = args.get("prompt", "")
            model = args.get("model", "openvidia")
            system = args.get("system")
            max_tokens = args.get("max_tokens", 2048)
            ans = call_chat(prompt, model, system, max_tokens)
            send({**base, "result": {"content": [{"type": "text", "text": ans}]}})
            return

        if tool_name == "openvidia_list_models":
            ans = list_models()
            send({**base, "result": {"content": [{"type": "text", "text": ans}]}})
            return

        if tool_name == "openvidia_status":
            ans = get_status()
            send({**base, "result": {"content": [{"type": "text", "text": ans}]}})
            return

        send({**base, "error": {"code": -32601, "message": f"Tool '{tool_name}' not found"}})
        return

    send({**base, "error": {"code": -32601, "message": f"Method '{method}' not found"}})


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
            handle_request(msg)
        except Exception as e:
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(e)}})


if __name__ == "__main__":
    main()
