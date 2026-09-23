from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Protocol

from .engine import Team
from .models import digest


class Executor(Protocol):
    """Trusted backend owns isolation, fresh identities, checks and billing.

    Never implement this by executing model-generated shell on the controller.
    Long-running backends must heartbeat and attest termination on timeout.
    """

    def run(self, ticket: dict) -> dict: ...


class SimulationExecutor:
    """Synthetic receipts only. Does not write product code or run real checks."""

    def run(self, ticket: dict) -> dict:
        spec = ticket["inputs"]["task"]
        receipt = {
            "lease_id": ticket["id"], "actor_id": ticket["actor_id"],
            "input_digest": ticket["input_digest"], "actual_cost_cents": 0,
            "outcome": "success", "evidence": ["simulation://synthetic-not-real-validation"],
            "summary": "SIMULATION: no code was implemented and no real check was run",
        }
        if ticket["phase"] == "implement":
            receipt.update(artifact_digest=digest({"simulation": ticket["input_digest"]}),
                           checks={check: "PASS" for check in spec["checks"]}, changed_files=[])
        else:
            receipt.update(subject_digest=ticket["inputs"]["subject_digest"],
                           checks={key: "PASS" for key in ("review", "requirements", "tests", "scope", "independence")},
                           verdict="approve", findings=[])
        return receipt


def run_wave(team: Team, executor: Executor) -> dict:
    """Dispatch one ready wave. Backend instances must support concurrent calls.

    This adapter API is trusted Python code. External worker identity must be
    authenticated by the backend. Tickets are not authorization to mutate policy.
    """
    dispatch = team.dispatch()
    assignments = dispatch["assignments"]
    if not assignments:
        return {"started": 0, "waiting": dispatch["waiting"], "results": []}
    results = []
    with ThreadPoolExecutor(max_workers=len(assignments)) as pool:
        futures = {pool.submit(executor.run, ticket): ticket for ticket in assignments}
        for future in as_completed(futures):
            ticket = futures[future]
            try:
                receipt = future.result()
                results.append(team.finish(receipt))
            except Exception as exc:
                # An exception does not prove the remote process stopped. Keep
                # lease/resource reservation until expiry and explicit recovery.
                results.append({"task_id": ticket["task_id"], "status": "executor_error",
                                "error_type": type(exc).__name__,
                                "next_action": "Inspect the trusted backend; do not release a live executor blindly"})
    return {"started": len(assignments), "roles": [x["role"] for x in assignments],
            "waiting": dispatch["waiting"], "results": results}


def simulate(team: Team, max_waves: int = 1000) -> dict:
    waves = []
    for index in range(max_waves):
        wave = run_wave(team, SimulationExecutor())
        waves.append({"wave": index + 1, **wave})
        state = team.snapshot()
        if state["summary"]["complete"] or wave["started"] == 0:
            break
    state = team.snapshot()
    return {"simulation": True, "warning": "Synthetic scheduling evidence, NOT delivered software",
            "project": state["project"], "peak_active": max((w["started"] for w in waves), default=0),
            "final_active": state["summary"]["active_agents"], "complete": state["summary"]["complete"],
            "waves": waves, "summary": state["summary"]}
