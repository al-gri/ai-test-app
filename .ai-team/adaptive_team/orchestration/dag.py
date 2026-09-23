"""Validated DAGs and deterministic, resource-aware static wave previews.

Static waves are a planning view, not permission to execute. Runtime readiness
must come from accepted AND integrated dependencies and Team's current policy.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from types import MappingProxyType
from typing import Iterable, Mapping

from ..models import PolicyError, identifier, integer, overlaps, scope


@dataclass(frozen=True)
class Node:
    id: str
    dependencies: tuple[str, ...] = ()
    write_scopes: tuple[str, ...] = ()
    resources: tuple[str, ...] = ()
    priority: int = 50

    def __post_init__(self):
        identifier(self.id)
        integer(self.priority, "priority")
        for field in ("dependencies", "write_scopes", "resources"):
            items = getattr(self, field)
            if not isinstance(items, tuple) or len(set(items)) != len(items):
                raise PolicyError(f"{field} must be an immutable unique tuple")
            for value in items:
                scope(value) if field == "write_scopes" else identifier(value)


class DAG:
    def __init__(self, nodes: Iterable[Node]):
        supplied = tuple(nodes)
        if not supplied or len(supplied) > 10_000:
            raise PolicyError("Expected 1..10000 DAG nodes")
        data = {node.id: node for node in supplied}
        if len(data) != len(supplied):
            raise PolicyError("Duplicate task ID")
        for node in supplied:
            if node.id in node.dependencies or set(node.dependencies) - data.keys():
                raise PolicyError("Self-dependency or missing dependency")
        self.nodes = MappingProxyType(data)
        # Kahn's algorithm avoids recursion limits on large linear plans.
        indegree = {n.id: len(n.dependencies) for n in supplied}
        children = {n.id: [] for n in supplied}
        for node in supplied:
            for parent in node.dependencies:
                children[parent].append(node.id)
        frontier = [key for key, count in indegree.items() if not count]
        visited = 0
        while frontier:
            key = frontier.pop()
            visited += 1
            for child in children[key]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    frontier.append(child)
        if visited != len(data):
            raise PolicyError("Task graph contains a cycle")

    def ready(self, completed: set[str], occupied: set[str] | None = None,
              held: set[str] | None = None) -> tuple[Node, ...]:
        occupied, held = set(occupied or ()), set(held or ())
        if (completed | occupied | held) - self.nodes.keys():
            raise PolicyError("State references an unknown DAG node")
        if completed & (occupied | held):
            raise PolicyError("Completed work cannot also be occupied or held")
        return tuple(sorted((n for n in self.nodes.values()
                             if n.id not in completed | occupied | held
                             and set(n.dependencies) <= completed),
                            key=lambda n: (-n.priority, n.id)))

    def select(self, completed: set[str], *, max_parallel: int,
               capacities: Mapping[str, int] | None = None,
               occupied: set[str] | None = None, held: set[str] | None = None) -> tuple[Node, ...]:
        integer(max_parallel, "max_parallel", 1)
        capacities = dict(capacities or {})
        for key, value in capacities.items():
            identifier(key)
            integer(value, "resource capacity", 1)
        if {r for n in self.nodes.values() for r in n.resources} - capacities.keys():
            raise PolicyError("A resource has no configured capacity")
        ready = self.ready(completed, occupied, held)
        running = [self.nodes[key] for key in occupied or ()]
        selected = []
        usage = Counter(r for n in running for r in n.resources)
        for node in ready:
            if len(running) + len(selected) >= max_parallel:
                break
            if any(overlaps(a, b) for other in running + selected
                   for a in node.write_scopes for b in other.write_scopes):
                continue
            if any(usage[r] >= capacities[r] for r in node.resources):
                continue
            selected.append(node)
            usage.update(node.resources)
        return tuple(selected)

    def waves(self, *, max_parallel: int, capacities: Mapping[str, int] | None = None) -> tuple[tuple[str, ...], ...]:
        completed: set[str] = set()
        waves = []
        while len(completed) < len(self.nodes):
            wave = self.select(completed, max_parallel=max_parallel, capacities=capacities)
            if not wave:
                raise PolicyError("No feasible wave under the specified resource limits")
            waves.append(tuple(n.id for n in wave))
            completed.update(n.id for n in wave)
        return tuple(waves)
