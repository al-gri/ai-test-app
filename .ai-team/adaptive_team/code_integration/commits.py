"""Immutable candidate commits and verified compare-and-swap integration.

Only trusted adapters call these APIs. A backend must stop/freeze the writer
before capture. Verification must run in its own execution boundary; this module
never runs product tests or model-generated shell commands on the coordinator.
"""
from __future__ import annotations

import stat
from contextlib import nullcontext
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from ..models import PolicyError, digest, integer, overlaps, scope, within
from ..orchestration.task_lifecycle import RecoveryRequired, TaskContract, object_id
from ._git import GitError, checked_path
from .worktrees import Worktree, WorktreeManager
from .dependency_policy import enforce_paths, enforce_diff
from ..observability.otel import instrument


@dataclass(frozen=True)
class Candidate:
    contract_digest: str
    lease_id: str
    base_commit: str
    commit: str
    tree: str
    changed_files: tuple[str, ...]
    trace_id: str

    @property
    def artifact_digest(self) -> str:
        return digest(asdict(self))

    @classmethod
    def load(cls, value: dict) -> Candidate:
        return cls(**{**value, "changed_files": tuple(value["changed_files"])})


class MergeConflict(RuntimeError):
    def __init__(self, path: str, diff: str, prompt: dict | None = None):
        self.path, self.diff, self.prompt = path, diff, prompt
        super().__init__("Integration conflict retained in an isolated worktree")


