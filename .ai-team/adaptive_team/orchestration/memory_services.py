"""Optional Coordinator hooks and explicit reference retrieval for live tickets."""
from ..models import PolicyError, canonical
from ..memory.retrieval import Retrieval
from ..memory.reflector import Reflector
from ..memory.vector_store import HashEmbedding
from ..memory.snapshots import SnapshotManager
from contextlib import closing


class MemoryServices:
    def __init__(self, team, store, summarizer=None):
        if team.snapshot()["project"] != store.state.project:
            raise PolicyError("Memory service belongs to a different Team")
        self.team, self.store = team, store
        self.snapshots, self.retrieval = SnapshotManager(store.state), Retrieval(store)
        self.reflector = Reflector(team, store, summarizer)

    def _live(self, ticket):
        state = self.team.snapshot()
        current = state["leases"].get(ticket.get("id"))
        immutable = lambda value: {k: v for k, v in value.items() if k != "expires_at"}
        if current is None or immutable(current) != immutable(ticket):
            raise PolicyError("Memory requires an authenticated Team assignment")
        self.team.heartbeat(ticket["id"], ticket["actor_id"])

    def prepare(self, ticket):
        self._live(ticket)
        milestone = ticket["inputs"]["task"].get("context", {}).get("milestone")
        if milestone is not None:
            self.snapshots.before_milestone(milestone)

    def recall(self, ticket, *, max_chars=8000):
        self._live(ticket)
        task = ticket["inputs"]["task"]
        query = (task["title"] + "\n" + "\n".join(task["acceptance"]))[:8000]
        context = self.retrieval.context(query, role=ticket["role"], max_chars=max_chars)
        self._live(ticket)
        return context

    def accepted(self, task_id):
        # Call after Team.finish has committed acceptance; errors here cannot
        # reverse task acceptance, repeat execution, or alter billing.
        return self.reflector.reflect(task_id)

    def sync_accepted(self):
        """Recover missed post-acceptance notifications from persisted Team state.

        Only the zero-network default runs inside the admission pump. A configured
        model summarizer is dispatched by the deployment's bounded background
        worker through accepted(); the pump merely reports pending task IDs.
        """
        result = {"reflected": [], "pending": [], "errors": []}
        for task_id, task in self.team.snapshot()["tasks"].items():
            if task["status"] != "accepted":
                continue
            if self.reflector.summarizer is not None or type(self.store.embedder) is not HashEmbedding:
                result["pending"].append(task_id)
                continue
            try:
                lesson = self.accepted(task_id)
                result["reflected"].append(lesson.id)
            except Exception as exc:
                result["errors"].append({"task_id": task_id, "error_type": type(exc).__name__,
                    "action": "Reconcile memory separately; do not repeat task execution"})
        return result
