"""MCP 2026 parameter-to-header projection without header injection."""
import base64
from ..http import HEADER
from ..jsonrpc import ProtocolError


def header_value(value: str | int | bool) -> str:
    if type(value) is bool:
        value = "true" if value else "false"
    elif type(value) is int:
        if abs(value) > 2**53 - 1:
            raise ProtocolError("MCP header integer exceeds interoperable range")
        value = str(value)
    elif not isinstance(value, str):
        raise ProtocolError("MCP header values require strings, integers or booleans")
    safe = (value == value.strip() and all(32 <= ord(c) <= 126 for c in value)
            and not (value.startswith("=?base64?") and value.endswith("?=")))
    return value if safe else "=?base64?" + base64.b64encode(value.encode()).decode("ascii") + "?="


def projections(schema: dict) -> list[tuple[tuple[str, ...], str, str]]:
    result, names = [], set()
    def visit(node, path, reachable, depth):
        if depth > 40:
            raise ProtocolError("Tool schema nesting limit exceeded")
        if isinstance(node, dict):
            if "x-mcp-header" in node:
                name, kind = node["x-mcp-header"], node.get("type")
                if (not reachable or not path or not isinstance(name, str) or not HEADER.fullmatch(name)
                        or len(name) > 100 or name.lower() in names or kind not in ("string", "integer", "boolean")):
                    raise ProtocolError("Invalid x-mcp-header annotation")
                names.add(name.lower()); result.append((path, "Mcp-Param-" + name, kind))
            for key, child in node.items():
                if key == "properties" and isinstance(child, dict):
                    for property_name, sub in child.items():
                        visit(sub, (*path, property_name), reachable, depth + 1)
                elif key != "x-mcp-header":
                    visit(child, path, False, depth + 1)
        elif isinstance(node, list):
            for child in node:
                visit(child, path, False, depth + 1)
    visit(schema, (), True, 0)
    return result


def parameter_headers(schema: dict, arguments: dict) -> dict:
    result = {}
    for path, name, kind in projections(schema):
        current = arguments
        for step in path:
            if not isinstance(current, dict) or step not in current:
                break
            current = current[step]
        else:
            expected = {"string": str, "integer": int, "boolean": bool}[kind]
            if type(current) is not expected:
                raise ProtocolError("Tool header parameter does not match its declared type")
            result[name] = header_value(current)
    return result
