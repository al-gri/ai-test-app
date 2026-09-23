"""Coordinator-owned detached worktrees, never arbitrary user directories.

Workers receive file access through an isolated backend. They must not receive
the Git common directory, control state, or these management APIs.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from ..models import PolicyError, digest, identifier, scope
from ..orchestration.task_lifecycle import TaskContract, TaskLifecycle, object_id
from ._git import Git, checked_path


@dataclass(frozen=True)
class Worktree:
    path: str
    base_commit: str
    contract_digest: str
    operation_key: str


class WorktreeManager:
    def __init__(self, repository: str | Path, workspace_root: str | Path,
                 control_root: str | Path):
        self.repository = checked_path(repository)
        self.root, self.control = checked_path(workspace_root), checked_path(control_root)
        if not self.repository.is_dir():
            raise PolicyError("Repository does not exist")
        # A candidate's directory can never contain the controller or source repo.
        for a, b in ((self.root, self.repository), (self.control, self.repository), (self.root, self.control)):
            if a == b or a in b.parents or b in a.parents:
                raise PolicyError("Repository, workspaces, and control roots must be disjoint")
        self.root.mkdir(parents=True, exist_ok=True)
        self.git = Git(self.control)
        self.git.safe_config(self.repository)
        top = Path(self.git.run(self.repository, "rev-parse", "--show-toplevel").decode().strip())
        if checked_path(top) != self.repository:
            raise PolicyError("Use the exact repository root")
        common = self.git.run(self.repository, "rev-parse", "--path-format=absolute", "--git-common-dir").decode().strip()
        self.common = checked_path(common)
        self.journal = TaskLifecycle(self.control / "operations.sqlite")
        binding = {"repository": str(self.repository), "common": str(self.common), "workspaces": str(self.root)}
        with self.git.lock():
            previous = self.journal.begin("manager-binding", binding)
            if previous is None:
                self.journal.finish("manager-binding", binding)

    def resolve(self, revision: str = "HEAD") -> str:
        # Ref names here are trusted configuration, never arbitrary command text.
        if not revision or revision.startswith("-") or any(c in revision for c in "\x00\n\r"):
            raise PolicyError("Invalid Git revision")
        return object_id(self.git.run(self.repository, "rev-parse", "--verify", "--end-of-options",
                                      revision + "^{commit}").decode().strip())

    def _check_tree(self, commit: str) -> None:
        entries = self.git.run(self.repository, "ls-tree", "-rz", "--full-tree", object_id(commit))
        names = set()
        for entry in entries.split(b"\0"):
            if not entry:
                continue
            meta, raw_name = entry.split(b"\t", 1)
            mode = meta.split()[0]
            name = raw_name.decode("utf-8", "strict")
            scope(name)
            if mode not in (b"100644", b"100755"):
                raise PolicyError("This backend rejects symlink and submodule trees")
            if any(part.casefold() == ".git" for part in name.split("/")):
                raise PolicyError("Git metadata path in candidate tree")
            if name.casefold() in names:
                raise PolicyError("Case-aliased repository paths are unsupported")
            names.add(name.casefold())

    def create(self, contract: TaskContract, *, purpose: str = "worker",
               base_commit: str | None = None) -> Worktree:
        with self.git.lock():
            return self._create_locked(contract, purpose=purpose, base_commit=base_commit)

    def _create_locked(self, contract: TaskContract, *, purpose: str = "worker",
                       base_commit: str | None = None) -> Worktree:
        identifier(purpose, "worktree purpose")
        base = object_id(base_commit or contract.base_commit)
        name = f"{contract.lease_id}-{purpose}-{base[:12]}"
        path = checked_path(self.root / contract.project / name)
        key = "worktree:" + digest({"contract": contract.binding, "purpose": purpose, "base": base})
        request = {"path": str(path), "base_commit": base, "contract_digest": contract.binding,
                   "operation_key": key}
        old = self.journal.begin(key, request)
        if old is not None:
            handle = Worktree(**old)
            self.validate(handle)
            return handle
        if path.exists():
            raise PolicyError("Will not adopt or overwrite an existing directory")
        self.git.safe_config(self.repository)
        self._check_tree(base)
        path.parent.mkdir(parents=True, exist_ok=True)
        # --no-checkout avoids executing checkout filters before tree admission.
        # Reset only touches the fresh, coordinator-owned worktree just created.
        self.git.run(self.repository, "worktree", "add", "--detach", "--no-checkout", str(path), base)
        self.git.run(path, "reset", "--hard", base)
        self.journal.finish(key, request)
        return Worktree(**request)

    def validate(self, handle: Worktree) -> Path:
        record = self.journal.get(handle.operation_key)
        if not record or record["status"] != "complete" or record["result"] != asdict(handle):
            raise PolicyError("Unregistered or modified worktree handle")
        path = checked_path(handle.path)
        if self.root not in path.parents or not path.is_dir():
            raise PolicyError("Worktree escaped its registered root or is missing")
        common = self.git.run(path, "rev-parse", "--path-format=absolute", "--git-common-dir").decode().strip()
        if checked_path(common) != self.common:
            raise PolicyError("Worktree belongs to another repository")
        actual = self.git.run(path, "rev-parse", "--show-toplevel").decode().strip()
        if checked_path(actual) != path:
            raise PolicyError("Worktree root mismatch")
        if self.git.run(path, "symbolic-ref", "-q", "HEAD", allowed=(0, 1)).strip():
            raise PolicyError("Managed worktrees must retain detached HEADs")
        return path

    def remove(self, handle: Worktree, *, termination_evidence: str) -> None:
        """Explicit retirement; never force-remove dirty/untracked user content.

        Only the trusted backend may attest termination. A worker message or an
        expired lease is not evidence that its process stopped.
        """
        if not isinstance(termination_evidence, str) or not termination_evidence.strip():
            raise PolicyError("Confirmed termination evidence required")
        with self.git.lock():
            path = self.validate(handle)
            if self.git.run(path, "status", "--porcelain=v1", "--untracked-files=all").strip():
                raise PolicyError("Retain dirty worktree for explicit reconciliation")
            self.git.run(self.repository, "worktree", "remove", str(path))

    def restore_tree(self, handle: Worktree, candidate_commit: str) -> None:
        """Seed a fresh repair directory with the previous candidate's files.

        HEAD deliberately stays on the approved integration base, so the next
        capture still validates the *entire* candidate diff, not only its repair.
        A changed base requires explicit reconciliation rather than dropping work.
        """
        candidate_commit = object_id(candidate_commit)
        with self.git.lock():
            path = self.validate(handle)
            parents = self.git.run(path, "rev-list", "--parents", "-n", "1", candidate_commit).decode().split()
            if parents != [candidate_commit, handle.base_commit]:
                raise PolicyError("Repair base moved; explicit candidate reconciliation is required")
            key = "restore:" + digest({"handle": asdict(handle), "candidate": candidate_commit})
            if self.journal.begin(key, {"handle": asdict(handle), "candidate": candidate_commit}) is not None:
                return
            if self.git.run(path, "rev-parse", "HEAD").decode().strip() != handle.base_commit:
                raise PolicyError("Repair worktree HEAD changed")
            if self.git.run(path, "status", "--porcelain=v1", "--untracked-files=all").strip():
                raise PolicyError("Will not overwrite a nonempty repair workspace")
            self.git.safe_config(self.repository)
            self._check_tree(candidate_commit)
            self.git.run(path, "read-tree", "--reset", "-u", candidate_commit)
            self.journal.finish(key, {"restored": candidate_commit})
