from __future__ import annotations

import json
import math
import re
import sqlite3
import time
import uuid
from collections import Counter
from contextlib import closing, contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterator

from .storage import relational as rows
from .models import Policy, Task, PolicyError, canonical, digest, integer, overlaps, scope, validate_plan, within


def catalog() -> dict:
    data = json.loads(Path(__file__).with_name("roles.json").read_text(encoding="utf-8"))
    from .prompting import prompt_bundle
    roles = {}
    for role in data["roles"]:
        role = dict(role)
        role.setdefault("version", 1)
        role.setdefault("generation", 1)  # Seed metadata; live generation is operational SQL authority.
        role.setdefault("status", "active")
        role["prompt_digest"] = digest(prompt_bundle(role))
        roles[role["id"]] = role
    return roles


def _hash(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


class Team:
    """Trusted local coordinator, not a sandbox or an unauthenticated agent API.

    Each mutation is one SQLite transaction. Executors receive tickets and return
    receipts through a trusted adapter. They must never get access to this DB.
    """

    def __init__(self, database: str | Path, *, evolution=None):
        self.database = str(database)
        self.roles = catalog()
        from .prompting import prompt_bundle
        self.bundles = {key: prompt_bundle(value) for key, value in self.roles.items()}
        if evolution is not None:
            self.bundles = evolution.snapshot()["active"]
            if not self.bundles:
                raise PolicyError("Evolution registry has no approved seed")
            self.roles = {key: {**value["role"], "prompt_digest": digest(value)} for key, value in self.bundles.items()}

    def _connect(self) -> sqlite3.Connection:
        return rows.connect(self.database)

    def initialize(self, plan: dict, now: float | None = None, *, approval_record: dict | None = None, project_context: dict | None = None, plan_evidence: dict | None = None) -> dict:
        from .verification.plan_gate import PlanGate
        gate = PlanGate().evaluate(plan, self.roles, evidence=plan_evidence).require()
        policy, tasks = validate_plan(plan, self.roles)
        if not policy.allowed_roles:
            policy.allowed_roles = sorted(key for key, role in self.roles.items() if role["status"] in ("active", "trial"))
        now = time.time() if now is None else now
        state = {
            "schema_version": 3, "project": plan["project"], "plan_digest": digest(plan),
            "policy": asdict(policy), "policy_digest": digest(asdict(policy)),
            "catalog_digest": digest(self.roles),
            "historical_catalog": self.roles, "product_approval": approval_record,
            "plan_gate_result": gate.to_dict(),
            "project_context": project_context or {},
            "created_at": now, "last_time": now, "paused": False, "pause_reason": "",
            "spent_cents": 0, "budget_exceeded": False,
            "tasks": {task.id: {
                "spec": asdict(task), "status": "pending", "attempts": 0,
                "created_at": now, "ready_after": now, "result": None,
                "reason": "", "blocker": None, "history": [], "unresolved_findings": []
            } for task in tasks},
            "leases": {},
        }
        with closing(self._connect()) as connection:
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute("BEGIN IMMEDIATE")
            try:
                rows.install(connection)
                if connection.execute("SELECT 1 FROM projects").fetchone():
                    raise PolicyError("Database already initialized; existing work will not be overwritten")
                rows.seed_roles(connection, self.roles, self.bundles)
                rows.save(connection, None, state)
                from .orchestration.task_files import enqueue
                enqueue(connection, None, state)
                self._event(connection, now, "initialized", {"plan_digest": state["plan_digest"]})
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return state

    @staticmethod
    def _event(connection, now, kind, payload):
        connection.execute("INSERT INTO events(at,payload) VALUES(?,?)", (now, canonical({"kind": kind, **payload})))

    @contextmanager
    def _edit(self, now: float | None = None, *, clock=None) -> Iterator[tuple[dict, sqlite3.Connection, float]]:
        # Resolve modules BEFORE taking the writer lock. The unit of work below
        # reads pinned SQL documents; no prompt/DDL files or projections are read
        # or fsynced here. SQLite's own durable commit is necessarily database IO.
        from .orchestration.task_files import enqueue
        from .llmops import token_ledger
        # Explicit timestamps are deterministic test/maintenance inputs. Production
        # wall time must be sampled AFTER obtaining the serialization lock: a
        # waiting transaction must not compare its stale pre-lock time to a newer
        # committed heartbeat, or admit a lease that expired while waiting.
        if now is not None and (type(now) not in (int, float) or not math.isfinite(now)):
            raise PolicyError("Invalid clock")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            now = (clock or time.time)() if now is None else now
            if type(now) not in (int, float) or not math.isfinite(now):
                raise PolicyError("Invalid clock")
            state = rows.load(connection)
            before = json.loads(canonical(state))
            self._refresh_roles(state)
            if now < state["last_time"]:
                raise PolicyError("Clock moved backwards; reconcile the controller clock")
            yield state, connection, now
            state["last_time"] = now
            rows.save(connection, before, state)
            enqueue(connection, before, state)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def snapshot(self) -> dict:
        with closing(self._connect()) as connection:
            connection.execute('BEGIN')
            state = rows.load(connection)
            connection.commit()
        self._refresh_roles(state)
        active = self._active(state)
        future_reviews = self._future_reviews(state)
        return {**state, "summary": {
            "task_counts": dict(Counter(t["status"] for t in state["tasks"].values())),
            "active_agents": len(active), "active_roles": dict(Counter(x["role"] for x in active)),
            "reserved_cents": sum(x["reserved_cents"] for x in active),
            "committed_review_cents": future_reviews,
            "available_cents": state["policy"]["budget_cents"] - state["spent_cents"] - sum(x["reserved_cents"] for x in active) - future_reviews,
            "complete": all(t["status"] == "accepted" for t in state["tasks"].values()),
        }}

    def events(self) -> list[dict]:
        with closing(self._connect()) as connection:
            return [{"sequence": row[0], "at": row[1], **json.loads(row[2])}
                    for row in connection.execute("SELECT id,at,payload FROM events ORDER BY id")]

    @staticmethod
    def _active(state: dict) -> list[dict]:
        # Expired executors still own resources until termination is attested.
        return [x for x in state["leases"].values() if x["status"] in ("active", "expired")]

    @staticmethod
    def _future_reviews(state: dict) -> int:
        return sum(t["spec"]["review_budget_cents"] for t in state["tasks"].values()
                   if t["spec"]["review_required"] and t["status"] in ("running", "review_ready"))

    @staticmethod
    def _depths(tasks: dict) -> dict[str, int]:
        children = {key: [] for key in tasks}
        for key, task in tasks.items():
            for parent in task["spec"]["dependencies"]:
                children[parent].append(key)
        # Iterative topological traversal supports long plans without recursion.
        incoming = {key: len(task["spec"]["dependencies"]) for key, task in tasks.items()}
        queue = [key for key, count in incoming.items() if not count]
        order = []
        while queue:
            key = queue.pop()
            order.append(key)
            for child in children[key]:
                incoming[child] -= 1
                if not incoming[child]:
                    queue.append(child)
        depths = {}
        for key in reversed(order):
            depths[key] = 1 + max((depths[child] for child in children[key]), default=0)
        return depths

    @staticmethod
    def _expire(state, now):
        for lease in state["leases"].values():
            if lease["status"] == "active" and lease["expires_at"] <= now:
                lease["status"] = "expired"
                task = state["tasks"][lease["task_id"]]
                task["reason"] = "Lease expired: executor termination must be confirmed before retry"

    def _refresh_roles(self, state):
        registry = json.loads(canonical(state['role_registry']))
        self.roles = {k:v['card'] for k,v in registry.items()}
        self.bundles = {k:v['bundle'] for k,v in registry.items()}

    def role_card(self, role_id):
        """Owner-issued card; generation is operational, not model-editable."""
        state = self.snapshot()
        item = state['role_registry'][role_id]
        return {**item['card'], 'generation':item['generation'], 'revoked':item['revoked']}

    def adopt_role(self, role_id, bundle, *, expected_generation, reason, now=None):
        """Trusted deployment of an independently approved immutable bundle.

        Learning approval is not deployment. This atomic operational transaction
        changes only this role and fences its existing assignments. It never
        refunds bills or pretends those old executors have stopped.
        """
        from .security.role_authority import install_role
        with self._edit(now) as (state, db, at):
            return install_role(db, role_id, bundle, expected_generation=expected_generation,
                reason=reason, at=at)

    def deploy_role(self, evolution, role_id, *, expected_generation, expected_bundle_digest, reason, now=None):
        """Deploy an exact approved learning revision without distributed writes.

        Learning can roll back independently: operational adoption pins its own
        immutable copy. Restore never restores execution generations.
        """
        bundle = evolution.snapshot()['active'].get(role_id)
        if not bundle or digest(bundle) != expected_bundle_digest:
            raise PolicyError('Approved deployment bundle changed')
        return self.adopt_role(role_id, bundle, expected_generation=expected_generation, reason=reason, now=now)

    def revoke_role(self, role_id, *, expected_generation, reason, now=None):
        from .security.role_authority import revoke_role
        with self._edit(now) as (state, db, at):
            return revoke_role(db, role_id, expected_generation=expected_generation, reason=reason, at=at)

    def restore_role(self, role_id, *, target_generation, expected_generation, reason, now=None):
        """Owner rollback of prompt content, with NEW version and generation."""
        from .security.role_authority import current, install_role
        with self._edit(now) as (state, db, at):
            old = current(db, role_id, expected_generation)
            prior = db.execute('SELECT bundle FROM role_versions WHERE role_id=? AND generation=?',
                (role_id, target_generation)).fetchone()
            if not prior or not old: raise PolicyError('Unknown historical role revision')
            bundle = json.loads(prior[0]); bundle['role']['version'] = old['version'] + 1
            return install_role(db, role_id, bundle, expected_generation=expected_generation, reason=reason, at=at)

    def dispatch(self, now: float | None = None, *, eligible_tasks: set[str] | None = None) -> dict:
        with self._edit(now) as (state, connection, now):
            # A concurrently finishing snapshot may refresh public caches. Only
            # this transaction's registry can authorize the assignment we write.
            roles = {key:item['card'] for key,item in state['role_registry'].items()}
            bundles = {key:item['bundle'] for key,item in state['role_registry'].items()}
            self._expire(state, now)
            if state["paused"] or state["budget_exceeded"]:
                return {"assignments": [], "waiting": [{"reason": state["pause_reason"] or "budget_exceeded"}]}
            policy = state["policy"]
            tasks = state["tasks"]
            if eligible_tasks is not None:
                if not isinstance(eligible_tasks, (set, frozenset)) or eligible_tasks - tasks.keys():
                    raise PolicyError("Eligibility must be a set of known task IDs")
                eligible_tasks = frozenset(eligible_tasks)
            depths = self._depths(tasks)
            ready = []
            waiting = []
            from .llmops.human_requests import held_tasks
            human_held = held_tasks(state)
            for key, task in tasks.items():
                if task["status"] not in ("pending", "review_ready"):
                    continue
                if key in human_held:
                    waiting.append({"task": key, "reason": "human_hold"})
                    continue
                if eligible_tasks is not None and key not in eligible_tasks:
                    waiting.append({"task": key, "reason": "external_readiness"})
                    continue
                if task["ready_after"] > now:
                    waiting.append({"task": key, "reason": "retry_backoff", "until": task["ready_after"]})
                    continue
                if any(tasks[parent]["status"] != "accepted" for parent in task["spec"]["dependencies"]):
                    waiting.append({"task": key, "reason": "dependencies_not_accepted"})
                    continue
                review = task["status"] == "review_ready"
                age = int((now - task["created_at"]) // 60)
                # Reviews drain first; aging prevents permanent priority starvation.
                score = task["spec"]["priority"] + min(age, 10000) + depths[key] * 5
                ready.append((not review, -score, key))
            ready.sort()
            assignments = []
            while ready:
                # Round-robin between squads of equal phase priority, preserving
                # age/dependency priority inside each squad. One shared cap/budget.
                occupancy = Counter(tasks[x["task_id"]]["spec"]["group"] for x in self._active(state))
                ready.sort(key=lambda item: (item[0], occupancy[tasks[item[2]]["spec"]["group"]], item[1], item[2]))
                _, _, key = ready.pop(0)
                task = tasks[key]
                spec = task["spec"]
                active = self._active(state)
                phase = "review" if task["status"] == "review_ready" else "implement"
                role = spec["reviewer_role"] if phase == "review" else spec["role"]
                reserve = spec["review_budget_cents"] if phase == "review" else spec["attempt_budget_cents"]
                future_reviews = self._future_reviews(state)
                future_reviews += (-spec["review_budget_cents"] if phase == "review" else
                                   spec["review_budget_cents"] if spec["review_required"] else 0)
                reason = None
                from .llmops.token_ledger import admission
                if state["role_registry"][role]["revoked"] or roles[role]["status"] not in {"active", "trial"}:
                    reason = "role_revoked"
                elif not admission(connection, role, now, key):
                    reason = "role_token_quota"
                elif len(active) >= policy["max_active"]:
                    reason = "global_capacity"
                elif sum(x["phase"] == phase for x in active) >= policy["max_reviewers" if phase == "review" else "max_implementers"]:
                    reason = "phase_capacity"
                elif sum(tasks[x["task_id"]]["spec"]["group"] == spec["group"] for x in active) >= policy["group_capacities"].get(spec["group"], policy["max_active_per_group"]):
                    reason = "group_capacity"
                elif sum(x["role"] == role for x in active) >= policy["max_active_per_role"]:
                    reason = "role_capacity"
                elif state["spent_cents"] + sum(x["reserved_cents"] for x in active) + reserve + future_reviews > policy["budget_cents"]:
                    reason = "budget_capacity"
                elif phase == "implement" and task["attempts"] >= spec["max_attempts"]:
                    task["status"], task["blocker"] = "escalated", "attempt_limit"
                    reason = "attempt_limit"
                else:
                    for running in active:
                        running_spec = tasks[running["task_id"]]["spec"]
                        if any(overlaps(a, b) for a in spec["write_scopes"] for b in running_spec["write_scopes"]):
                            reason = "scope_conflict"
                            break
                    for resource in spec["resources"]:
                        count = sum(resource in tasks[x["task_id"]]["spec"]["resources"] for x in active)
                        if count >= policy["resource_capacities"][resource]:
                            reason = "resource_capacity:" + resource
                            break
                if reason:
                    waiting.append({"task": key, "reason": reason})
                    continue
                if phase == "implement":
                    task["attempts"] += 1
                lease_id = uuid.uuid4().hex
                actor_id = "agent-" + uuid.uuid4().hex
                execution = {"actor_id": actor_id, "run_uuid": lease_id, "workspace_id": f"{state['project']}/{key}/{lease_id}",
                             "capabilities": ["read", "review"] if phase == "review" else roles[role]["capabilities"]}
                inputs = {
                    "execution": execution,
                    "project_context": state["project_context"],
                    "task": spec, "dependencies": {dep: tasks[dep]["result"]["artifact_digest"] for dep in spec["dependencies"]},
                    "policy_digest": state["policy_digest"], "phase": phase,
                    "acceptance_gate": policy.get("acceptance_gate", {"threshold": 95, "checks": {}}),
                    "role_generation": state["role_registry"][role]["generation"],
                    "agent_card": {**roles[role], "generation": state["role_registry"][role]["generation"]},
                    "prompt_bundle": bundles[role], "prompt_digest": digest(bundles[role]),
                    "subject_digest": task["result"]["artifact_digest"] if phase == "review" else None,
                    "previous_findings": task["unresolved_findings"],
                }
                lease = {
                    "id": lease_id, "task_id": key, "phase": phase, "role": role, "actor_id": actor_id,
                    "generation": state["role_registry"][role]["generation"],
                    "attempt": task["attempts"], "started_at": now,
                    "expires_at": now + policy["lease_seconds"], "status": "active",
                    "reserved_cents": reserve, "input_digest": digest(inputs), "inputs": inputs,
                    **execution,
                    "receipt_digest": None,
                }
                state["leases"][lease_id] = lease
                task["status"] = "reviewing" if phase == "review" else "running"
                task["reason"], task["blocker"] = "", None
                assignments.append(lease)
                self._event(connection, now, "dispatched", {"task": key, "lease": lease_id, "role": role, "phase": phase})
            return {"assignments": assignments, "waiting": waiting}

    def heartbeat(self, lease_id: str, actor_id: str, now: float | None = None) -> dict:
        # Frequent lease renewal reads one lease and one policy, never the entire
        # project snapshot or all prompt bundles. No task-file projection changes.
        with closing(self._connect()) as db:
            db.execute('BEGIN IMMEDIATE'); rows.require(db)
            at = time.time() if now is None else now
            project = db.execute('SELECT project,last_time FROM projects WHERE id=1').fetchone()
            if type(at) not in (int,float) or not math.isfinite(at):
                raise PolicyError('Invalid clock')
            if at < project['last_time']:
                raise PolicyError('Clock moved backwards; reconcile the controller clock')
            lease = rows.lease(db,lease_id)
            if not lease or lease['actor_id']!=actor_id or lease['status']!='active' or lease['expires_at']<=at:
                raise PolicyError('Stale/expired lease or wrong actor')
            rows.fence(db,lease)
            policy=json.loads(db.execute("SELECT document FROM project_documents WHERE project=? AND name='policy'",(project['project'],)).fetchone()[0])
            expiry=min(lease['started_at']+policy['max_run_seconds'],at+policy['lease_seconds'])
            db.execute('UPDATE leases SET expires_at=? WHERE id=?',(expiry,lease_id))
            db.execute('UPDATE projects SET last_time=? WHERE id=1',(at,));db.commit()
            return {'lease_id':lease_id,'expires_at':expiry}

    @staticmethod
    def _live_lease(state, lease_id, actor_id, now):
        lease = state["leases"].get(lease_id)
        if not lease or lease["actor_id"] != actor_id:
            raise PolicyError("Unknown lease or wrong actor")
        if lease["status"] != "active" or lease["expires_at"] <= now:
            raise PolicyError("Stale/expired lease; result cannot authorize a transition")
        authority = state['role_registry'].get(lease['role'])
        if not authority or authority['revoked'] or authority['generation'] != lease.get('generation'):
            raise PolicyError('Stale or revoked role generation')
        return lease

    def finish(self, receipt: dict, now: float | None = None) -> dict:
        with self._edit(now) as (state, connection, now):
            lease_id = receipt.get("lease_id")
            lease = state["leases"].get(lease_id)
            if not lease:
                raise PolicyError("Unknown lease")
            receipt_digest = digest(receipt)
            if lease["receipt_digest"]:
                if lease["receipt_digest"] != receipt_digest:
                    raise PolicyError("Conflicting duplicate receipt")
                return {"idempotent": True, "task_id": lease["task_id"], "status": lease["result_status"]}
            self._live_lease(state, lease_id, receipt.get("actor_id"), now)
            if receipt.get("input_digest") != lease["input_digest"]:
                raise PolicyError("Input identity mismatch")
            cost = integer(receipt.get("actual_cost_cents"), "actual_cost_cents")
            from .llmops.token_ledger import verify_final_cost
            verify_final_cost(connection, lease_id, cost)
            evidence = receipt.get("evidence")
            if not isinstance(evidence, list) or not evidence or not all(isinstance(x, str) and x.strip() for x in evidence):
                raise PolicyError("Trusted executor evidence references required")
            task = state["tasks"][lease["task_id"]]
            spec = task["spec"]
            outcome = receipt.get("outcome")
            allowed = {"success", "candidate_defect", "infrastructure_defect", "missing_evidence", "external_blocker"}
            if outcome not in allowed:
                raise PolicyError("Unknown outcome")
            if outcome == "success":
                if lease["phase"] == "implement":
                    if not _hash(receipt.get("artifact_digest")):
                        raise PolicyError("A verified artifact SHA256 is required")
                    checks = receipt.get("checks", {})
                    from .verification.acceptance_gate import AcceptanceGate
                    gate = AcceptanceGate().evaluate(subject_digest=receipt["artifact_digest"], checks=checks,
                        required_checks=tuple(spec["checks"]), policy=state["policy"].get("acceptance_gate")).require()
                    changed = receipt.get("changed_files", [])
                    if not isinstance(changed, list):
                        raise PolicyError("changed_files must be a list")
                    for path in changed:
                        scope(path)
                        if not any(within(path, root) for root in spec["write_scopes"]):
                            raise PolicyError(f"Out-of-scope output: {path}")
                    from .code_integration.dependency_policy import enforce_paths
                    enforce_paths(lease["role"], changed)
                    task["result"] = {"artifact_digest": receipt["artifact_digest"], "checks": checks,
                                      "evidence": evidence, "author": lease["actor_id"], "lease": lease_id,
                                      "acceptance_gate_result": gate.to_dict()}
                    task["status"] = "review_ready" if spec["review_required"] else "accepted"
                else:
                    result = task["result"]
                    if lease["actor_id"] in [result["author"], *spec["source_authors"]]:
                        raise PolicyError("Material author cannot approve this candidate")
                    if receipt.get("subject_digest") != result["artifact_digest"]:
                        raise PolicyError("Review is not bound to the current artifact")
                    verdict = receipt.get("verdict")
                    findings = receipt.get("findings", [])
                    if not isinstance(findings, list) or not all(isinstance(x, dict) and
                            set(x) == {"id", "severity", "problem", "required_change"} and
                            x["severity"] in ("critical", "major", "minor") and
                            all(isinstance(x[k], str) and x[k].strip() for k in ("id", "problem", "required_change")) for x in findings):
                        raise PolicyError("Malformed review findings")
                    if len({x["id"] for x in findings}) != len(findings):
                        raise PolicyError("Duplicate finding ID")
                    if verdict == "approve":
                        checks = receipt.get("checks", {})
                        required = {"review", "requirements", "tests", "scope", "independence"}
                        if not isinstance(checks, dict) or any(checks.get(x) != "PASS" for x in required) or any(
                            value not in ("PASS", "NOT_APPLICABLE") for value in checks.values()
                        ) or any(x["severity"] != "minor" for x in findings):
                            raise PolicyError("Approval contradicts checks/findings")
                        from .verification.acceptance_gate import AcceptanceGate
                        review_gate = AcceptanceGate().evaluate(subject_digest=result["artifact_digest"],
                            checks=checks, required_checks=tuple(sorted(required)),
                            policy=state["policy"].get("acceptance_gate")).require()
                        task["status"] = "accepted"
                        task["unresolved_findings"] = []
                        result["review"] = {"actor": lease["actor_id"], "evidence": evidence, "findings": findings,
                                            "gate": review_gate.to_dict()}
                    elif verdict == "changes_required":
                        if not any(x["severity"] in ("critical", "major") for x in findings):
                            raise PolicyError("Changes require actionable findings")
                        task["unresolved_findings"] = findings
                        self._retry(task, state["policy"], now, "review_changes")
                    elif verdict == "blocked":
                        task["status"], task["blocker"] = "blocked", "missing_evidence"
                    else:
                        raise PolicyError("Unknown review verdict")
            elif outcome == "candidate_defect":
                self._retry(task, state["policy"], now, "candidate_defect")
            else:
                task["status"], task["blocker"] = "blocked", outcome
            task["reason"] = receipt.get("summary", outcome)
            task["history"].append({"lease": lease_id, "phase": lease["phase"], "outcome": outcome,
                                    "findings": receipt.get("findings", []), "evidence": evidence,
                                    "summary": task["reason"]})
            # Record real reported spend even on an overrun; never discard a bill.
            state["spent_cents"] += cost
            if cost > lease["reserved_cents"] or state["spent_cents"] > state["policy"]["budget_cents"]:
                state["budget_exceeded"] = True
                state["pause_reason"] = "Executor exceeded its reserved budget; reconcile provider billing"
            lease.update(status="complete", receipt_digest=receipt_digest, result_status=task["status"])
            self._event(connection, now, "finished", {"task": lease["task_id"], "lease": lease_id,
                                                       "status": task["status"], "cost_cents": cost})
            return {"idempotent": False, "task_id": lease["task_id"], "status": task["status"]}

    @staticmethod
    def _retry(task, policy, now, reason):
        task["status"] = "pending" if task["attempts"] < task["spec"]["max_attempts"] else "escalated"
        task["blocker"] = reason if task["status"] == "escalated" else None
        task["ready_after"] = now + policy["retry_backoff_seconds"] * (2 ** min(task["attempts"] - 1, 10))
        task["result"] = None

    def recover_expired(self, lease_id: str, termination_evidence: str, now: float | None = None) -> dict:
        """Trusted executor confirms the old process is stopped, not merely timed out."""
        if not isinstance(termination_evidence, str) or not termination_evidence.strip():
            raise PolicyError("Termination evidence required")
        with self._edit(now) as (state, connection, now):
            self._expire(state, now)
            lease = state["leases"].get(lease_id)
            if not lease or lease["status"] != "expired":
                raise PolicyError("Only an expired lease can be recovered")
            from .llmops.token_ledger import cost_floor
            recovered_cost = max(lease['reserved_cents'], cost_floor(connection, lease_id))
            lease["status"] = "abandoned"
            state["spent_cents"] += recovered_cost
            if recovered_cost > lease['reserved_cents'] or state['spent_cents'] > state['policy']['budget_cents']:
                state['budget_exceeded'] = True
                state['pause_reason'] = 'Reconciled provider overrun requires owner review'
            task = state["tasks"][lease["task_id"]]
            # Infrastructure termination requires a conscious resume, not a blind repair.
            task["status"], task["blocker"] = "blocked", "infrastructure_defect"
            task["reason"] = "Executor terminated; full reservation charged until billing reconciled"
            self._event(connection, now, "recovered", {"lease": lease_id, "evidence": termination_evidence})
            return {"status": task["status"], "charged_cents": recovered_cost}

    def resume_task(self, task_id: str, reason: str, now: float | None = None) -> None:
        if not isinstance(reason, str) or not reason.strip():
            raise PolicyError("Evidence-backed resume reason required")
        with self._edit(now) as (state, connection, now):
            task = state["tasks"].get(task_id)
            if not task or task["status"] != "blocked":
                raise PolicyError("Only blocked tasks can be resumed")
            if task["result"] is None and task["attempts"] >= task["spec"]["max_attempts"]:
                raise PolicyError("Attempt budget exhausted; needs an explicitly revised task")
            task["status"] = "review_ready" if task["result"] else "pending"
            task["ready_after"], task["reason"], task["blocker"] = now, reason, None
            self._event(connection, now, "resumed", {"task": task_id, "reason": reason})

    def pause(self, paused: bool, reason: str, now: float | None = None) -> None:
        if type(paused) is not bool or not isinstance(reason, str) or not reason.strip():
            raise PolicyError("Explicit pause/resume decision and reason required")
        with self._edit(now) as (state, connection, now):
            state["paused"], state["pause_reason"] = paused, reason if paused else ""
            self._event(connection, now, "paused" if paused else "unpaused", {"reason": reason})

    def extend(self, new_tasks: list[dict], expected_plan_digest: str, reason: str, now: float | None = None, *, plan_evidence: dict | None = None) -> None:
        """A trusted architect can add bounded tasks without changing policy/accepted work."""
        if not isinstance(reason, str) or not reason.strip() or not new_tasks:
            raise PolicyError("Extension needs tasks and a reason")
        with self._edit(now) as (state, connection, now):
            roles = {k:v['card'] for k,v in state['role_registry'].items()}
            if expected_plan_digest != state["plan_digest"]:
                raise PolicyError("Stale plan extension")
            plan = {"schema_version": 1, "project": state["project"], "policy": state["policy"],
                    "tasks": [x["spec"] for x in state["tasks"].values()] + new_tasks}
            policy = Policy(**state["policy"])
            for spec in new_tasks:
                Task(**spec).validate(policy, roles)
            accepted = {key: value["spec"] for key, value in state["tasks"].items() if value["status"] == "accepted"}
            _, tasks = validate_plan(plan, roles, accepted_specs=accepted)
            from .verification.plan_gate import PlanGate
            gate = PlanGate().evaluate(plan, roles, evidence=plan_evidence, accepted_specs=accepted).require()
            state["plan_gate_result"] = gate.to_dict()
            for task in tasks:
                if task.id not in state["tasks"]:
                    state["tasks"][task.id] = {"spec": asdict(task), "status": "pending", "attempts": 0,
                                              "created_at": now, "ready_after": now, "result": None,
                                              "reason": "", "blocker": None, "history": [], "unresolved_findings": []}
            state["plan_digest"] = digest(plan)
            self._event(connection, now, "extended", {"reason": reason, "plan_digest": state["plan_digest"],
                                                      "added": [t["id"] for t in new_tasks]})


    def adopt_catalog(self, expected_digest: str, reason: str, now=None) -> dict:
        """Trusted maintenance only. New roles never widen the frozen allowlist."""
        desired_roles = json.loads(canonical(self.roles))
        desired_bundles = json.loads(canonical(self.bundles))
        if expected_digest != digest(desired_roles) or not isinstance(reason, str) or not reason.strip():
            raise PolicyError("Exact catalog digest and maintenance reason required")
        if set(desired_roles) != set(desired_bundles):raise PolicyError('Catalog/card bundle mismatch')
        for key,card in desired_roles.items():
            bundle = desired_bundles[key]
            without_generation = lambda value:{k:v for k,v in value.items() if k not in {'generation','prompt_digest'}}
            if card.get('prompt_digest') != digest(bundle) or without_generation(card) != without_generation(bundle['role']):
                raise PolicyError('Catalog/card bundle mismatch')
        with self._edit(now) as (state, connection, now):
            if self._active(state):
                raise PolicyError("Drain all active/expired leases before catalog adoption")
            Policy(**state["policy"]).validate(desired_roles)
            for task in state["tasks"].values():
                if task["status"] == "accepted":
                    continue
                spec = task["spec"]
                roles = {spec["role"]} | ({spec["reviewer_role"]} if spec["review_required"] else set())
                if any(role not in desired_roles or desired_roles[role]["status"] not in ("active", "trial") for role in roles):
                    raise PolicyError("Pending tasks need an available role before retirement")
                Task(**spec).validate(Policy(**state["policy"]), desired_roles)
            from .security.role_authority import install_role
            for role_id, card in desired_roles.items():
                current = state['role_registry'].get(role_id)
                if current and current['card'] == card:
                    continue
                bundle = json.loads(canonical(desired_bundles[role_id]))
                install_role(connection, role_id, bundle, expected_generation=current['generation'] if current else 0,
                    reason=reason, at=now, allow_authority_change=True, require_revision=False)
            previous = state["catalog_digest"]
            state["catalog_digest"] = expected_digest
            for role_id, role in desired_roles.items():
                state["historical_catalog"].setdefault(role_id, role)
            self._event(connection, now, "catalog_adopted", {"previous": previous, "current": expected_digest, "reason": reason})
            return {"catalog_digest": expected_digest, "allowed_roles": state["policy"]["allowed_roles"]}

    def authorize_roles(self, role_ids: list[str], expected_policy_digest: str, reason: str, now=None) -> dict:
        """Explicit owner maintenance; never callable as a worker tool."""
        if not isinstance(role_ids, list) or not role_ids or not reason:
            raise PolicyError("Explicit available roles and reason required")
        with self._edit(now) as (state, connection, now):
            roles = state['role_registry']
            if any(role not in roles or roles[role]['status'] not in {'active','trial'} or roles[role]['revoked'] for role in role_ids):
                raise PolicyError('Role is not available for authorization')
            if self._active(state) or expected_policy_digest != state["policy_digest"] :
                raise PolicyError("Drain workers and match current policy/catalog before authorizing roles")
            state["policy"]["allowed_roles"] = sorted(set(state["policy"]["allowed_roles"]) | set(role_ids))
            state["policy_digest"] = digest(state["policy"])
            state["plan_digest"] = digest({"project": state["project"], "policy": state["policy"], "tasks": [x["spec"] for x in state["tasks"].values()], "schema_version": 1})
            self._event(connection, now, "roles_authorized", {"roles": role_ids, "reason": reason, "policy_digest": state["policy_digest"]})
            return {"policy_digest": state["policy_digest"], "plan_digest": state["plan_digest"]}
