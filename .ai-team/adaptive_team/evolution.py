"""Evidence-gated role/prompt revisions. Called only by a trusted control service.

No LLM calls or self-grading here. Keys and evaluator datasets live outside workers
and outside the repository. HMAC authenticates a report, not its scientific quality.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import math
import re
import sqlite3
import time
from contextlib import closing, contextmanager
from pathlib import Path

from .models import PolicyError, canonical, digest, identifier, integer


def attest(payload: dict, actor: str, key: bytes) -> dict:
    """Trusted service helper; never expose signing keys/tools to workers."""
    body = {"actor": actor, "payload": payload}
    return {**body, "signature": hmac.new(key, canonical(body).encode(), hashlib.sha256).hexdigest()}


def verify(envelope: dict, authorities: dict[str, dict], permission: str) -> tuple[str, dict]:
    actor = envelope.get("actor")
    authority = authorities.get(actor, {})
    if permission not in authority.get("permissions", []) or not isinstance(authority.get("key"), bytes):
        raise PolicyError("Untrusted attestation authority")
    expected = attest(envelope.get("payload"), actor, authority["key"])["signature"]
    if not isinstance(envelope.get("signature"), str) or not hmac.compare_digest(expected, envelope["signature"]):
        raise PolicyError("Invalid attestation signature")
    if not isinstance(envelope.get("payload"), dict):
        raise PolicyError("Attestation payload must be an object")
    return actor, envelope["payload"]


def review_due(metrics: dict, *, now: float, last_review: float, cooldown_days=7) -> list[str]:
    """Read-only audit triggers; never retires or rewrites a role automatically."""
    if now - last_review < cooldown_days * 86400:
        return []
    reasons = []
    for field, threshold in (("completed_tasks", 25), ("repeated_failure_count", 3), ("idle_days", 90)):
        if metrics.get(field, 0) >= threshold:
            reasons.append(field)
    if metrics.get("dependency_changed"):
        reasons.append("dependency_changed")
    if now - last_review >= 30 * 86400:
        reasons.append("monthly_review")
    return reasons


class Evolution:
    def __init__(self, database: str | Path, authorities: dict[str, dict], *, min_cases=20,
                 min_gain=0.02, max_cost_ratio=1.10, max_latency_ratio=1.20, minimum_quality=0.8):
        from .llmops.quality_policy import QualityPolicy
        self.quality_policy = QualityPolicy(minimum_quality)
        self.database, self.authorities = str(database), authorities
        integer(min_cases, "min_cases", 2)
        for value in (min_gain, max_cost_ratio, max_latency_ratio):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise PolicyError("Invalid evaluation policy")
        self.policy = dict(min_cases=min_cases, min_gain=min_gain, max_cost_ratio=max_cost_ratio, max_latency_ratio=max_latency_ratio, minimum_quality=minimum_quality)

    @contextmanager
    def _edit(self):
        from .storage import relational, evolution_rows
        with closing(relational.connect(self.database)) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                state = evolution_rows.load(db,self.policy)
                before = copy.deepcopy(state)
                yield state
                evolution_rows.save(db,before,state)
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def seed(self, bundles: dict) -> None:
        with self._edit() as state:
            if state["active"]:
                raise PolicyError("Registry already seeded")
            for key, value in bundles.items():
                if value["role"]["id"] != key:
                    raise PolicyError("Role identity mismatch")
            state["active"] = copy.deepcopy(bundles)
            state["history"].append({"kind": "seed", "digest": digest(bundles)})

    def observe(self, envelope: dict, *, now=None) -> None:
        """Ingest bounded, authenticated run metadata; never raw private logs."""
        observer, item = verify(envelope, self.authorities, "observe")
        now = time.time() if now is None else now
        event_id = identifier(item.get("id"), "observation")
        if set(item) != {"id", "role_id", "at", "outcome", "receipt_digest", "prompt_digest"}:
            raise PolicyError("Observation accepts only bounded receipt metadata")
        if item["outcome"] not in {"accepted", "candidate_defect", "infrastructure_defect", "missing_evidence", "external_blocker", "dependency_changed"}:
            raise PolicyError("Invalid observation outcome")
        if type(item["at"]) not in (int, float) or not math.isfinite(item["at"]) or item["at"] < 0:
            raise PolicyError("Invalid observation time")
        if any(not isinstance(item[key], str) or not re.fullmatch(r"[0-9a-f]{64}", item[key]) for key in ("receipt_digest", "prompt_digest")):
            raise PolicyError("Observation requires exact receipt and prompt hashes")
        with self._edit() as state:
            if item["role_id"] not in state["active"]:
                raise PolicyError("Unknown observed role")
            previous = state["observations"].get(event_id)
            value = {**item, "observer": observer}
            if previous is not None:
                if {key: data for key, data in previous.items() if key not in {"sequence", "ingested_at"}} != value:
                    raise PolicyError("Conflicting duplicate observation")
                return
            if type(now) not in (int, float) or not math.isfinite(now) or now < item["at"]:
                raise PolicyError("Observation is ahead of trusted controller time")
            if len(state["observations"]) >= 10000 and previous is None:
                raise PolicyError("Archive project observations through reviewed maintenance before adding more")
            state["observations"][event_id] = {**value, "sequence": len(state["observations"]) + 1, "ingested_at": now}

    def audit_queue(self, *, now=None) -> list[dict]:
        now = time.time() if now is None else now
        with self._edit() as state:
            result = []
            for role_id in state["active"]:
                last = state["audits"].get(role_id, {}).get("at", 0)
                checkpoint = state["audits"].get(role_id, {}).get("through_sequence", 0)
                observations = [x for x in state["observations"].values() if x["role_id"] == role_id and x["sequence"] > checkpoint and x["ingested_at"] <= now]
                last_use = max([x["at"] for x in state["observations"].values() if x["role_id"] == role_id and x["at"] <= now] or [now])
                metrics = {"completed_tasks": sum(x["outcome"] == "accepted" for x in observations),
                           "repeated_failure_count": sum(x["outcome"] == "candidate_defect" for x in observations),
                           "dependency_changed": any(x["outcome"] == "dependency_changed" for x in observations),
                           "idle_days": (now - last_use) // 86400}
                reasons = review_due(metrics, now=now, last_review=last)
                if reasons:
                    result.append({"role_id": role_id, "reasons": reasons, "metrics": metrics, "observations_digest": digest(observations)})
            return result

    def close_audit(self, envelope: dict, *, now=None) -> None:
        actor, report = verify(envelope, self.authorities, "audit")
        now = time.time() if now is None else now
        role_id = report.get("role_id")
        if type(report.get("at")) not in (int, float) or not math.isfinite(report["at"]) or report["at"] < 0 or report["at"] > now or not report.get("conclusion") or not report.get("evidence_refs"):
            raise PolicyError("Audit needs timestamp, conclusion and evidence")
        with self._edit() as state:
            if role_id not in state["active"] or report["at"] <= state["audits"].get(role_id, {}).get("at", 0):
                raise PolicyError("Unknown role or stale audit")
            checkpoint = state["audits"].get(role_id, {}).get("through_sequence", 0)
            observations = [x for x in state["observations"].values() if x["role_id"] == role_id and x["sequence"] > checkpoint and x["ingested_at"] <= report["at"]]
            if report.get("observations_digest") != digest(observations):
                raise PolicyError("Audit must bind the exact observations reviewed")
            state["audits"][role_id] = {"at": report["at"], "through_sequence": max([x["sequence"] for x in observations] or [checkpoint]), "actor": actor, "receipt": copy.deepcopy(envelope)}
            state["history"].append({"kind": "audited", "role_id": role_id, "receipt_digest": digest(envelope)})

    def snapshot(self) -> dict:
        with self._edit() as state:
            return copy.deepcopy(state)

    def propose(self, bundle: dict, author: str, reason: str) -> dict:
        role = bundle.get("role", {})
        role_id = identifier(role.get("id"), "role")
        if not author or not reason or not isinstance(bundle.get("instructions"), str) or not bundle["instructions"].strip():
            raise PolicyError("Candidate needs author, rationale and prompt")
        if role.get("status") not in {"trial", "active", "deprecated", "retired"}:
            raise PolicyError("Unknown lifecycle status")
        integer(role.get("version"), "version", 1)
        capabilities = role.get("capabilities")
        if not isinstance(capabilities, list) or not capabilities or set(capabilities) - {"read", "write", "test", "review", "plan"}:
            raise PolicyError("Invalid capabilities")
        with self._edit() as state:
            baseline = state["active"].get(role_id)
            if not state["active"]:
                raise PolicyError("Seed trusted common instructions first")
            common = baseline or next(iter(state["active"].values()))
            if set(bundle) != set(common) or any(bundle[key] != common[key] for key in ("contract", "implement", "review")):
                raise PolicyError("Role evolution cannot change shared authority or execution contracts")
            if baseline and role["version"] != baseline["role"]["version"] + 1:
                raise PolicyError("Role revision must increment exactly once")
            if not baseline and (role["version"] != 1 or role["status"] != "trial"):
                raise PolicyError("New roles start at trial version 1")
            proposal = {"role_id": role_id, "baseline_digest": digest(baseline), "candidate_digest": digest(bundle),
                        "bundle": copy.deepcopy(bundle), "author": author, "reason": reason, "status": "proposed"}
            candidate_id = digest(proposal)
            if candidate_id not in state["candidates"]:
                state["candidates"][candidate_id] = proposal
            return {"id": candidate_id, **copy.deepcopy(state["candidates"][candidate_id])}

    def evaluate(self, candidate_id: str, envelope: dict) -> dict:
        evaluator, report = verify(envelope, self.authorities, "evaluate")
        with self._edit() as state:
            candidate = state["candidates"].get(candidate_id)
            if not candidate or candidate["status"] != "proposed" or candidate["author"] == evaluator:
                raise PolicyError("Unknown candidate, invalid state or non-independent evaluator")
            for field in ("baseline_digest", "candidate_digest"):
                if report.get(field) != candidate[field]:
                    raise PolicyError("Evaluation bound to wrong revision")
            if report.get("candidate_id") != candidate_id or report.get("held_out") is not True or report.get("trusted_checks") is not True:
                raise PolicyError("Held-out trusted evaluation bound to proposal required")
            if any(not isinstance(report.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", report[key]) for key in ("conditions_digest", "dataset_digest")) or not isinstance(report.get("evidence_refs"), list) or not report["evidence_refs"]:
                raise PolicyError("Model/tools/budgets, hidden dataset and raw evidence bindings required")
            cases = report.get("cases", [])
            if not isinstance(cases, list) or len(cases) < self.policy["min_cases"]:
                raise PolicyError("Insufficient evaluation cases")
            quality = self.quality_policy.evaluate(cases)
            seen, gains = set(), []
            total = {k: 0.0 for k in ("baseline_cost", "candidate_cost", "baseline_latency", "candidate_latency")}
            for case in cases:
                if not isinstance(case.get("id"), str) or not case["id"] or case["id"] in seen:
                    raise PolicyError("Invalid or duplicate held-out case")
                seen.add(case["id"])
                for key in ("baseline", "candidate", *total):
                    value = case.get(key)
                    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                        raise PolicyError("Invalid score/cost/latency")
                if max(case["baseline"], case["candidate"]) > 1 or case.get("critical_pass") is not True:
                    raise PolicyError("Critical regression or unbounded quality score")
                if case["candidate"] < case["baseline"]:
                    raise PolicyError("Per-case quality regression; expand/revise candidate")
                gains.append(case["candidate"] - case["baseline"])
                for key in total:
                    total[key] += case[key]
            gain = sum(gains) / len(gains)
            # Transparent conservative paired normal bound, not a universal
            # significance guarantee. Backend owns sampling independence/repeats.
            variance = sum((g - gain) ** 2 for g in gains) / (len(gains) - 1)
            lower_bound = gain - 1.96 * math.sqrt(variance / len(gains))
            efficiency_gain = (total["baseline_cost"] > 0 and total["candidate_cost"] <= total["baseline_cost"] * 0.9
                               and total["candidate_latency"] <= total["baseline_latency"])
            if lower_bound < self.policy["min_gain"] and not (min(gains) >= 0 and efficiency_gain):
                raise PolicyError("Neither supported quality gain nor >=10% cost gain without regression")
            if total["candidate_cost"] > total["baseline_cost"] * self.policy["max_cost_ratio"] or total["candidate_latency"] > total["baseline_latency"] * self.policy["max_latency_ratio"]:
                raise PolicyError("Cost/latency regression beyond policy")
            candidate.update(status="evaluated", evaluation_digest=digest(envelope), evaluator=evaluator,
                             evaluation=copy.deepcopy(envelope), gain=gain, lower_bound=lower_bound, quality=quality)
            return {"candidate_id": candidate_id, "status": "evaluated", "gain": gain, "lower_bound": lower_bound}

    def evaluate_lifecycle(self, candidate_id: str, envelope: dict) -> dict:
        """Status-only changes use workload/coverage evidence, not fake score gains."""
        evaluator, report = verify(envelope, self.authorities, "evaluate")
        with self._edit() as state:
            candidate = state["candidates"].get(candidate_id)
            if not candidate or candidate["status"] != "proposed" or candidate["author"] == evaluator:
                raise PolicyError("Independent lifecycle evaluation required")
            baseline = state["active"].get(candidate["role_id"])
            if not baseline or digest(baseline) != candidate["baseline_digest"]:
                raise PolicyError("Lifecycle transition needs a current existing baseline")
            before, after = copy.deepcopy(baseline), copy.deepcopy(candidate["bundle"])
            for value in (before, after):
                for key in ("status", "version"):
                    value["role"].pop(key)
            if before != after or baseline["role"]["status"] == candidate["bundle"]["role"]["status"]:
                raise PolicyError("Lifecycle evaluation cannot conceal instruction/capability changes")
            if any(report.get(key) != candidate[key] for key in ("baseline_digest", "candidate_digest")) or report.get("candidate_id") != candidate_id:
                raise PolicyError("Lifecycle evidence bound to wrong proposal")
            if any(report.get(key) is not True for key in ("coverage_preserved", "replacement_validated", "rare_risks_reviewed", "migration_ready")) or not report.get("evidence_refs"):
                raise PolicyError("Lifecycle needs coverage, rare-risk, replacement and migration evidence")
            candidate.update(status="evaluated", evaluation_digest=digest(envelope), evaluator=evaluator,
                             evaluation=copy.deepcopy(envelope), evaluation_kind="lifecycle")
            return {"candidate_id": candidate_id, "status": "evaluated"}

    def activate(self, candidate_id: str, envelope: dict) -> dict:
        reviewer, review = verify(envelope, self.authorities, "activate")
        with self._edit() as state:
            candidate = state["candidates"].get(candidate_id)
            if not candidate or candidate["status"] != "evaluated" or reviewer in {candidate["author"], candidate["evaluator"]}:
                raise PolicyError("Evaluated candidate and independent promotion authority required")
            if review.get("candidate_id") != candidate_id or review.get("evaluation_digest") != candidate["evaluation_digest"] or review.get("verdict") != "approve" or not review.get("evidence_refs"):
                raise PolicyError("Review not bound to complete evidence")
            previous = state["active"].get(candidate["role_id"])
            if digest(previous) != candidate["baseline_digest"]:
                raise PolicyError("Stale baseline; rebase and evaluate again")
            # Capability/role lifecycle is maintenance authority, not ordinary
            # prompt editing. Explicit permission must be provisioned off-repo.
            role = candidate["bundle"]["role"]
            if (previous is None or role["status"] != previous["role"]["status"] or role["capabilities"] != previous["role"]["capabilities"]):
                if "roles" not in self.authorities[reviewer]["permissions"] or review.get("lifecycle_reviewed") is not True:
                    raise PolicyError("Lifecycle/capability change needs role governance authority")
            state["active"][candidate["role_id"]] = copy.deepcopy(candidate["bundle"])
            candidate.update(status="activated", predecessor=previous, promotion=copy.deepcopy(envelope))
            state["history"].append({"kind": "activated", "candidate_id": candidate_id, "reviewer": reviewer})
            return copy.deepcopy(state["active"][candidate["role_id"]])

    def rollback(self, candidate_id: str, envelope: dict) -> None:
        actor, receipt = verify(envelope, self.authorities, "rollback")
        with self._edit() as state:
            candidate = state["candidates"].get(candidate_id)
            if not candidate or candidate["status"] != "activated" or receipt.get("candidate_id") != candidate_id or not receipt.get("reason"):
                raise PolicyError("Invalid rollback")
            if digest(state["active"].get(candidate["role_id"])) != candidate["candidate_digest"]:
                raise PolicyError("Cannot roll back over a newer revision")
            if candidate["predecessor"] is None:
                # Retain a tombstone so project allowlists/history stay valid.
                state["active"][candidate["role_id"]]["role"]["status"] = "retired"
            else:
                state["active"][candidate["role_id"]] = copy.deepcopy(candidate["predecessor"])
            candidate["status"] = "rolled_back"
            state["history"].append({"kind": "rolled_back", "candidate_id": candidate_id, "actor": actor, "reason": receipt["reason"]})
