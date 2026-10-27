"""Minimal JSON-RPC 2.0 helpers shared by the MCP and A2A endpoints."""

from __future__ import annotations

from typing import Any

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def ok(id_: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def err(id_: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": id_, "error": error}


def validate_request(msg: Any) -> tuple[Any, str, dict[str, Any], bool]:
    """Return ``(id, method, params, is_notification)`` or raise ``RpcError``."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
        raise RpcError(INVALID_REQUEST, "Invalid JSON-RPC request")
    params = msg.get("params", {})
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise RpcError(INVALID_PARAMS, "params must be an object")
    is_notification = "id" not in msg
    id_ = msg.get("id")
    if not is_notification and not isinstance(id_, str | int):
        raise RpcError(INVALID_REQUEST, "id must be a string or integer")
    return id_, msg["method"], params, is_notification
