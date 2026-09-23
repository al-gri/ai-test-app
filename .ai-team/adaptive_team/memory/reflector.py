"""Reflect accepted Team evidence into a bounded problem -> solution record.

A summarizer is optional and untrusted. Its output cannot change attribution,
artifact, role, permissions or acceptance. No external model is called by default.
Claims prevent duplicate paid summarization after uncertain callback outcomes.
"""
from dataclasses import asdict
import json
from contextlib import closing

from ..models import PolicyError, canonical, digest
from ..orchestration.task_lifecycle import RecoveryRequired
from .vector_store import Lesson, VectorStore


class Reflector:
    def __init__(self, team, store: VectorStore, summarizer=None):
        self.team, self.store, self.summarizer = team, store, summarizer

    def reflect(self, task_id: str):
        epoch = self.store.state.epoch
        state = self.team.snapshot()
        task = state["tasks"].get(task_id)
        if state["project"] != self.store.state.project or task is None or task["status"] != "accepted" or not task["result"]:
            raise PolicyError("Reflection requires an accepted task from this project")
        result = task["result"]
        lease = state["leases"][result["lease"]]
        lesson_id = digest({"project": state["project"], "task": task_id, "artifact": result["artifact_digest"],
                            "receipt": lease["receipt_digest"]})
        facts = {"task": task_id, "title": task["spec"]["title"], "acceptance": task["spec"]["acceptance"],
                 "artifact": result["artifact_digest"], "checks": result["checks"], "evidence": result["evidence"]}
        state_store = self.store.state
        with state_store.locked():
            if state_store._control()["epoch"] != epoch:
                raise PolicyError("Rollback invalidated reflection")
            # External invocation fences survive learning rollback. A forgotten
            # lesson must not cause an uncertain/paid call to execute again.
            with closing(state_store.connect(state_store.control)) as db:
                row = db.execute("SELECT value FROM reflection_claims WHERE id=?", (lesson_id,)).fetchone()
                if row:
                    claim = json.loads(row[0])
                    if claim["status"] == "pending":
                        raise RecoveryRequired("Reflection outcome uncertain; reconcile before retry")
                    with closing(state_store.connect(state_store.database)) as memory:
                        saved = memory.execute("SELECT document FROM lessons WHERE id=?", (lesson_id,)).fetchone()
                    if saved is None:
                        raise RecoveryRequired("Lesson was forgotten by rollback; do not automatically relearn it")
                    return Lesson.load(json.loads(saved[0]))
                if db.execute("SELECT COUNT(*) FROM reflection_claims").fetchone()[0] >= 10000:
                    raise PolicyError("Reflection operation ledger capacity reached")
                db.execute("INSERT INTO reflection_claims VALUES(?,?)", (lesson_id,
                    canonical({"status": "pending", "facts_digest": digest(facts), "epoch": epoch})))
        if self.summarizer:
            # Trusted adapters own timeout, termination, redaction and spend.
            proposal = self.summarizer(json.loads(canonical(facts)))
        else:
            proposal = {"problem": facts["title"], "solution": "Accepted artifact " + facts["artifact"]
                + "; verified checks: " + ", ".join(sorted(k for k, v in facts["checks"].items() if v == "PASS")),
                "applicability": "Consult the linked evidence and revalidate against the new task; acceptance is project-specific.",
                "tags": []}
        if not isinstance(proposal, dict) or set(proposal) != {"problem", "solution", "applicability", "tags"} or not isinstance(proposal["tags"], list):
            raise PolicyError("Reflector proposal contains unsupported fields")
        lesson = Lesson(lesson_id, state["project"], task["spec"]["role"], proposal["problem"], proposal["solution"],
            proposal["applicability"], tuple(result["evidence"][:20]), task_id, result["artifact_digest"],
            lease["inputs"]["prompt_digest"], tuple(proposal["tags"]))
        values = self.store.prepare(lesson)
        with self.store.state.transaction(expected_epoch=epoch) as db:
            self.store._put(db, lesson, values)
        # A crash between these commits conservatively retains a pending claim.
        # Recovery can compare the saved lesson, but must not repeat the callback.
        with state_store.locked():
            if state_store._control()["epoch"] != epoch:
                raise PolicyError("Rollback invalidated reflection completion")
            with closing(state_store.connect(state_store.control)) as db:
                db.execute("UPDATE reflection_claims SET value=? WHERE id=?",
                    (canonical({"status": "complete", "facts_digest": digest(facts), "epoch": epoch}), lesson_id))
        return lesson
