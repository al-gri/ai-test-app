"""Pre-code formal plan assessment, bound to the exact executable plan."""
from __future__ import annotations

from ..models import PolicyError, digest, validate_plan
from .gates import evaluate


DEFAULT_PLAN_GATE = {"threshold": 95, "checks": {
    "structure": {"weight": 100, "required": True, "critical": True},
}}


class PlanGate:
    def evaluate(self, plan: dict, catalog: dict, *, evidence: dict | None = None,
                 accepted_specs: dict | None = None):
        # Deterministic structural validation covers DAG, role access, scopes,
        # review requirements, finite budgets and bounded retry/lease policy.
        # It does NOT pretend to measure market desirability or semantic quality.
        policy, _ = validate_plan(plan, catalog, accepted_specs=accepted_specs)
        checks = {"structure": "PASS"}
        if evidence is not None:
            if (set(evidence) != {"subject_digest", "checks", "evidence_refs"}
                    or evidence["subject_digest"] != digest(plan)
                    or not isinstance(evidence["checks"], dict)
                    or not isinstance(evidence["evidence_refs"], list) or not evidence["evidence_refs"]
                    or not all(isinstance(x, str) and x.strip() for x in evidence["evidence_refs"])):
                raise PolicyError("Plan evidence must be bound to this exact plan")
            if "structure" in evidence["checks"]:
                raise PolicyError("External reports cannot override structural validation")
            checks.update(evidence["checks"])
        return evaluate(policy.plan_gate, checks, subject_digest=digest(plan),
                        expected_checks=("structure",), critical=("structure",))
