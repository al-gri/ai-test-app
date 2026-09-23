"""Post-code scoring of trusted lint/test/security evidence, never model scores."""
from __future__ import annotations

from .gates import evaluate


DEFAULT_ACCEPTANCE_GATE = {"threshold": 95, "checks": {}}


class AcceptanceGate:
    def evaluate(self, *, subject_digest: str, checks: dict,
                 required_checks: tuple[str, ...], policy: dict | None = None,
                 critical_checks: tuple[str, ...] = ()):
        # Required task checks stay mandatory unless an owner-approved policy
        # explicitly makes a NON-security check optional. Security always vetoes.
        return evaluate(DEFAULT_ACCEPTANCE_GATE if policy is None else policy, checks,
                        subject_digest=subject_digest, expected_checks=required_checks, critical=critical_checks)
