"""Strict, immutable A2A 1.0 Agent Cards. A parsed card is not a trusted identity."""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256

from ..http import endpoint_url
from ..jsonrpc import ProtocolError, decode, encode, text


def strings(value, name, *, allow_empty=False) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 1000 or (not allow_empty and not value):
        raise ProtocolError(f"Invalid {name} list")
    result = tuple(text(item, name, limit=200) for item in value)
    if len(set(result)) != len(result):
        raise ProtocolError(f"Duplicate {name}")
    return result


def validate_security(requirements, schemes):
    if not isinstance(requirements, list) or len(requirements) > 100:
        raise ProtocolError("Malformed A2A securityRequirements")
    for alternative in requirements:
        if not isinstance(alternative, dict) or set(alternative) - {"schemes"}:
            raise ProtocolError("A2A 1.0 security requirement must contain schemes")
        required = alternative.get("schemes", {})
        if not isinstance(required, dict) or set(required) - set(schemes):
            raise ProtocolError("Security requirement references an undeclared scheme")
        for scopes in required.values():
            if not isinstance(scopes, dict) or set(scopes) - {"list"}:
                raise ProtocolError("Security scopes require the A2A StringList wrapper")
            strings(scopes.get("list", []), "security scopes", allow_empty=True)


def security_satisfied(requirements: list, grants: dict[str, frozenset[str]]) -> bool:
    # Alternatives are OR; schemes inside one alternative are AND. Grants are
    # configured by the credential owner, never inferred from a card's text.
    return not requirements or any(all(name in grants and set(scopes.get("list", [])) <= grants[name]
        for name, scopes in alternative.get("schemes", {}).items()) for alternative in requirements)


@dataclass(frozen=True)
class AgentCard:
    document: bytes

    @classmethod
    def parse(cls, source: bytes | str | dict, *, allow_loopback_http=False) -> AgentCard:
        payload = encode(source) if isinstance(source, dict) else source.encode() if isinstance(source, str) else source
        data = decode(payload, limit=512 * 1024)
        # Optional application metadata, not a claim about the A2A standard or
        # permission. The owner's durable registry assigns effective generations.
        if 'generation' in data and (type(data['generation']) is not int or data['generation']<1):
            raise ProtocolError('Agent Card generation must be a positive integer')
        for name in ("name", "description", "version"):
            text(data.get(name), name)
        interfaces = data.get("supportedInterfaces")
        if not isinstance(interfaces, list) or not 1 <= len(interfaces) <= 16:
            raise ProtocolError("A2A 1.0 supportedInterfaces are required; legacy cards are not silently upgraded")
        seen = set()
        for interface in interfaces:
            if not isinstance(interface, dict):
                raise ProtocolError("Malformed AgentInterface")
            endpoint_url(interface.get("url"), allow_loopback_http=allow_loopback_http)
            for field in ("protocolBinding", "protocolVersion"):
                text(interface.get(field), field, limit=200)
            if "tenant" in interface:
                text(interface["tenant"], "tenant", limit=200)
            identity = (interface["url"], interface["protocolBinding"], interface["protocolVersion"], interface.get("tenant"))
            if identity in seen:
                raise ProtocolError("Duplicate AgentInterface")
            seen.add(identity)
        capabilities = data.get("capabilities")
        if not isinstance(capabilities, dict):
            raise ProtocolError("Agent capabilities must be an object")
        for flag in ("streaming", "pushNotifications", "extendedAgentCard"):
            if flag in capabilities and type(capabilities[flag]) is not bool:
                raise ProtocolError("Capability flag must be boolean")
        extensions = capabilities.get("extensions", [])
        if not isinstance(extensions, list):
            raise ProtocolError("Malformed capability extensions")
        for extension in extensions:
            if not isinstance(extension, dict) or type(extension.get("required", False)) is not bool:
                raise ProtocolError("Malformed extension declaration")
            text(extension.get("uri"), "extension URI")
            if extension.get("required"):
                raise ProtocolError("Required A2A extensions are unsupported by this client profile")
        for name in ("defaultInputModes", "defaultOutputModes"):
            strings(data.get(name), name)
        schemes = data.get("securitySchemes", {})
        if not isinstance(schemes, dict):
            raise ProtocolError("Malformed securitySchemes")
        allowed = {"httpAuthSecurityScheme", "apiKeySecurityScheme", "oauth2SecurityScheme",
                   "openIdConnectSecurityScheme", "mtlsSecurityScheme"}
        for name, value in schemes.items():
            text(name, "security scheme")
            if (not isinstance(value, dict) or len(set(value) & allowed) != 1
                    or not isinstance(value[next(iter(set(value) & allowed))], dict)):
                raise ProtocolError("SecurityScheme requires exactly one recognized variant")
        validate_security(data.get("securityRequirements", []), schemes)
        skills = data.get("skills")
        if not isinstance(skills, list) or not 1 <= len(skills) <= 1000:
            raise ProtocolError("Agent Card needs a bounded skill list")
        seen = set()
        for skill in skills:
            if not isinstance(skill, dict):
                raise ProtocolError("Malformed AgentSkill")
            for name in ("id", "name", "description"):
                text(skill.get(name), "skill " + name)
            if skill["id"] in seen:
                raise ProtocolError("Duplicate skill ID")
            seen.add(skill["id"])
            strings(skill.get("tags"), "skill tags", allow_empty=True)
            for name in ("inputModes", "outputModes"):
                if name in skill:
                    strings(skill[name], name)
            validate_security(skill.get("securityRequirements", []), schemes)
        return cls(encode(data))

    @property
    def data(self) -> dict:
        return decode(self.document, limit=512 * 1024)  # Defensive copy.

    @property
    def digest(self) -> str:
        return sha256(self.document).hexdigest()

    def interface(self, allowed_endpoints: frozenset[str]) -> dict:
        for item in self.data["supportedInterfaces"]:
            if (item["protocolBinding"] == "JSONRPC" and item["protocolVersion"] == "1.0"
                    and item["url"] in allowed_endpoints):
                return item
        raise ProtocolError("No owner-admitted A2A 1.0 JSONRPC interface")
