from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import PurePosixPath
from typing import Any


class PolicyError(ValueError):
    """The submitted input or transition violates the control-plane contract."""


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def integer(value: Any, name: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise PolicyError(f"{name} must be an integer >= {minimum}")
    return value


def identifier(value: Any, name: str = "id") -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,79}", value):
        raise PolicyError(f"Invalid {name}: {value!r}")
    return value


def scope(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise PolicyError("Scopes must be canonical repository-relative POSIX paths")
    if value.startswith("/") or value.endswith("/") or any(c in value for c in '*?[]<>"|\x00\r\n'):
        raise PolicyError(f"Unsafe scope: {value!r}")
    if any(p in ("", ".", "..") for p in value.split("/")):
        raise PolicyError(f"Unsafe scope: {value!r}")
    reserved = {"con", "prn", "aux", "nul"} | {f"{p}{i}" for p in ("com", "lpt") for i in range(1, 10)}
    if any(p.endswith((".", " ")) or p.split(".")[0].casefold() in reserved for p in value.split("/")):
        raise PolicyError(f"Ambiguous cross-platform scope: {value!r}")
    if str(PurePosixPath(value)) != value:
        raise PolicyError(f"Noncanonical scope: {value!r}")
    return value


def within(path: str, root: str) -> bool:
    # Conservative across Windows/Linux: case-only paths also conflict.
    path, root = path.casefold(), root.casefold()
    return path == root or path.startswith(root + "/")


def overlaps(a: str, b: str) -> bool:
    return within(a, b) or within(b, a)


@dataclass
class Policy:
    plan_gate: dict = field(default_factory=lambda: {"threshold": 95, "checks": {
        "structure": {"weight": 100, "required": True, "critical": True}}})
    acceptance_gate: dict = field(default_factory=lambda: {"threshold": 95, "checks": {}})
    max_conflict_attempts: int = 3
    max_active: int = 4
    max_implementers: int = 2
    max_reviewers: int = 2
    max_active_per_role: int = 2
    max_active_per_group: int = 4
    group_capacities: dict[str, int] = field(default_factory=dict)
    budget_cents: int = 2000
    lease_seconds: int = 900
    max_run_seconds: int = 3600
    retry_backoff_seconds: int = 30
    allow_solo_low_risk: bool = True
    protected_scopes: list[str] = field(default_factory=lambda: [
        ".git", ".github", ".ai-team", "adaptive_team", "agents", "tasks", "AGENTS.md", ".env", "secrets"
    ])
    allowed_roles: list[str] = field(default_factory=list)
    resource_capacities: dict[str, int] = field(default_factory=lambda: {
        "tia": 1, "release": 1, "database_migration": 1
    })

    def validate(self, catalog: dict) -> None:
        from .verification.gates import validate_policy
        validate_policy(self.plan_gate)
        validate_policy(self.acceptance_gate)
        integer(self.max_conflict_attempts, "max_conflict_attempts", 1)
        if self.max_conflict_attempts > 10:
            raise PolicyError("Conflict attempts are limited to 10")
        for name in ("max_active", "max_implementers", "max_reviewers", "max_active_per_role",
                     "budget_cents", "lease_seconds", "max_run_seconds", "max_active_per_group"):
            integer(getattr(self, name), name, 1)
        integer(self.retry_backoff_seconds, "retry_backoff_seconds")
        if self.lease_seconds > self.max_run_seconds:
            raise PolicyError("lease_seconds exceeds max_run_seconds")
        if type(self.allow_solo_low_risk) is not bool:
            raise PolicyError("allow_solo_low_risk must be bool")
        if not isinstance(self.allowed_roles, list) or not all(isinstance(x, str) for x in self.allowed_roles) or len(set(self.allowed_roles)) != len(self.allowed_roles):
            raise PolicyError("allowed_roles must be a unique list")
        if set(self.allowed_roles) - catalog.keys():
            raise PolicyError("Unknown role in policy")
        if not isinstance(self.protected_scopes, list):
            raise PolicyError("protected_scopes must be a list")
        for item in self.protected_scopes:
            scope(item)
        if not isinstance(self.group_capacities, dict):
            raise PolicyError("group_capacities must be an object")
        for key, value in self.group_capacities.items():
            identifier(key, "group")
            integer(value, "group capacity", 1)
        if not isinstance(self.resource_capacities, dict):
            raise PolicyError("resource_capacities must be an object")
        for key, value in self.resource_capacities.items():
            identifier(key, "resource")
            integer(value, "resource capacity", 1)


@dataclass
class Task:
    id: str
    title: str
    role: str
    acceptance: list[str]
    checks: list[str]
    write_scopes: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    risk: str = "low"
    review_required: bool = True
    reviewer_role: str = "code_reviewer"
    resources: list[str] = field(default_factory=list)
    priority: int = 50
    attempt_budget_cents: int = 100
    review_budget_cents: int = 50
    max_attempts: int = 3
    group: str = "product"
    source_authors: list[str] = field(default_factory=list)
    context: dict = field(default_factory=dict)

    def validate(self, policy: Policy, catalog: dict) -> None:
        if not isinstance(self.context, dict) or len(canonical(self.context)) > 16000:
            raise PolicyError("Task context must be a bounded object")
        identifier(self.id)
        identifier(self.group, "group")
        if not isinstance(self.title, str) or not self.title.strip():
            raise PolicyError("Task needs a title")
        if self.risk not in ("low", "medium", "high"):
            raise PolicyError("Unknown risk")
        if type(self.review_required) is not bool:
            raise PolicyError("review_required must be bool")
        for name in ("acceptance", "checks"):
            values = getattr(self, name)
            if not isinstance(values, list) or not values or not all(isinstance(x, str) and x.strip() for x in values):
                raise PolicyError(f"Task requires nonempty {name}")
            if len(set(values)) != len(values):
                raise PolicyError(f"Duplicate {name}")
        for name in ("write_scopes", "dependencies", "resources", "source_authors"):
            values = getattr(self, name)
            if not isinstance(values, list) or not all(isinstance(x, str) for x in values) or len(set(values)) != len(values):
                raise PolicyError(f"Invalid {name}")
        for role in (self.role, self.reviewer_role):
            if not isinstance(role, str) or role not in catalog or (policy.allowed_roles and role not in policy.allowed_roles):
                raise PolicyError(f"Role not authorized: {role}")
        for role in {self.role, self.reviewer_role} if self.review_required else {self.role}:
            if catalog[role].get("status", "active") not in ("active", "trial"):
                raise PolicyError(f"Role unavailable for new tasks: {role}")
        if self.review_required and "review" not in catalog[self.reviewer_role]["capabilities"]:
            raise PolicyError("Reviewer role lacks review capability")
        if self.write_scopes and "write" not in catalog[self.role]["capabilities"]:
            raise PolicyError("Read-only role cannot receive write scopes")
        if not self.review_required and (self.risk != "low" or not policy.allow_solo_low_risk):
            raise PolicyError("Solo execution is allowed only for authorized low-risk tasks")
        for path in self.write_scopes:
            scope(path)
            if any(overlaps(path, protected) for protected in policy.protected_scopes):
                raise PolicyError(f"Protected control-plane scope: {path}")
        for dep in self.dependencies:
            identifier(dep, "dependency")
        for resource in self.resources:
            if resource not in policy.resource_capacities:
                raise PolicyError(f"Resource must have an explicit capacity: {resource}")
        for name in ("attempt_budget_cents", "review_budget_cents", "max_attempts"):
            integer(getattr(self, name), name, 1)
        integer(self.priority, "priority")


def validate_plan(plan: dict, catalog: dict, *, accepted_specs: dict | None = None) -> tuple[Policy, list[Task]]:
    if not isinstance(plan, dict) or set(plan) != {"schema_version", "project", "policy", "tasks"} or type(plan["schema_version"]) is not int or plan["schema_version"] != 1:
        raise PolicyError("Unsupported plan shape/version")
    if not isinstance(plan["policy"], dict) or not isinstance(plan["tasks"], list):
        raise PolicyError("Policy must be an object and tasks must be a list")
    identifier(plan["project"], "project")
    try:
        policy = Policy(**plan["policy"])
        tasks = [Task(**x) for x in plan["tasks"]]
    except (TypeError, KeyError) as exc:
        raise PolicyError(f"Invalid plan fields: {exc}") from exc
    policy.validate(catalog)
    if not tasks or len(tasks) > 10000:
        raise PolicyError("Plan needs 1..10000 bounded tasks")
    ids = {task.id for task in tasks}
    if len(ids) != len(tasks):
        raise PolicyError("Duplicate task ID")
    for task in tasks:
        # Internal maintenance can preserve exact already accepted historical
        # specs; changed/new tasks always require current role validation.
        if not accepted_specs or accepted_specs.get(task.id) != asdict(task):
            task.validate(policy, catalog)
        if set(task.dependencies) - ids or task.id in task.dependencies:
            raise PolicyError(f"Invalid dependencies on {task.id}")
    pending = {t.id: set(t.dependencies) for t in tasks}
    done: set[str] = set()
    while pending:
        ready = {key for key, deps in pending.items() if deps <= done}
        if not ready:
            raise PolicyError("Dependency graph contains a cycle")
        done |= ready
        pending = {key: deps for key, deps in pending.items() if key not in ready}
    return policy, tasks


def plan_dict(project: str, policy: Policy, tasks: list[Task]) -> dict:
    return {"schema_version": 1, "project": project, "policy": asdict(policy),
            "tasks": [asdict(task) for task in tasks]}