class CommitManager:
    def __init__(self, worktrees: WorktreeManager, *, protected_scopes: tuple[str, ...] = ()):
        self.worktrees, self.git, self.journal = worktrees, worktrees.git, worktrees.journal
        self.protected = tuple(dict.fromkeys((".git", ".github", ".ai-team", ".ai-team-state",
                                             ".env", "AGENTS.md", "secrets", *protected_scopes)))
        for value in self.protected:
            scope(value)

    def _paths(self, data: bytes) -> tuple[str, ...]:
        return tuple(sorted({scope(p.decode("utf-8", "strict")) for p in data.split(b"\0") if p}))

    def _check_paths(self, paths: tuple[str, ...], contract: TaskContract) -> None:
        enforce_paths(contract.ticket["role"], paths)
        for path in paths:
            parts = path.casefold().split("/")
            # Attributes can affect Git's behavior even outside positive code scopes.
            if any(p in (".git", ".gitattributes", ".gitmodules") for p in parts):
                raise PolicyError("Candidate changes Git control metadata")
            if any(overlaps(path, protected) for protected in self.protected):
                raise PolicyError(f"Protected candidate path: {path}")
            if not any(within(path, allowed) for allowed in contract.write_scopes):
                raise PolicyError(f"Candidate path outside assignment: {path}")

    @instrument('git.commit', lambda self,handle,contract,**kw: dict(trace_id=contract.trace_id,
        attributes={'lease_id':contract.lease_id,'role':contract.ticket['role']}))
    def capture(self, handle: Worktree, contract: TaskContract, *, termination_evidence: str) -> Candidate:
        if not isinstance(termination_evidence, str) or not termination_evidence.strip():
            raise PolicyError("Trusted confirmation that the writer stopped is required")
        if contract.ticket["phase"] != "implement" or handle.contract_digest != contract.binding:
            raise PolicyError("Only the bound implementation assignment may create a candidate")
        key = "commit:" + contract.lease_id
        request = {"contract": contract.binding, "handle": asdict(handle),
                   "termination_evidence": termination_evidence, "protected": self.protected}
        with self.git.lock():
            path = self.worktrees.validate(handle)
            old = self.journal.begin(key, request)
            if old is not None:
                candidate = Candidate.load(old)
                self.verify(candidate, contract)
                if self.git.run(path, "rev-parse", "HEAD").decode().strip() != candidate.commit:
                    raise PolicyError("Candidate worktree HEAD changed after capture")
                return candidate
            self.git.safe_config(self.worktrees.repository)
            head = self.git.run(path, "rev-parse", "HEAD").decode().strip()
            if head != contract.base_commit or head != handle.base_commit:
                raise PolicyError("Worker changed HEAD or assignment base")
            # Include staged changes, deletions and ignored/untracked files in the
            # preflight; checking only `git diff` would omit newly created files.
            changed = self._paths(self.git.run(path, "diff", "--no-ext-diff", "--no-textconv",
                                               "--no-renames", "--name-only", "-z", head, "--")
                                  + self.git.run(path, "ls-files", "--others", "-z"))
            self._check_paths(changed, contract)
            for name in changed:
                item = checked_path(path / name)
                if item.exists() and not stat.S_ISREG(item.stat().st_mode):
                    raise PolicyError("Only regular candidate files are supported")
            self.git.run(path, "add", "--all", "--", ".")
            changed = self._paths(self.git.run(path, "diff", "--cached", "--no-ext-diff", "--no-textconv",
                                               "--no-renames", "--name-only", "-z", head, "--"))
            self._check_paths(changed, contract)
            tree = object_id(self.git.run(path, "write-tree").decode().strip())
            enforce_diff(contract.ticket["role"], self.git.run(path, "diff", "--cached", "--name-status",
                         "--no-renames", "-z", head, "--"))
            self.worktrees._check_tree(tree)
            message = (f"Task {contract.ticket['task_id']}\n\n"
                       f"AI-Lease: {contract.lease_id}\nAI-Contract: {contract.binding}\n"
                       f"AI-Trace: {contract.trace_id}\n").encode()
            # commit-tree never invokes candidate hooks or an editor. The commit
            # has exactly one pinned parent; the worker cannot smuggle in history.
            commit = object_id(self.git.run(path, "commit-tree", tree, "-p", head,
                                            input=message).decode().strip())
            candidate = Candidate(contract.binding, contract.lease_id, head, commit, tree,
                                  changed, contract.trace_id)
            self.journal.checkpoint(key, {"candidate": asdict(candidate)})
            self.git.run(path, "update-ref", "--no-deref", "HEAD", commit, head)
            ref = f"refs/ai-team/candidates/{contract.project}/{contract.lease_id}"
            self.git.run(path, "update-ref", ref, commit, "0" * len(commit))
            self.journal.finish(key, asdict(candidate))
            return candidate

    def verify(self, candidate: Candidate, contract: TaskContract) -> None:
        """Recompute immutable Git facts instead of trusting an artifact payload."""
        if (candidate.contract_digest != contract.binding or candidate.lease_id != contract.lease_id
                or candidate.base_commit != contract.base_commit or candidate.trace_id != contract.trace_id):
            raise PolicyError("Candidate belongs to a different assignment")
        repository = self.worktrees.repository
        object_id(candidate.commit); object_id(candidate.tree)
        parents = self.git.run(repository, "rev-list", "--parents", "-n", "1", candidate.commit).decode().split()
        if parents != [candidate.commit, candidate.base_commit]:
            raise PolicyError("Candidate has unexpected ancestry")
        tree = self.git.run(repository, "rev-parse", candidate.commit + "^{tree}").decode().strip()
        if tree != candidate.tree:
            raise PolicyError("Candidate tree mismatch")
        changed = self._paths(self.git.run(repository, "diff", "--no-ext-diff", "--no-textconv",
                                           "--no-renames", "--name-only", "-z",
                                           candidate.base_commit, candidate.commit, "--"))
        if changed != candidate.changed_files:
            raise PolicyError("Candidate changed-file list mismatch")
        self._check_paths(changed, contract)
        self.worktrees._check_tree(candidate.commit)

    def initialize_target(self, project: str, base_commit: str) -> str:
        from ..models import identifier
        identifier(project)
        ref = f"refs/heads/ai-team/{project}"
        base_commit = object_id(base_commit)
        with self.git.lock():
            # update-ref with an all-zero old value refuses existing branches.
            self.git.run(self.worktrees.repository, "update-ref", ref, base_commit, "0" * len(base_commit))
        return ref

    @instrument('git.merge', lambda self,candidate,contract,**kw: dict(trace_id=contract.trace_id,
        attributes={'lease_id':contract.lease_id,'artifact_digest':candidate.artifact_digest}))
    def integrate(self, candidate: Candidate, contract: TaskContract, *, target_ref: str,
                  expected_head: str, required_checks: tuple[str, ...],
                  verify_merged: Callable[[Path, str, str, tuple[str, ...]], dict],
                  verification_budget_cents: int = 0, acceptance_policy: dict | None = None,
                  conflict_resolver: Callable | None = None, max_conflict_attempts: int = 3,
                  publication_guard: Callable | None = None) -> dict:
        """Merge an already accepted candidate, verify exact merge, CAS the ref.

        Caller must authenticate acceptance BEFORE calling (Coordinator does).
        Callback is a trusted adapter, never a model verdict; it must bound its
        execution and return after stopping its verifier. Only zero-provider-cost
        integration checks are admitted in this slice; paid checks require a shared
        reservation adapter in a subsequent release. No worker funds are borrowed.
        """
        if target_ref != f"refs/heads/ai-team/{contract.project}":
            raise PolicyError("Integration may only update the project's dedicated ref")
        object_id(expected_head)
        integer(verification_budget_cents, "verification budget")
        if verification_budget_cents != 0:
            raise PolicyError("Paid integration checks require a shared reservation adapter; this release admits zero-cost checks only")
        if not required_checks or len(set(required_checks)) != len(required_checks):
            raise PolicyError("Integration requires an explicit unique check list")
        request = {"artifact": candidate.artifact_digest, "target": target_ref, "base": expected_head,
                   "checks": required_checks, "budget_cents": verification_budget_cents,
                   "acceptance_policy": acceptance_policy, "max_conflict_attempts": max_conflict_attempts}
        key = "merge:" + digest(request)
        # The physical workspace name is independent of check/policy changes.
        # All requests sharing it must share exclusion AND one durable owner.
        workspace_key = "merge-workspace:" + digest({"project": contract.project,
            "lease": contract.lease_id, "base_prefix": expected_head[:12]})
        with self.git.lock(operation=workspace_key):
            with self.git.lock():
                # Malformed input must not claim the workspace: no external
                # action has started, so a corrected candidate remains admissible.
                self.git.safe_config(self.worktrees.repository)
                self.verify(candidate, contract)
                owner = self.journal.begin(workspace_key, {"merge_key": key})
                if owner is None:
                    self.journal.finish(workspace_key, {"merge_key": key})
                record = self.journal.get(key)
                # Crash after successful ref CAS but before SQLite completion: the
                # checkpoint already contains the verified exact target and receipt.
                # A later accepted merge may already have advanced the target again.
                if record and record["status"] == "pending" and record["checkpoint"]:
                    checkpoint = record["checkpoint"]
                    if checkpoint.get("ready_to_publish"):
                        actual = self.worktrees.resolve(target_ref)
                        ancestor = self.git.run(self.worktrees.repository, "merge-base", checkpoint["commit"],
                                                actual, allowed=(0, 1)).decode().strip()
                        if ancestor == checkpoint["commit"]:
                            return self.journal.finish(key, checkpoint)
                        raise RecoveryRequired("Verified merge pending; inspect ref before retrying: " + key)
                conflict_case = (record or {}).get("checkpoint") or {}
                conflict_case = conflict_case.get("conflict_case")
                # A conflict checkpoint is resumable only via its durable attempt
                # journal. Other pending states remain explicit recovery incidents.
                old = None if conflict_case else self.journal.begin(key, request)
                if old is not None:
                    actual = self.worktrees.resolve(target_ref)
                    ancestor = self.git.run(self.worktrees.repository, "merge-base", old["commit"],
                                            actual, allowed=(0, 1)).decode().strip()
                    if ancestor != old["commit"]:
                        raise RecoveryRequired("Previously integrated revision is absent from target history")
                    return old
                if self.worktrees.resolve(target_ref) != expected_head:
                    raise PolicyError("Integration target moved; replan against its current SHA")
                listing = self.git.run(self.worktrees.repository, "worktree", "list", "--porcelain").decode()
                if "branch " + target_ref + "\n" in listing:
                    raise PolicyError("Integration target must not be checked out elsewhere")
                from .conflicts import ConflictManager
                conflicts = ConflictManager(self.worktrees)
                if conflict_case:
                    path = Path(conflict_case["workspace"])
                else:
                    handle = self.worktrees._create_locked(contract, purpose="merge", base_commit=expected_head)
                    path = Path(handle.path)
                    try:
                        self.git.run(path, "merge", "--no-commit", "--no-ff", "--no-verify", candidate.commit)
                    except GitError:
                        if not self.git.run(path, "ls-files", "--unmerged", "-z"):
                            raise
                        conflict_case = conflicts.capture(path, contract=contract, ours=expected_head,
                            theirs=candidate.commit, merge_key=key, max_attempts=max_conflict_attempts)
                        self._check_paths(tuple(item["path"] for item in conflict_case["files"]), contract)
                        self.journal.checkpoint(key, {"conflict_case": conflict_case})
            resolution = None
            if conflict_case:
                if conflict_resolver is None:
                    diff = self.git.run(path, "diff", "--no-ext-diff", "--no-textconv", "--cc").decode("utf-8", "replace")
                    raise MergeConflict(str(path), diff[:65536], conflicts.prompt(conflict_case, 1))
                resolution = conflicts.resolve(conflict_case, conflict_resolver)
                # A repaired merge introduces new material after candidate review.
                # Its exact merge tree MUST receive fresh independent review.
                required_checks = tuple(dict.fromkeys((*required_checks, "conflict_resolution", "review", "security", "independence")))
                repaired_paths = self._paths(self.git.run(path, "diff", "--cached", "--no-renames",
                    "--name-only", "-z", expected_head, "--"))
                self._check_paths(repaired_paths, contract)
            tree = object_id(self.git.run(path, "write-tree").decode().strip())
            self.worktrees._check_tree(tree)
            commit = object_id(self.git.run(path, "commit-tree", tree, "-p", expected_head,
                                            "-p", candidate.commit,
                                            input=f"Integrate {contract.lease_id}\n".encode()).decode().strip())
            self.git.run(path, "update-ref", "--no-deref", "HEAD", commit, expected_head)
            # Git commit/tree IDs bind evidence; the callback receives no publisher
            # credentials and must execute checks in its own restricted runtime.
            evidence = verify_merged(path, commit, expected_head, required_checks)
            self.journal.checkpoint(key, {"commit": commit, "evidence": evidence})
            integer(evidence.get("actual_cost_cents"), "actual verification cost")
            if (evidence.get("commit") != commit or evidence.get("tree") != tree
                    or evidence.get("expected_head") != expected_head
                    or not isinstance(evidence.get("evidence"), list) or not evidence["evidence"]
                    or not all(isinstance(x, str) and x.strip() for x in evidence["evidence"])
                    or evidence["actual_cost_cents"] > verification_budget_cents):
                raise PolicyError("Merged revision lacks bound passing evidence or exceeds its budget")
            from ..verification.acceptance_gate import AcceptanceGate
            gate = AcceptanceGate().evaluate(subject_digest=digest({"commit": commit, "tree": tree}),
                checks=evidence.get("checks", {}), required_checks=required_checks, policy=acceptance_policy,
                critical_checks=("conflict_resolution", "review", "security", "independence") if resolution else ()).require()
            result = {"ready_to_publish": True, "commit": commit, "tree": tree,
                      "previous": expected_head, "target": target_ref,
                      "artifact_digest": candidate.artifact_digest, "evidence": evidence,
                      "acceptance_gate_result": gate.to_dict(), "conflict_resolution": resolution}
            with self.git.lock(), (publication_guard() if publication_guard else nullcontext()):
                # Evidence authorizes exactly this immutable merge and base.
                # Recheck mutable host facts after the external callback returns.
                self.git.safe_config(self.worktrees.repository)
                self.verify(candidate, contract)
                if self.worktrees.resolve(target_ref) != expected_head:
                    raise PolicyError("Integration target moved during verification; replan and reverify")
                listing = self.git.run(self.worktrees.repository, "worktree", "list", "--porcelain").decode()
                if "branch " + target_ref + "\n" in listing:
                    raise PolicyError("Integration target was checked out during verification")
                if (self.git.run(path, "rev-parse", "HEAD").decode().strip() != commit
                        or self.git.run(path, "write-tree").decode().strip() != tree
                        or self.git.run(path, "diff", "--no-ext-diff", "--no-textconv", "--name-only", "HEAD", "--").strip()
                        or self.git.run(path, "ls-files", "--others", "-z").strip()):
                    raise PolicyError("Verifier changed the bound integration workspace")
                self.journal.checkpoint(key, result)
                # This operation is atomic in Git. A stale head cannot be overwritten.
                self.git.run(self.worktrees.repository, "update-ref", target_ref, commit, expected_head)
                return self.journal.finish(key, result)
