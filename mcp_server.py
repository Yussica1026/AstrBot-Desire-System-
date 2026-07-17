from __future__ import annotations

import json
import os
import sys
from typing import Any

from desire.integration import DesireEngine

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.environ.get("DESIRE_DB_FILE") or os.path.join(BASE_DIR, "desire_system.db")


class DesireMCPServer:
    def __init__(self):
        self.engine = DesireEngine(DB_FILE)

    def tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "desire_status",
                "description": "View the current nine-dimensional desire drive state, thoughts, tick count, and baselines.",
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            },
            {
                "name": "desire_event",
                "description": "Trigger a desire system event such as wife_message, task_done, fight, reconcile, rest, or happy_moment.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "event_type": {
                            "type": "string",
                            "description": "Event type to apply.",
                        }
                    },
                    "required": ["event_type"],
                },
            },
            {
                "name": "desire_tick",
                "description": "Run one desire system heartbeat and return changes, action hints, next interval, and monologue.",
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            },
            {
                "name": "desire_resolve_thought",
                "description": "Resolve a thought from the thought pool. Reflection thoughts add a small joy bonus when resolved.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "thought_text": {
                            "type": "string",
                            "description": "Full thought text or a keyword contained in the thought.",
                        }
                    },
                    "required": ["thought_text"],
                },
            },
        ]

    def call_tool(self, name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
        arguments = arguments or {}
        if name == "desire_status":
            result = self.engine.summary()
        elif name == "desire_event":
            result = self.engine.trigger_event(str(arguments.get("event_type", "")))
        elif name == "desire_tick":
            result = self.engine.tick()
        elif name == "desire_resolve_thought":
            result = self.engine.resolve(str(arguments.get("thought_text", "")))
        else:
            raise ValueError(f"Unknown tool: {name}")
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(result, ensure_ascii=False, indent=2),
                }
            ]
        }

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        method = message.get("method")
        request_id = message.get("id")
        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "astrbot-desire-system", "version": "2.0.0"},
                },
            }
        if method == "notifications/initialized":
            return None
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": self.tools()}}
        if method == "tools/call":
            params = message.get("params") or {}
            try:
                result = self.call_tool(str(params.get("name", "")), params.get("arguments") or {})
                return {"jsonrpc": "2.0", "id": request_id, "result": result}
            except Exception as exc:
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32000, "message": str(exc)},
                }
        if request_id is None:
            return None
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }


def main() -> None:
    server = DesireMCPServer()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
            response = server.handle(message)
        except Exception as exc:
            response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": str(exc)},
            }
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
