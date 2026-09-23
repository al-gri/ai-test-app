"""Repeated authenticated error codes -> bounded, authority-preserving revisions.

Free-form failed logs never become instructions. Corrective text comes from an
owner-defined rule catalog. Candidates flow through existing independent evals
and promotion; the supervisor does not possess evaluator/promotion credentials.
"""
from __future__ import annotations

import copy
import json
from contextvars import ContextVar
from contextlib import contextmanager

from ..evolution import Evolution, verify, attest
from ..models import PolicyError, canonical, digest, identifier
from .vector_store import bounded_text


def assert_same_authority(baseline: dict, candidate: dict):
    """Strict allowlist: only instruction text and role revision may change.

    This also freezes future sandbox/tool/permission fields not known today.
    It cannot prove arbitrary natural-language instructions harmless: independent
    behavioral evals and technical sandbox enforcement remain mandatory.
    """
    before, after = copy.deepcopy(baseline), copy.deepcopy(candidate)
    if after.get("role", {}).get("version") != before.get("role", {}).get("version", 0) + 1:
        raise PolicyError("Correction must increment exactly one role revision")
    for value in (before, after):
        value.pop("instructions", None)
        value["role"].pop("version", None)
    if before != after:
        raise PolicyError("Automatic correction cannot alter any authority or card metadata")


class LearningEvolution(Evolution):
    """Evolution stored inside the capsule and serialized with snapshot/restore."""
    def __init__(self, state, authorities, **kwargs):
        self.learning_state = state
        self._proposal_guard = ContextVar("learning_proposal_guard", default=None)
        self._epoch_guard = ContextVar("learning_evolution_epoch", default=None)
        super().__init__(state.database, authorities, **kwargs)

    @contextmanager
    def _edit(self):
        with self.learning_state.locked():
            expected_epoch = self._epoch_guard.get()
            if expected_epoch is not None and self.learning_state._control()["epoch"] != expected_epoch:
                raise PolicyError("Stale learning authorization epoch")
            with super()._edit() as state:
                guard = self._proposal_guard.get()
                if guard is not None:
                    bundle, epoch = guard
                    if self.learning_state._control()["epoch"] != epoch:
                        raise PolicyError("Rollback invalidated prompt proposal")
                    active = state["active"].get(bundle.get("role", {}).get("id"))
                    if active is None:
                        raise PolicyError("Supervisor cannot invent roles")
                    assert_same_authority(active, bundle)
                yield state

    def propose(self, bundle, author, reason, *, expected_epoch=None):
        # Automatic memory evolution is narrower than general role maintenance.
        bundle = copy.deepcopy(bundle)
        token = self._proposal_guard.set((bundle, expected_epoch or self.learning_state.epoch))
        try:
            return super().propose(bundle, author, reason)
        finally:
            self._proposal_guard.reset(token)

    def _bound(self, envelope, permission, callback):
        actor, payload = verify(envelope, self.authorities, permission)
        epoch = payload.get("epoch")
        if not isinstance(epoch, str) or epoch != self.learning_state.epoch:
            raise PolicyError("Learning authorization must bind the current epoch")
        # Preserve the legacy validator's exact payload schema after authenticating
        # the additional epoch. The lock checks that epoch again at commit time.
        legacy = attest({k: v for k, v in payload.items() if k != "epoch"}, actor, self.authorities[actor]["key"])
        token = self._epoch_guard.set(epoch)
        try:
            return callback(legacy)
        finally:
            self._epoch_guard.reset(token)

    def evaluate(self, candidate_id, envelope):
        return self._bound(envelope, "evaluate", lambda e: super(LearningEvolution, self).evaluate(candidate_id, e))

    def evaluate_lifecycle(self, candidate_id, envelope):
        return self._bound(envelope, "evaluate", lambda e: super(LearningEvolution, self).evaluate_lifecycle(candidate_id, e))

    def activate(self, candidate_id, envelope):
        return self._bound(envelope, "activate", lambda e: super(LearningEvolution, self).activate(candidate_id, e))

    def rollback(self, candidate_id, envelope):
        return self._bound(envelope, "rollback", lambda e: super(LearningEvolution, self).rollback(candidate_id, e))

    def observe(self, envelope, *, now=None):
        return self._bound(envelope, "observe", lambda e: super(LearningEvolution, self).observe(e, now=now))

    def close_audit(self, envelope, *, now=None):
        return self._bound(envelope, "audit", lambda e: super(LearningEvolution, self).close_audit(e, now=now))


