"""Skill-based routing among explicitly registered, digest-pinned remote agents."""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from threading import RLock

from ...models import PolicyError, identifier, integer
from ..http import HttpTransport
from ..jsonrpc import text
from .cards import AgentCard, security_satisfied


@dataclass(frozen=True)
class Requirements:
    skill_ids: frozenset[str] = frozenset()
    tags: frozenset[str] = frozenset()
    input_mode: str = "application/json"
    output_mode: str = "application/json"

    def __post_init__(self):
        if not self.skill_ids and not self.tags:
            raise PolicyError("Routing requires explicit skill IDs or tags")
        for values in (self.skill_ids, self.tags):
            if not isinstance(values, frozenset) or len(values) > 100:
                raise PolicyError("Routing skills/tags require a bounded immutable set")
            for value in values:
                text(value, "skill requirement", limit=200)
        text(self.input_mode, "input mode", limit=200); text(self.output_mode, "output mode", limit=200)


@dataclass(frozen=True)
class Selection:
    agent_id: str
    card_digest: str
    endpoint: str
    tenant: str | None
    skill_ids: tuple[str, ...]
    generation: int = 1


class AgentRegistry:
    """Owner configuration chooses trust; cards choose capability matches.

    Card signatures are retained as data, not claimed verified. An owner-supplied
    pinned digest plus endpoint allowlist is the trust mechanism in this slice.
    Discovery cannot auto-register a new agent or expand its permitted roles.
    """
    def __init__(self, database=None):
        self._entries, self._lock = {}, RLock()
        self._generations, self.database = {}, None
        if database is not None:self.bind(database)

    def bind(self, database):
        """Bind once to the owning Team; cached data never overwrites durable state."""
        from pathlib import Path
        from ...storage import remote_agents as durable
        path=str(Path(database).resolve())
        with self._lock:
            if self.database:
                if self.database!=path:raise PolicyError('Registry belongs to another operational database')
                return
            durable.install(path)
            # Explicit registrations made before binding are owner configuration.
            # If any identity is already durable, require a fresh registration
            # rather than resurrecting a cached entry over a revocation tombstone.
            for key,entry in self._entries.items():
                durable.bootstrap(path,key,entry)
            self.database=path
            self._entries,self._generations=durable.load(path)

    async def fetch(self, transport: HttpTransport, *, expected_digest: str) -> AgentCard:
        card = AgentCard.parse(await transport.get_json(headers={"A2A-Version": "1.0"}))
        if card.digest != expected_digest:
            raise PolicyError("Discovered Agent Card differs from the owner-approved digest")
        return card

    def register(self, agent_id: str, card: AgentCard, *, expected_digest: str,
                 allowed_roles: frozenset[str], allowed_endpoints: frozenset[str],
                 expires_at: float, priority: int = 0, security_grants: dict | None = None) -> None:
        identifier(agent_id, "registered agent ID")
        integer(priority, "routing priority")
        if (not isinstance(card, AgentCard) or card.digest != expected_digest
                or not isinstance(allowed_roles, frozenset) or not allowed_roles
                or type(expires_at) not in (int, float) or not math.isfinite(expires_at)):
            raise PolicyError("Invalid owner registration or card digest")
        for role in allowed_roles:
            identifier(role, "registered role")
        if not isinstance(allowed_endpoints, frozenset):
            raise PolicyError("Immutable endpoint allowlist required")
        if security_grants is not None and not isinstance(security_grants, dict):
            raise PolicyError("Security grants require an owner-configured map")
        for name, scopes in (security_grants or {}).items():
            if name not in card.data.get("securitySchemes", {}) or not isinstance(scopes, (list, tuple, frozenset)):
                raise PolicyError("Security grant references an undeclared scheme or invalid scopes")
            for scope in scopes:
                text(scope, "granted scope", limit=200)
        interface = card.interface(allowed_endpoints)
        grants = {name: frozenset(scopes) for name, scopes in (security_grants or {}).items()}
        with self._lock:
            entry = (card, interface, allowed_roles, expires_at, priority, grants)
            if self.database:
                from ...storage import remote_agents as durable
                durable.register(self.database,agent_id,entry)
                return
            if agent_id in self._entries:
                raise PolicyError("Agent already registered; explicitly revoke before replacing its card")
            self._entries[agent_id] = entry
            self._generations[agent_id] = self._generations.get(agent_id,0)+1

    def revoke(self, agent_id: str) -> None:
        with self._lock:
            if self.database:
                from ...storage import remote_agents as durable
                durable.revoke(self.database,agent_id)
                return
            self._entries.pop(agent_id, None)
            self._generations[agent_id] = self._generations.get(agent_id,0)+1

    def select(self, requirements: Requirements, *, role: str, now: float | None = None) -> Selection:
        now = time.time() if now is None else now
        if type(now) not in (int, float) or not math.isfinite(now):
            raise PolicyError("Invalid routing clock")
        ranked = []
        with self._lock:
            if self.database:
                from ...storage import remote_agents as durable
                self._entries,self._generations=durable.load(self.database)
            for agent_id, (card, interface, roles, expiry, priority, grants) in self._entries.items():
                if role not in roles or now >= expiry:
                    continue
                data = card.data
                if not security_satisfied(data.get("securityRequirements", []), grants):
                    continue
                compatible = [skill for skill in data["skills"]
                    if requirements.input_mode in skill.get("inputModes", data["defaultInputModes"])
                    and requirements.output_mode in skill.get("outputModes", data["defaultOutputModes"])
                    and security_satisfied(skill.get("securityRequirements", []), grants)]
                ids = {skill["id"] for skill in compatible}
                tags = {tag.casefold() for skill in compatible for tag in skill["tags"]}
                required_tags = {tag.casefold() for tag in requirements.tags}
                if not requirements.skill_ids <= ids or not required_tags <= tags:
                    continue
                matched = tuple(sorted(skill["id"] for skill in compatible
                    if skill["id"] in requirements.skill_ids or required_tags & {tag.casefold() for tag in skill["tags"]}))
                # Stable owner priority then smallest surplus then ID. No role->URL
                # mapping, language-specific branch or model-generated score.
                selection = Selection(agent_id, card.digest, interface["url"], interface.get("tenant"), matched,self._generations[agent_id])
                ranked.append((-priority, len(tags - required_tags), agent_id, selection))
        if not ranked:
            raise PolicyError("No authorized agent satisfies all required skills and modes")
        return min(ranked, key=lambda row: row[:3])[3]

    def validate(self, selection: Selection, *, role: str, now: float | None = None, db=None) -> None:
        now = time.time() if now is None else now
        if type(now) not in (int, float) or not math.isfinite(now):
            raise PolicyError("Invalid routing clock")
        # Never take a process mutex while the gateway owns the SQL writer lock:
        # register/revoke may already own that mutex while waiting for SQL.
        if self.database:
            from ...storage import remote_agents as durable
            entries,generations=durable.load(self.database,db)
        else:
            with self._lock:entries,generations=dict(self._entries),dict(self._generations)
        entry = entries.get(selection.agent_id)
        if (entry is None or type(selection.generation) is not int or generations.get(selection.agent_id)!=selection.generation
                or entry[0].digest != selection.card_digest or role not in entry[2]
                or now >= entry[3] or entry[1]["url"] != selection.endpoint
                or entry[1].get("tenant") != selection.tenant):
            raise PolicyError("Agent selection is expired, revoked or changed")
