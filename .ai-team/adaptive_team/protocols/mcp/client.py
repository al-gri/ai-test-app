"""Version-pinned MCP capability discovery and guarded tool invocation.

One client per authenticated worker/principal. The owner grants tool names;
discovery and server annotations cannot grant permission. No implicit downgrade,
automatic tool retry, paid sampling, command execution or arbitrary URL fetching.
"""
from __future__ import annotations

import asyncio
from typing import Callable

from ... import __version__
from ...models import PolicyError
from ..jsonrpc import ProtocolError, HTTPStatusError, RemoteError, RpcClient, decode, encode, text
from .headers import header_value, parameter_headers, projections

MODERN = "2026-07-28"
LEGACY = "2025-11-25"


class InputRequired(ProtocolError):
    def __init__(self, result):
        self.result = result
        super().__init__("MCP server requires input; no automatic continuation is authorized")


class ToolExecutionError(ProtocolError):
    def __init__(self, result):
        self.result = result
        super().__init__("MCP tool reported an execution error")


class MCPClient:
    def __init__(self, rpc: RpcClient, *, allowed_tools: frozenset[str], version=MODERN,
                 authorize: Callable[[str, dict], None] | None = None,
                 argument_validator: Callable[[dict, dict], None] | None = None, guard=None):
        if version not in (MODERN, LEGACY):
            raise PolicyError("Unsupported MCP profile; select an implemented version explicitly")
        if not isinstance(allowed_tools, frozenset) or any(not isinstance(x, str) or not x for x in allowed_tools):
            raise PolicyError("Explicit immutable tool allowlist required")
        self.rpc, self.version, self.allowed_tools = rpc, version, allowed_tools
        self.authorize, self.argument_validator = authorize, argument_validator
        self.guard = guard
        self._lock = asyncio.Lock()
        self._capabilities, self._session = None, None
        self.rejected_tools: dict[str, str] = {}

    def _request(self, method, params):
        params = dict(params)
        headers = {"MCP-Protocol-Version": self.version}
        if self.version == MODERN:
            params["_meta"] = {"io.modelcontextprotocol/protocolVersion": self.version,
                "io.modelcontextprotocol/clientInfo": {"name": "adaptive-ai-team", "version": __version__},
                "io.modelcontextprotocol/clientCapabilities": {}}
            headers["Mcp-Method"] = method
        elif self._session:
            headers["Mcp-Session-Id"] = self._session
        return params, headers

    def _complete(self, result):
        if self.version == MODERN:
            if result.get("resultType") == "input_required":
                raise InputRequired(result)
            if result.get("resultType") != "complete":
                raise ProtocolError("Unsupported MCP result type")
        return result

    async def _rpc(self, method, params, extra_headers=None):
        params, headers = self._request(method, params)
        try:
            wire = await self.rpc.request(method, params, headers={**headers, **(extra_headers or {})})
        except (HTTPStatusError, RemoteError) as exc:
            if self.version == LEGACY and getattr(exc, "status", None) == 404:
                # Invalidate only. Never repeat the failed tool call. A later
                # explicit operation reconnects without the expired session ID.
                self._session, self._capabilities = None, None
            raise
        session = wire.headers.get("mcp-session-id")
        if self.version == LEGACY and session:
            if (len(session) > 256 or any(not 33 <= ord(c) <= 126 for c in session)
                    or (self._session and self._session != session)):
                raise ProtocolError("Invalid or unexpectedly rotated MCP session")
            if method == "initialize":
                self._session = session
        return self._complete(wire.message["result"])

    async def connect(self) -> dict:
        async with self._lock:
            if self._capabilities is not None:
                return dict(self._capabilities)
            if self.version == MODERN:
                result = await self._rpc("server/discover", {})
                versions = result.get("supportedVersions")
                if not isinstance(versions, list) or self.version not in versions:
                    raise ProtocolError("Server does not advertise the configured MCP version")
            else:
                result = await self._rpc("initialize", {"protocolVersion": self.version, "capabilities": {},
                    "clientInfo": {"name": "adaptive-ai-team", "version": __version__}})
                if result.get("protocolVersion") != self.version:
                    raise ProtocolError("Server selected an unsupported MCP version")
                _, headers = self._request("notifications/initialized", {})
                await self.rpc.notify("notifications/initialized", headers=headers)
            capabilities = result.get("capabilities")
            if not isinstance(capabilities, dict):
                raise ProtocolError("MCP capabilities must be an object")
            if "tools" in capabilities and not isinstance(capabilities["tools"], dict):
                raise ProtocolError("Malformed tools capability")
            self._capabilities = capabilities
            return dict(capabilities)

    async def list_tools(self) -> list[dict]:
        capabilities = await self.connect()
        if "tools" not in capabilities:
            raise ProtocolError("Server did not declare tools support")
        tools, seen, cursors, cursor, total = [], set(), set(), None, 0
        self.rejected_tools = {}
        # Refresh on each invocation instead of treating a stale schema as an
        # authorization grant. No subscription/cross-principal cache is assumed.
        for _ in range(100):
            result = await self._rpc("tools/list", {} if cursor is None else {"cursor": cursor})
            batch = result.get("tools")
            if not isinstance(batch, list):
                raise ProtocolError("Malformed tools/list response")
            total += len(encode(result))
            if total > 4 * 1024 * 1024 or len(seen) + len(batch) > 10000:
                raise ProtocolError("Tool discovery admission limit exceeded")
            for tool in batch:
                if not isinstance(tool, dict):
                    raise ProtocolError("Malformed tool descriptor")
                name = text(tool.get("name"), "tool name", limit=200)
                if name in seen:
                    raise ProtocolError("Duplicate tool name across discovery pages")
                seen.add(name)
                schema = tool.get("inputSchema")
                if not isinstance(schema, dict) or schema.get("type") != "object":
                    raise ProtocolError("Tool inputSchema must describe an object")
                if self.version == MODERN:
                    try:
                        projections(schema)
                    except ProtocolError:
                        self.rejected_tools[name] = "invalid_header_annotation"
                        continue
                if name in self.allowed_tools:
                    tools.append(tool)
            cursor = result.get("nextCursor")
            if cursor is None:
                return tools
            text(cursor, "pagination cursor")
            if cursor in cursors:
                raise ProtocolError("Cyclic tool-discovery cursor")
            cursors.add(cursor)
        raise ProtocolError("Tool discovery page limit exceeded")

    async def call_tool(self, name: str, arguments: dict) -> dict:
        if self.guard is not None:
            if name not in self.allowed_tools:
                raise PolicyError("Tool is not granted to this worker")
            return await self.guard.call(self.rpc.transport.endpoint, name, lambda: self._call_tool(name, arguments))
        return await self._call_tool(name, arguments)

    async def _call_tool(self, name: str, arguments: dict) -> dict:
        if name not in self.allowed_tools:
            raise PolicyError("Tool is not granted to this worker")
        if not isinstance(arguments, dict) or len(encode(arguments)) > 1024 * 1024:
            raise PolicyError("Tool arguments must be a bounded JSON object")
        arguments = decode(encode(arguments))  # Freeze the request across discovery awaits.
        # Discovery cannot replace a task-specific scope/write/budget check.
        if self.authorize:
            self.authorize(name, arguments)
        tool = next((value for value in await self.list_tools() if value["name"] == name), None)
        if tool is None:
            raise PolicyError("Granted tool is unavailable or its descriptor was rejected")
        if self.argument_validator:
            self.argument_validator(tool["inputSchema"], arguments)
        headers = {}
        if self.version == MODERN:
            headers = {"Mcp-Name": header_value(name), **parameter_headers(tool["inputSchema"], arguments)}
        result = await self._rpc("tools/call", {"name": name, "arguments": arguments}, headers)
        if (not isinstance(result.get("content"), list)
                or type(result.get("isError", False)) is not bool):
            raise ProtocolError("Malformed MCP tool result")
        for block in result["content"]:
            if not isinstance(block, dict) or not isinstance(block.get("type"), str):
                raise ProtocolError("Malformed MCP content block")
            if block["type"] == "text" and not isinstance(block.get("text"), str):
                raise ProtocolError("Malformed MCP text block")
        if result.get("isError"):
            raise ToolExecutionError(result)
        # Remote text, resource links and structuredContent remain untrusted data.
        # In particular, no embedded URI is fetched and no content is executed.
        return result
