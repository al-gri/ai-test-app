"""MCP Streamable HTTP client profiles; no implicit tool authorization."""
from .client import MCPClient, MODERN, LEGACY, InputRequired, ToolExecutionError

__all__ = ["MCPClient", "MODERN", "LEGACY", "InputRequired", "ToolExecutionError"]
