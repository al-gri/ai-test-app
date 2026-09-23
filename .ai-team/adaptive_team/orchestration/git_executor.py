"""Git integration of the existing Team control plane.

No default model backend is supplied. The backend below is a *trusted service
adapter*, not a model-callable API. Implement its methods using isolated workers
and independently authenticated verifiers; never run candidate shell on this host.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Protocol

from ..code_integration.commits import Candidate, CommitManager
from ..engine import Team
from ..models import PolicyError, integer, digest
from ..observability.otel import instrument, operation, event
from ..observability.run_identity import execution_trace
from .task_lifecycle import TaskContract


@dataclass(frozen=True)
class StoppedExecution:
    termination_evidence: str
    actual_cost_cents: int


class TrustedBackend(Protocol):
    def implement(self, contract: TaskContract, workspace: Path,
                  heartbeat: Callable[[], dict]) -> StoppedExecution: ...

    def verify(self, contract: TaskContract, candidate: Candidate, workspace: Path,
               heartbeat: Callable[[], dict]) -> dict:
        """Return a bound Team receipt and termination_evidence.

        Implementation-phase verification reports only its own verification cost;
        GitExecutor adds the separately recorded implementation cost exactly once.
        Review uses a separate actor/session and the exact candidate commit.
        """
        ...


class GitExecutor:
    def __init__(self, team: Team, commits: CommitManager, backend: TrustedBackend,
                 target_ref: str, traces=None):
        self.team, self.commits, self.backend, self.target_ref = team, commits, backend, target_ref
        self.worktrees, self.journal = commits.worktrees, commits.journal
        self.traces = traces

    def candidate(self, artifact_digest: str) -> tuple[Candidate, TaskContract]:
        record = self.journal.get("artifact:" + artifact_digest)
        if not record or record["status"] != "complete":
            raise PolicyError("Candidate artifact is not registered")
        value = record["result"]
        candidate, contract = Candidate.load(value["candidate"]), TaskContract(**value["contract"])
        if candidate.artifact_digest != artifact_digest:
            raise PolicyError("Stored artifact digest mismatch")
        self.commits.verify(candidate, contract)
        return candidate, contract

    @instrument('agent.execution', lambda self,ticket: dict(trace_id=execution_trace(ticket),
        attributes={'task':ticket['task_id'],'role':ticket['role'],'lease_id':ticket['id'],
                    'generation':ticket['generation'],'phase':ticket['phase']}))
    def run(self, ticket: dict) -> dict:
        # Confirm the entire ticket against trusted persisted state, not only its
        # self-consistent hash. That hash alone would be forgeable by a worker.
        state = self.team.snapshot()
        lease = state["leases"].get(ticket["id"])
        # Heartbeats legitimately extend expires_at after the initial dispatch.
        immutable = lambda value: {k: v for k, v in value.items() if k != "expires_at"}
        if lease is None or immutable(lease) != immutable(ticket):
            raise PolicyError("Assignment is not the current trusted Team ticket")
        key = "execute:" + ticket["id"]
        request = {"ticket": ticket, "target_ref": self.target_ref}
        old = self.journal.begin(key, request)
        if old is not None:
            return old
        heartbeat = lambda: self.team.heartbeat(ticket["id"], ticket["actor_id"])
        # Validate live lease before spending or touching a workspace.
        heartbeat()
        if ticket["phase"] == "review":
            candidate, original = self.candidate(ticket["inputs"]["subject_digest"])
            if original.project != state["project"] or original.ticket["task_id"] != ticket["task_id"]:
                raise PolicyError("Review candidate belongs to another task")
            base = candidate.commit
        else:
            base = self.worktrees.resolve(self.target_ref)
            for dependency, artifact in ticket["inputs"]["dependencies"].items():
                record = self.journal.get(f"integrated:{state['project']}:{dependency}")
                if (not record or record["status"] != "complete"
                        or record["result"]["artifact_digest"] != artifact
                        or not self.contains(record["result"]["commit"], base)):
                    raise PolicyError("Dependency is not present in the chosen Git base")
        contract = TaskContract.from_ticket(ticket, state["project"], base)
        event('agent.prompt_bound',{'role':ticket['role'],'prompt_digest':ticket['inputs']['prompt_digest'],
                                  'input_digest':ticket['input_digest']})
        if self.traces:
            from ..observability.tracing import TraceContext
            self.traces.emit(TraceContext.task(contract.trace_id), 'prompt.bound', {
                'lease_id': contract.lease_id, 'phase': ticket['phase'], 'role': ticket['role'],
                'prompt_digest': ticket['inputs']['prompt_digest'], 'input_digest': ticket['input_digest']})
        self.journal.checkpoint(key, {"contract": asdict(contract)})
        implementation_cost = 0
        if ticket["phase"] == "implement":
            handle = self.worktrees.create(contract)
            previous = self.previous_candidate(state, ticket["task_id"])
            if previous is not None:
                self.worktrees.restore_tree(handle, previous.commit)
            elif ticket["inputs"]["previous_findings"]:
                raise PolicyError("Repair findings have no retained candidate")
            with operation('agent.implement',contract.trace_id,{'role':ticket['role']}):
                stopped = self.backend.implement(contract, Path(handle.path), heartbeat)
            if not isinstance(stopped, StoppedExecution):
                raise PolicyError("Trusted stopped-executor result required")
            integer(stopped.actual_cost_cents, "implementation cost")
            implementation_cost = stopped.actual_cost_cents
            self.journal.checkpoint(key, {"contract": asdict(contract), "stopped": asdict(stopped)})
            candidate = self.commits.capture(handle, contract, termination_evidence=stopped.termination_evidence)
            if self.traces:
                self.traces.emit(TraceContext.task(contract.trace_id), 'git.commit', {'commit': candidate.commit,
                    'artifact_digest': candidate.artifact_digest, 'lease_id': contract.lease_id})
            artifact = {"candidate": asdict(candidate), "contract": asdict(contract)}
            artifact_key = "artifact:" + candidate.artifact_digest
            if self.journal.begin(artifact_key, artifact) is None:
                self.journal.finish(artifact_key, artifact)
        # Verification sees a fresh checkout of the immutable commit, not the
        # mutable directory in which the implementation worker ran.
        verification = self.worktrees.create(contract, purpose="verification", base_commit=candidate.commit)
        with operation('agent.verify',contract.trace_id,{'role':ticket['role']}):
            receipt = dict(self.backend.verify(contract, candidate, Path(verification.path), heartbeat))
        if not isinstance(receipt.get("termination_evidence"), str) or not receipt["termination_evidence"].strip():
            raise PolicyError("Verifier termination must be confirmed")
        for key_name, expected in (("lease_id", ticket["id"]), ("actor_id", ticket["actor_id"]),
                                   ("input_digest", ticket["input_digest"])):
            if receipt.get(key_name) != expected:
                raise PolicyError("Verifier receipt identity mismatch")
        if receipt.get("outcome") == "success":
            field = "artifact_digest" if ticket["phase"] == "implement" else "subject_digest"
            if receipt.get(field) != candidate.artifact_digest:
                raise PolicyError("Verifier did not check this candidate")
            if ticket["phase"] == "implement" and sorted(receipt.get("changed_files", [])) != list(candidate.changed_files):
                raise PolicyError("Receipt omits or invents changed files")
        integer(receipt.get("actual_cost_cents"), "verification cost")
        receipt["actual_cost_cents"] += implementation_cost
        receipt["candidate_digest"] = candidate.artifact_digest
        # Persist before Team.finish. A crash between databases is recoverable by
        # resubmitting this exact receipt; Team.finish provides billing idempotency.
        return self.journal.finish(key, receipt)

    def previous_candidate(self, state: dict, task_id: str) -> Candidate | None:
        for event in reversed(state["tasks"][task_id]["history"]):
            if event["phase"] != "implement":
                continue
            record = self.journal.get("execute:" + event["lease"])
            if record and record["status"] == "complete":
                artifact = record["result"].get("candidate_digest")
                if artifact:
                    return self.candidate(artifact)[0]
        return None

    def contains(self, ancestor: str, descendant: str) -> bool:
        common = self.worktrees.git.run(self.worktrees.repository, "merge-base", ancestor, descendant,
                                        allowed=(0, 1)).decode().split()
        return common == [ancestor]


