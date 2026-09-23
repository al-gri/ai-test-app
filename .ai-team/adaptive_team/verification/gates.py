"""Integer-only weighted gates with mandatory checks and an unconditional veto.

Weights and criticality come from owner policy, never from an agent's report.
95% is a formal pass ratio, not a probability of product correctness.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from ..models import PolicyError, digest, integer


DEFAULT_RULE = {"weight": 1, "required": True, "critical": False}
SECURITY_CHECKS = frozenset({"security", "scope", "dependency_ownership", "independence"})


def validate_policy(policy: dict) -> None:
    if not isinstance(policy, dict) or set(policy) != {"threshold", "checks"}:
        raise PolicyError("Gate policy requires exactly threshold and checks")
    integer(policy["threshold"], "gate threshold", 95)
    if policy["threshold"] > 100 or not isinstance(policy["checks"], dict):
        raise PolicyError("Gate threshold must be 95..100 and checks must be an object")
    for name, rule in policy["checks"].items():
        if not isinstance(name, str) or not name.strip() or len(name) > 200:
            raise PolicyError("Invalid gate check name")
        if not isinstance(rule, dict) or set(rule) != set(DEFAULT_RULE):
            raise PolicyError("Each gate rule requires weight, required and critical")
        integer(rule["weight"], "gate weight", 1)
        if rule["weight"] > 1_000_000 or any(type(rule[k]) is not bool for k in ("required", "critical")):
            raise PolicyError("Invalid gate weight or flags")
        if name in SECURITY_CHECKS and not rule["critical"]:
            raise PolicyError("Security checks cannot be downgraded")


@dataclass(frozen=True)
class GateResult:
    passed: bool
    earned_weight: int
    total_weight: int
    score_basis_points: int
    threshold: int
    subject_digest: str
    policy_digest: str
    blockers: tuple[str, ...]

    def require(self) -> GateResult:
        if not self.passed:
            raise PolicyError("Gate blocked: " + "; ".join(self.blockers))
        return self

    def to_dict(self) -> dict:
        return {**asdict(self), "blockers": list(self.blockers)}


def evaluate(policy: dict, checks: dict, *, subject_digest: str,
             expected_checks: tuple[str, ...], critical: tuple[str, ...] = ()) -> GateResult:
    validate_policy(policy)
    if not expected_checks or len(set(expected_checks)) != len(expected_checks):
        raise PolicyError("Gate needs an explicit unique check set")
    if not isinstance(checks, dict):
        raise PolicyError("Check report must be an object")
    rules = {name: dict(policy["checks"].get(name, DEFAULT_RULE)) for name in expected_checks}
    # Configured checks are additional obligations, not ignored suggestions.
    rules.update({k: dict(v) for k, v in policy["checks"].items()})
    for name in set(critical) | (set(rules) & SECURITY_CHECKS):
        rules.setdefault(name, dict(DEFAULT_RULE))["critical"] = True
        rules[name]["required"] = True
    blockers = []
    earned = 0
    for name, rule in rules.items():
        status = checks.get(name)
        if status == "PASS":
            earned += rule["weight"]
        elif rule["critical"]:
            blockers.append("critical:" + name)
        elif rule["required"]:
            blockers.append("required:" + name)
        elif status != "FAIL":
            # Missing, ERROR, SKIP, NaN and NOT_APPLICABLE cannot silently lower
            # the denominator. Optional tolerated failures must be explicit FAIL.
            blockers.append("missing_or_invalid:" + name)
    # A verifier's extra failed security check cannot disappear from the rubric.
    for name in SECURITY_CHECKS & checks.keys():
        if checks[name] != "PASS" and "critical:" + name not in blockers:
            blockers.append("critical:" + name)
    total = sum(rule["weight"] for rule in rules.values())
    if earned * 100 < policy["threshold"] * total:
        blockers.append("score_below_threshold")
    return GateResult(not blockers, earned, total, earned * 10000 // total,
                      policy["threshold"], subject_digest,
                      digest({"threshold": policy["threshold"], "rules": rules}), tuple(blockers))
