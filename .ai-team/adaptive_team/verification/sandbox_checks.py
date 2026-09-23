"""Translate trusted sandbox evidence into existing acceptance-gate checks."""
from ..models import PolicyError
from ..security.sandbox import SandboxEvidence
from .acceptance_gate import AcceptanceGate


def sandbox_checks(evidence: SandboxEvidence, *, subject_digest: str, policy_digest: str, input_digest: str, check_name='tests'):
    if not isinstance(evidence, SandboxEvidence) or evidence.subject_digest != subject_digest or evidence.policy_digest != policy_digest or evidence.input_digest != input_digest:
        raise PolicyError("Sandbox result does not bind the expected artifact and policy")
    if check_name in {'security', 'sandbox_policy', 'sandbox_termination'}:
        raise PolicyError("Test name cannot replace a critical sandbox check")
    return {check_name: 'PASS' if evidence.stopped and not evidence.timed_out and not evidence.output_limited
            and evidence.console.exit_code == 0 else 'FAIL',
            'sandbox_termination': 'PASS' if evidence.stopped else 'FAIL',
            # This is an execution-policy check, not an application security
            # audit. Never synthesize or overwrite a verifier's "security" result.
            'sandbox_policy': 'PASS' if evidence.stopped and not evidence.timed_out and not evidence.output_limited else 'FAIL'}


def acceptance(evidence, *, subject_digest, policy_digest, input_digest, check_name='tests', policy=None):
    checks = sandbox_checks(evidence, subject_digest=subject_digest, policy_digest=policy_digest, input_digest=input_digest, check_name=check_name)
    return AcceptanceGate().evaluate(subject_digest=subject_digest, checks=checks,
        required_checks=(check_name, 'sandbox_termination', 'sandbox_policy'), policy=policy,
        critical_checks=('sandbox_termination', 'sandbox_policy'))
