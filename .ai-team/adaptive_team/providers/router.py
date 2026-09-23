"""Deterministic routing. Model names and available capacity are owner inputs.

This module chooses a route; it grants neither spend nor execution authority.
Prices and model availability must be maintained in deployment configuration.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from ..models import PolicyError, digest, identifier


@dataclass(frozen=True)
class ModelProfile:
    name: str
    provider: str
    model: str
    tier: str
    supports_batch: bool = False
    endpoint: str = "/v1/responses"
    # Owner-maintained prices in micro-USD per million tokens. None means
    # unpriced and is rejected by the metered gateway, never treated as free.
    input_price: int | None = None
    output_price: int | None = None
    batch_input_price: int | None = None
    batch_output_price: int | None = None

    def __post_init__(self):
        identifier(self.name, "model profile")
        identifier(self.provider, "provider")
        if not isinstance(self.model, str) or not self.model.strip() or len(self.model) > 200:
            raise PolicyError("A configured model identifier is required")
        if self.tier not in {"economy", "premium"} or type(self.supports_batch) is not bool:
            raise PolicyError("Invalid model tier or Batch capability")
        if self.endpoint not in {"/v1/responses", "/v1/chat/completions"}:
            raise PolicyError("Unsupported Batch-compatible model endpoint")
        for price in (self.input_price, self.output_price, self.batch_input_price, self.batch_output_price):
            if price is not None and (type(price) is not int or not 0 <= price <= 10**12):
                raise PolicyError("Price must be integer micro-USD per million tokens")


@dataclass(frozen=True)
class RoutingMetadata:
    task_type: str
    complexity: str = "medium"
    background: bool = False
    critical: bool = True
    deadline_seconds: int = 0

    def __post_init__(self):
        identifier(self.task_type, "task type")
        if self.complexity not in {"low", "medium", "high"}:
            raise PolicyError("Unknown complexity")
        if type(self.background) is not bool or type(self.critical) is not bool:
            raise PolicyError("Routing flags must be booleans")
        if type(self.deadline_seconds) is not int or not 0 <= self.deadline_seconds <= 365 * 86400:
            raise PolicyError("Invalid routing deadline")

    @classmethod
    def parse(cls, value: dict):
        if not isinstance(value, dict) or set(value) - set(cls.__dataclass_fields__):
            raise PolicyError("Unknown routing metadata fields")
        try:
            return cls(**value)
        except TypeError as exc:
            raise PolicyError("Incomplete routing metadata") from exc


@dataclass(frozen=True)
class Route:
    profile: ModelProfile
    mode: str
    policy_digest: str
    reason: str


class ModelRouter:
    @classmethod
    def from_config(cls, value: dict):
        """Parse owner-maintained JSON configuration; never accept model policy."""
        keys = {"profiles", "economy", "premium", "task_types", "premium_types"}
        if not isinstance(value, dict) or set(value) != keys:
            raise PolicyError("Router configuration fields do not match schema")
        for name in ("profiles", "task_types", "premium_types"):
            if not isinstance(value[name], list) or not 1 <= len(value[name]) <= 1000:
                raise PolicyError("Router configuration lists must be bounded")
        try:
            return cls(tuple(ModelProfile(**item) for item in value["profiles"]),
                economy=value["economy"], premium=value["premium"], task_types=frozenset(value["task_types"]),
                premium_types=frozenset(value["premium_types"]))
        except (TypeError, KeyError) as exc:
            raise PolicyError("Malformed model router configuration") from exc

    def __init__(self, profiles: tuple[ModelProfile, ...], *, economy: str, premium: str,
                 task_types: frozenset[str], premium_types: frozenset[str] = frozenset({"planning", "code_review"})):
        if not isinstance(profiles, tuple) or not profiles or not all(isinstance(p, ModelProfile) for p in profiles):
            raise PolicyError("Immutable model profiles are required")
        self._profiles = {p.name: p for p in profiles}
        if len(self._profiles) != len(profiles):
            raise PolicyError("Duplicate model profile")
        for name, tier in ((economy, "economy"), (premium, "premium")):
            if name not in self._profiles or self._profiles[name].tier != tier:
                raise PolicyError("Both routing tiers must have an explicit profile")
        for values in (task_types, premium_types):
            if not isinstance(values, frozenset) or not values:
                raise PolicyError("Immutable task-type sets are required")
            for value in values:
                identifier(value, "task type")
        if not premium_types <= task_types or "code_review" not in premium_types:
            raise PolicyError("Review must remain an explicit premium task type")
        self.economy, self.premium = economy, premium
        self.task_types, self.premium_types = task_types, premium_types
        self.policy_digest = digest({"profiles": [asdict(p) for p in sorted(profiles, key=lambda p: p.name)],
            "economy": economy, "premium": premium, "types": sorted(task_types), "premium_types": sorted(premium_types)})

    def route(self, metadata: RoutingMetadata) -> Route:
        if metadata.task_type not in self.task_types:
            raise PolicyError("Task type is not configured; no implicit cheap fallback")
        expensive = metadata.complexity == "high" or metadata.critical or metadata.task_type in self.premium_types
        profile = self._profiles[self.premium if expensive else self.economy]
        batch = (metadata.background and not metadata.critical and metadata.task_type != "code_review"
                 and metadata.deadline_seconds >= 86400 and profile.supports_batch)
        return Route(profile, "batch" if batch else "realtime", self.policy_digest,
                     "High complexity, critical work or premium task type" if expensive else "Routine noncritical work")

    def validate(self, route: Route, metadata: RoutingMetadata):
        if route != self.route(metadata):
            raise PolicyError("Route does not match current owner policy")