class Supervisor:
    def __init__(self, state, evolution: LearningEvolution, authorities: dict, rules: dict, *, threshold=3):
        if evolution.learning_state is not state:
            raise PolicyError("Supervisor and Evolution must use the same capsule")
        if type(threshold) is not int or not 2 <= threshold <= 100:
            raise PolicyError("Repeated-error threshold must be 2..100")
        if not isinstance(rules, dict) or not 1 <= len(rules) <= 100:
            raise PolicyError("Owner correction catalog required")
        for code, rule in rules.items():
            identifier(code, "failure code"); bounded_text(rule, "corrective rule", 1000)
        self.state, self.evolution, self.authorities = state, evolution, authorities
        self.rules, self.threshold = copy.deepcopy(rules), threshold

    def observe(self, envelope):
        actor, event = verify(envelope, self.authorities, "observe_failures")
        fields = {"id", "project", "task_id", "attempt_id", "role_id", "prompt_digest", "code", "gate", "evidence_digest", "epoch"}
        if set(event) != fields or event["project"] != self.state.project or event["code"] not in self.rules:
            raise PolicyError("Invalid failure event or unknown correction code")
        for field in ("id", "task_id", "attempt_id", "role_id", "code", "gate"):
            identifier(event[field], field)
        import re
        if any(not isinstance(event[k], str) or not re.fullmatch(r"[0-9a-f]{64}", event[k]) for k in ("prompt_digest", "evidence_digest")):
            raise PolicyError("Failure requires exact prompt and evidence hashes")
        with self.state.transaction(expected_epoch=event["epoch"]) as db:
            value = canonical({"actor": actor, **event})
            old = db.execute("SELECT value FROM failures WHERE id=?", (event["id"],)).fetchone()
            if old and old[0] != value:
                raise PolicyError("Conflicting failure event replay")
            if not old and db.execute("SELECT COUNT(*) FROM failures").fetchone()[0] >= 10000:
                raise PolicyError("Failure memory capacity reached")
            db.execute("INSERT OR IGNORE INTO failures VALUES(?,?)", (event["id"], value))

    def propose(self, role_id: str):
        epoch = self.state.epoch
        baseline = self.evolution.snapshot()["active"].get(role_id)
        if baseline is None:
            raise PolicyError("Unknown correction role")
        baseline_digest = digest(baseline)
        with self.state.transaction(expected_epoch=epoch) as db:
            events = [json.loads(row[0]) for row in db.execute("SELECT value FROM failures ORDER BY id")]
        proposals = []
        for code, rule in sorted(self.rules.items()):
            matches = [event for event in events if event["role_id"] == role_id and event["prompt_digest"] == baseline_digest and event["code"] == code]
            # A repeated event or multiple reports about one attempt count once.
            attempts = {(e["task_id"], e["attempt_id"]) for e in matches}
            marker = "[corrective-rule:" + digest({"code": code, "rule": rule}) + "]"
            if len(attempts) < self.threshold or marker in baseline["instructions"]:
                continue
            candidate = copy.deepcopy(baseline)
            candidate["role"]["version"] += 1
            candidate["instructions"] += "\n\n" + marker + "\n" + rule + "\n"
            if len(candidate["instructions"]) > 24000:
                raise PolicyError("Prompt growth bound exceeded")
            assert_same_authority(baseline, candidate)
            # propose is local and deterministic; no model callback or remote
            # charge can be duplicated if the process stops before recording it.
            proposed = self.evolution.propose(candidate, "memory-supervisor", "Repeated verified failure: " + code,
                                               expected_epoch=epoch)
            with self.state.transaction(expected_epoch=epoch) as db:
                db.execute("INSERT OR IGNORE INTO corrections VALUES(?,?)", (proposed["id"], canonical({
                    "candidate_id": proposed["id"], "code": code, "baseline_digest": baseline_digest,
                    "attempts": sorted(attempts), "evidence_digest": digest(matches)})))
            proposals.append(proposed)
        return proposals
