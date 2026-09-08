# region MODULE_CONTRACT [DOMAIN(9): MCPControl; CONCEPT(9): LocalObservability, DryRun; TECH(8): JSONRPC]
## @file mcp_server.py
## @brief Minimal stdio MCP control surface for the autonomous runtime.
## @modulecontract
## @purpose Let Codex inspect status and trigger bounded cycles without handling browser credentials or per-action confirmations.
## @scope JSON-RPC initialize, tools/list and tools/call over stdio.
## @input MCP requests.
## @output JSON-only status, reports and redacted errors.
## @invariants Dry-run is the MCP default; enabling autonomy remains a local CLI-only operation.
## @changes LAST_CHANGE: [v0.2.0 — Updated tools for browser bridge autonomous worker.]
## @modulemap
## FUNC 9[Dispatches safe MCP tools] => handle_message
## FUNC 8[Runs stdio server] => main
def _module_contract() -> None:
    pass
# endregion MODULE_CONTRACT
# GREP_SUMMARY: MCP, stdio, status, autonomous cycle, dry-run, audit, redacted error
# STRUCTURE: JSON-RPC line -> allowlisted tool -> service -> JSON result; secrets never cross stdio

import json
import sys
from typing import Any

from .app import create_service


TOOLS = [
    {"name": "status", "description": "Показать безопасный локальный статус bridge, автономии, лимитов и hard stop.", "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {
        "name": "run_autonomous_cycle",
        "description": "Запустить один цикл. По умолчанию dry-run; live разрешён только после локального opt-in.",
        "inputSchema": {"type": "object", "properties": {"dry_run": {"type": "boolean", "default": True}}, "additionalProperties": False},
    },
    {
        "name": "recent_audit",
        "description": "Показать обезличенные события аудита без session credentials.",
        "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20}}, "additionalProperties": False},
    },
]


def _result(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _dispatch(service: Any, name: str, args: dict[str, Any]) -> Any:
    if name == "status":
        return service.status()
    if name == "run_autonomous_cycle":
        return service.run_cycle(dry_run=bool(args.get("dry_run", True)))
    if name == "recent_audit":
        return service.recent_audit(limit=int(args.get("limit", 20)))
    raise ValueError("Неизвестный инструмент")


# region FUNC_handle_message [DOMAIN(9): MCPControl; CONCEPT(9): AllowlistedDispatch; TECH(8): JSONRPC]
## @purpose Process the supported MCP subset and return redacted tool failures.
## @io service, JSON object -> JSON object|None
## @complexity 7
def handle_message(service: Any, message: dict[str, Any]) -> dict[str, Any] | None:
    request_id, method, params = message.get("id"), message.get("method"), message.get("params") or {}
    if method == "initialize":
        return _result(request_id, {
            "protocolVersion": params.get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "amir-job-finder", "version": "0.2.0"},
            "instructions": "ИИ используется только для релевантности и grounded-текстов. Browser bridge исполняет закрытый список HTTP-команд. MCP dry-run по умолчанию; opt-in включается только локально через CLI.",
        })
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return _result(request_id, {})
    if method == "tools/list":
        return _result(request_id, {"tools": TOOLS})
    if method == "tools/call":
        try:
            args = params.get("arguments") or {}
            if not isinstance(args, dict):
                raise ValueError("Некорректные arguments")
            value = _dispatch(service, str(params.get("name") or ""), args)
            text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
            return _result(request_id, {"content": [{"type": "text", "text": text}], "isError": False})
        except Exception as exc:
            print(f"[IMP:8][MCP][TOOL_ERROR] {exc.__class__.__name__}", file=sys.stderr, flush=True)
            text = json.dumps({"error": "Локальная операция не выполнена", "type": exc.__class__.__name__}, ensure_ascii=False)
            return _result(request_id, {"content": [{"type": "text", "text": text}], "isError": True})
    if request_id is None:
        return None
    return _error(request_id, -32601, "Метод не поддерживается")
# endregion FUNC_handle_message


def main() -> None:
    try:
        service = create_service()
    except Exception as exc:
        print(f"[IMP:10][MCP][STARTUP_ERROR] {exc.__class__.__name__}", file=sys.stderr, flush=True)
        raise SystemExit(1) from exc
    try:
        for raw_line in sys.stdin.buffer:
            if not raw_line.strip():
                continue
            try:
                message = json.loads(raw_line)
                response = handle_message(service, message)
            except Exception:
                response = _error(None, -32700, "Некорректный JSON-RPC запрос")
            if response is not None:
                sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
                sys.stdout.flush()
    except KeyboardInterrupt:
        pass
    finally:
        service.stop_bridge()


if __name__ == "__main__":
    main()
