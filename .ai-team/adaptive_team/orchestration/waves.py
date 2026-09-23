"""Nonblocking dispatch pump for independent Team assignments.

There is deliberately no wait-for-all wave barrier: completed work can unblock
its successors while unrelated work is still running. A future completing with an
exception is NOT termination evidence and does not free the Team lease.
"""
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from threading import Lock
from typing import Callable

from ..engine import Team
from ..models import PolicyError, integer


class WaveDispatcher:
    def __init__(self, team: Team, executor, *, max_workers: int,
                 before_dispatch: Callable | None = None, before_submit: Callable | None = None):
        integer(max_workers, "max_workers", 1)
        # A smaller pool would queue already leased tasks, consuming lease time.
        if max_workers < team.snapshot()["policy"]["max_active"]:
            raise PolicyError("Pool must accommodate the entire configured active ceiling")
        self.team, self.executor = team, executor
        self.before_dispatch, self.before_submit = before_dispatch, before_submit
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="ai-task")
        self._futures: dict[str, tuple[Future, dict]] = {}
        self._lock = Lock()
        self._closed = False

    def tick(self, *, eligible_tasks: set[str] | None = None) -> dict:
        """Collect completed calls, then submit an independently ready wave.

        eligibility only narrows Team's checks. A coordinator uses it to exclude
        tasks whose dependencies have not yet entered its integration ref.
        """
        with self._lock:
            if self._closed:
                raise RuntimeError("Dispatcher is closed")
            results, errors = [], []
            for lease_id, (future, ticket) in list(self._futures.items()):
                if not future.done():
                    continue
                try:
                    # Trusted backend receipts are still validated by Team.finish.
                    results.append(self.team.finish(future.result()))
                except Exception as exc:
                    errors.append({"lease_id": lease_id, "error_type": type(exc).__name__,
                                   "action": "Retain lease and resources; inspect and recover explicitly"})
                del self._futures[lease_id]
            try:
                if self.before_dispatch:
                    self.before_dispatch()
            except Exception as exc:
                errors.append({"error_type": type(exc).__name__,
                               "action": "Repair task-file projection before new admission"})
                return {"submitted": [], "running_calls": len(self._futures),
                        "results": results, "errors": errors, "waiting": []}
            batch = self.team.dispatch(eligible_tasks=eligible_tasks)
            submitted = []
            for ticket in batch["assignments"]:
                try:
                    if self.before_submit:
                        self.before_submit(ticket)
                    from contextvars import copy_context
                    future = self._pool.submit(copy_context().run, self.executor.run, ticket)
                    self._futures[ticket["id"]] = (future, ticket)
                    submitted.append(ticket["id"])
                except Exception as exc:
                    # Dispatch already reserved this assignment. Never pretend it
                    # was completed or refund its budget without reconciliation.
                    errors.append({"lease_id": ticket["id"], "error_type": type(exc).__name__,
                                   "action": "Submission failed; reconcile reserved assignment"})
            return {"submitted": submitted, "running_calls": len(self._futures),
                    "results": results, "errors": errors, "waiting": batch["waiting"]}

    def close(self, *, wait: bool = True) -> None:
        """Stop admitting work. Does not manufacture receipts or kill remote jobs.

        Drain results with tick(eligible_tasks=set()) before closing normally.
        wait=False stops waiting for local threads; it does not stop their work.
        """
        with self._lock:
            self._closed = True
        self._pool.shutdown(wait=wait, cancel_futures=False)
