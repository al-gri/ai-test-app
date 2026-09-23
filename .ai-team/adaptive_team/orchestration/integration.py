"""Acceptance authentication and durable integration, separate from dispatch."""
from typing import Callable
from contextlib import contextmanager
from ..models import PolicyError, integer


class IntegrationService:
    def __init__(self, team, commits, executor, target_ref):
        self.team, self.commits = team, commits
        self.executor, self.target_ref = executor, target_ref

    def integrate(self, task_id: str, *, verify_merged: Callable,
                           required_checks: tuple[str, ...], verification_budget_cents: int = 0, conflict_resolver: Callable | None = None) -> dict:
        integer(verification_budget_cents, "verification budget")
        if verification_budget_cents != 0:
            raise PolicyError("Paid integration checks are unavailable until shared reservations are connected")
        state = self.team.snapshot()
        task = state["tasks"].get(task_id)
        if not task or task["status"] != "accepted":
            raise PolicyError("Only independently accepted work may enter the integration ref")
        # The caller may add checks, but cannot drop task or owner-policy checks.
        required_checks = tuple(dict.fromkeys((*task["spec"]["checks"], *required_checks,
            *state["policy"].get("acceptance_gate", {}).get("checks", {}))))
        artifact = task["result"]["artifact_digest"]
        key = f"integrated:{state['project']}:{task_id}"
        record = self.commits.journal.get(key)
        if record and record["request"]["artifact_digest"] != artifact:
            raise PolicyError("Cannot replace an integrated task's artifact")
        if record and record["status"] == "complete":
            if not self.executor.contains(record["result"]["commit"], self.commits.worktrees.resolve(self.target_ref)):
                raise PolicyError("Previously integrated task is absent from target history")
            return record["result"]
        for parent in task["spec"]["dependencies"]:
            parent_record = self.commits.journal.get(f"integrated:{state['project']}:{parent}")
            if not parent_record or parent_record["status"] != "complete":
                raise PolicyError("Integrate parent tasks first")
        request = {"artifact_digest": artifact,
                   "expected_head": self.commits.worktrees.resolve(self.target_ref),
                   "checks": list(required_checks), "budget": verification_budget_cents}
        if record:
            request = record["request"]
            if request["checks"] != list(required_checks) or request["budget"] != verification_budget_cents:
                raise PolicyError("Pending integration policy changed")
        else:
            self.commits.journal.begin(key, request)
        candidate, contract = self.executor.candidate(artifact)
        @contextmanager
        def publication_guard():
            # Reauthorize after slow verification and serialize publication with
            # Team holds/pause/policy transitions. No external callback runs here.
            from ..llmops.human_requests import held_tasks
            with self.team._edit() as (current, db, at):
                current_task = current['tasks'].get(task_id)
                if (current['paused'] or current['budget_exceeded']
                        or task_id in held_tasks(current)
                        or not current_task or current_task['status'] != 'accepted'
                        or current_task['result']['artifact_digest'] != artifact
                        or current['policy_digest'] != state['policy_digest']
                        or any(current['role_registry'][role]['generation'] != state['role_registry'][role]['generation']
                            or current['role_registry'][role]['revoked'] for role in
                            {task['spec']['role'],task['spec']['reviewer_role']})):
                    raise PolicyError('Integration authority changed during verification')
                yield
        result = self.commits.integrate(candidate, contract, target_ref=self.target_ref,
                                       expected_head=request["expected_head"], required_checks=required_checks,
                                       verify_merged=verify_merged,
                                       verification_budget_cents=verification_budget_cents,
                                       acceptance_policy=state["policy"].get("acceptance_gate"),
                                       max_conflict_attempts=state["policy"].get("max_conflict_attempts", 3),
                                       conflict_resolver=conflict_resolver, publication_guard=publication_guard)
        return self.commits.journal.finish(key, result)

