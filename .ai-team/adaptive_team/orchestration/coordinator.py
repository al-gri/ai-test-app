"""Small facade joining dependency readiness, execution, files and integration."""
from __future__ import annotations
from typing import Callable
from ..code_integration.commits import CommitManager
from ..engine import Team
from ..models import PolicyError
from .dag import DAG, Node
from .waves import WaveDispatcher
from .task_files import TaskFiles
from .integration import IntegrationService
from .task_lifecycle import TaskContract
# Preserve the v0.4 public import paths for backend integrations.
from .git_executor import GitExecutor, StoppedExecution, TrustedBackend


class Coordinator:
    """Single admission pump per Team; Git mutations serialize across managers.

    Team continues enforcing its owner policy, cost reservations, role/group caps,
    independent review, retries and leases. Git readiness only narrows admission.
    """
    def __init__(self, team: Team, commits: CommitManager, backend: TrustedBackend, *, target_ref: str, memory=None, traces=None, llmops=None):
        state = team.snapshot()
        if target_ref != f"refs/heads/ai-team/{state['project']}":
            raise PolicyError("Wrong project's integration target")
        self.team, self.commits, self.target_ref = team, commits, target_ref
        if memory is not None and memory.team is not team:
            raise PolicyError("Coordinator memory must use the same Team instance")
        self.memory = memory
        if llmops is not None and llmops.team is not team:
            raise PolicyError('LLMOps must use the same Team instance')
        self.llmops = llmops
        self.traces = traces
        self.executor = GitExecutor(team, commits, backend, target_ref, traces=traces)
        self.task_files = TaskFiles(team.database, commits.worktrees.git.control / "tasks" / state["project"])
        self.integration = IntegrationService(team, commits, self.executor, target_ref)
        self.dispatcher = WaveDispatcher(team, self.executor, max_workers=state["policy"]["max_active"],
                                        before_dispatch=self.task_files.drain, before_submit=self._admit)

    def _admit(self, ticket):
        self.task_files.admit(ticket)
        if self.memory is not None:
            self.memory.prepare(ticket)

    def tick(self) -> dict:
        if self.llmops is not None:
            self.llmops.sync()
        state = self.team.snapshot()
        base = self.commits.worktrees.resolve(self.target_ref)
        graph = DAG(Node(key, tuple(task["spec"]["dependencies"]), tuple(task["spec"]["write_scopes"]),
                         tuple(task["spec"]["resources"]), task["spec"]["priority"])
                    for key, task in state["tasks"].items())
        integrated = set()
        for key, task in state["tasks"].items():
            record = self.commits.journal.get(f"integrated:{state['project']}:{key}")
            if record and record["status"] == "complete":
                if (task["status"] != "accepted" or not task["result"]
                        or record["result"]["artifact_digest"] != task["result"]["artifact_digest"]
                        or not self.executor.contains(record["result"]["commit"], base)):
                    raise PolicyError("Integrated dependency history diverged")
                integrated.add(key)
        occupied = {key for key, task in state["tasks"].items() if task["status"] in ("running", "reviewing")}
        held = {key for key, task in state["tasks"].items()
                if task["status"] in ("blocked", "escalated", "accepted") and key not in integrated}
        eligible = {node.id for node in graph.ready(integrated, occupied, held)}
        # A repair must retain its candidate. If the base moved, block admission
        # before consuming a lease; conflict/rebase resolution is a later step.
        for key in tuple(eligible):
            task = state["tasks"][key]
            if task["status"] == "pending" and task["attempts"]:
                previous = self.executor.previous_candidate(state, key)
                if previous is not None and previous.base_commit != base:
                    eligible.remove(key)
        result = self.dispatcher.tick(eligible_tasks=eligible)
        if self.llmops is not None:
            result['llmops'] = self.llmops.sync()
        if self.memory is not None:
            result["memory"] = self.memory.sync_accepted()
        return result

    def integrate_accepted(self, task_id: str, *, verify_merged: Callable,
                           required_checks: tuple[str, ...], verification_budget_cents: int = 0,
                           conflict_resolver: Callable | None = None) -> dict:
        # Cross-process lock covers admission, recovery and release, rather than
        # only Git mutation. It is separate from Team's short DB transactions.
        with self._integration_mutex().edit():
            return self._integrate_accepted(task_id, verify_merged=verify_merged,
                required_checks=required_checks, verification_budget_cents=verification_budget_cents,
                conflict_resolver=conflict_resolver)

    def _integration_mutex(self):
        from pathlib import Path
        from ..security._store import SecurityStore
        database = Path(self.team.database)
        return SecurityStore(database.with_name(database.name + '.integration-lock.sqlite'))

    def _integrate_accepted(self, task_id: str, *, verify_merged: Callable,
                           required_checks: tuple[str, ...], verification_budget_cents: int = 0,
                           conflict_resolver: Callable | None = None) -> dict:
        from ..llmops.human_requests import held_tasks
        from ..models import integer
        integer(verification_budget_cents, 'verification budget')
        if verification_budget_cents != 0:
            raise PolicyError('Paid integration checks require a separately metered backend')
        with self.team._edit() as (state, db, at):
            task = state['tasks'].get(task_id)
            if not task or task['status'] != 'accepted':
                raise PolicyError('Only accepted work may enter integration')
            if task_id in held_tasks(state) or state.get('integration_inflight') not in (None, task_id):
                raise PolicyError("Human hold or unresolved integration admission")
            for parent in task['spec']['dependencies']:
                record = self.commits.journal.get(f"integrated:{state['project']}:{parent}")
                if not record or record['status'] != 'complete':
                    raise PolicyError('Integrate parent tasks first')
            state['integration_inflight'] = task_id
        # Durable admission closes the hold-vs-merge race without keeping a DB
        # lock during verification. Crash leaves a pin for owner reconciliation.
        try:
            result = self.integration.integrate(task_id, verify_merged=verify_merged,
                required_checks=required_checks, verification_budget_cents=verification_budget_cents,
                conflict_resolver=conflict_resolver)
        except BaseException:
            key = f"integrated:{state['project']}:{task_id}"
            if self.commits.journal.get(key) is None:
                # IntegrationService never started an external operation.
                self._release_integration(task_id, termination_evidence='No integration operation was started')
            raise
        with self.team._edit() as (state, db, at):
            if state.get('integration_inflight') != task_id:
                raise PolicyError('Integration admission changed unexpectedly')
            # Additive read projection for older Stage 2 databases. The encrypted
            # Git journal remains authoritative for external-operation recovery.
            db.execute('CREATE TABLE IF NOT EXISTS task_integrations(task_id TEXT PRIMARY KEY REFERENCES tasks(id), artifact_digest TEXT NOT NULL, commit_oid TEXT NOT NULL)')
            db.execute('INSERT INTO task_integrations VALUES(?,?,?) ON CONFLICT(task_id) DO UPDATE SET artifact_digest=excluded.artifact_digest,commit_oid=excluded.commit_oid',
                (task_id,result['artifact_digest'],result['commit']))
            state.pop('integration_inflight')
        if self.traces:
            from ..observability.tracing import TraceContext
            try:
                artifact = self.commits.journal.get('artifact:' + result['artifact_digest'])
                trace_id = TaskContract(**artifact['result']['contract']).trace_id
                self.traces.emit(TraceContext.task(trace_id), 'git.integrated', result)
            except Exception:
                # Optional correlation lookup cannot undo a known merged result.
                pass
        return result

    def release_integration(self, task_id, *, termination_evidence):
        """Trusted maintenance after all merge/verifier workers are stopped.

        A lost callback must be reconciled first. Clearing admission does not
        remove journal evidence, accept a candidate, or retry an external call.
        The same task can also retry integrate_accepted using its existing journal.
        """
        with self._integration_mutex().edit():
            self._release_integration(task_id, termination_evidence=termination_evidence)

    def _release_integration(self, task_id, *, termination_evidence):
        if not isinstance(termination_evidence, str) or not termination_evidence.strip():
            raise PolicyError('Verified termination evidence required')
        with self.team._edit() as (state, db, at):
            if state.get('integration_inflight') != task_id:
                raise PolicyError('No matching integration admission')
            state.pop('integration_inflight')
            from ..models import digest
            self.team._event(db, at, 'integration.reconciled', {'task': task_id,
                'termination_digest': digest(termination_evidence)})

    def replay_receipt(self, lease_id: str) -> dict:
        record = self.commits.journal.get("execute:" + lease_id)
        if not record or record["status"] != "complete":
            raise PolicyError("No durable completed receipt to replay")
        return self.team.finish(record["result"])

    def close(self, *, wait: bool = True) -> None:
        self.dispatcher.close(wait=wait)
        if self.llmops is not None:
            self.llmops.close(wait=wait)
