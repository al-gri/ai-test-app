"""Protocol services composed into a trusted backend, not into agent authority.

Coordinator -> GitExecutor -> TrustedBackend -> ProtocolServices -> MCP/A2A.
The return value is remote data. Git capture, stopped-executor evidence, cost
accounting, independent review and Team.finish still follow the Block 1 path.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import asdict

from ..engine import Team
from ..models import PolicyError, identifier
from ..protocols.a2a.client import A2AClient
from ..protocols.a2a.discovery import AgentRegistry, Requirements, Selection
from ..protocols.mcp.client import MCPClient
from ..protocols.jsonrpc import decode, encode, RemoteError
from ..security.action_admission import ActionAdmission, action_scope
from ..observability.rpc_outbox import deferred_telemetry
from ..providers.router import ModelRouter, RoutingMetadata
from .task_lifecycle import TaskContract, TaskLifecycle


class ProtocolServices:
    def __init__(self, team: Team, journal: TaskLifecycle, registry: AgentRegistry, router: ModelRouter):
        self.team, self.journal, self.registry, self.router = team, journal, registry, router
        self.registry.bind(team.database)

    def _live(self, contract: TaskContract) -> float:
        state, ticket = self.team.snapshot(), contract.ticket
        lease = state["leases"].get(ticket["id"])
        immutable = lambda value: {k: v for k, v in value.items() if k != "expires_at"}
        if state["project"] != contract.project or lease is None or immutable(lease) != immutable(ticket):
            raise PolicyError("Protocol operation requires the current authenticated Team assignment")
        # Heartbeat validates live actor, absolute run limit and current expiry.
        result = self.team.heartbeat(ticket["id"], ticket["actor_id"])
        return result["expires_at"]

    def route(self, contract: TaskContract):
        self._live(contract)
        ticket = contract.ticket
        raw = dict(ticket["inputs"]["task"].get("context", {}).get("routing", {}))
        if ticket["phase"] == "review":
            raw["task_type"] = "code_review"
        else:
            raw.setdefault("task_type", "implementation")
        # A live lease always takes the realtime route. Review cannot be made
        # cheap or deferred by a task's model-supplied context.
        raw.update(background=False, deadline_seconds=0)
        if ticket["phase"] == "review" or ticket["inputs"]["task"].get("risk") in {"high", "critical"}:
            raw["critical"] = True
        return self.router.route(RoutingMetadata.parse(raw))

    def select(self, contract: TaskContract, requirements: Requirements) -> Selection:
        self._live(contract)
        return self.registry.select(requirements, role=contract.ticket["role"])

    async def _once(self, contract, operation_id, request, invoke, *, rpc, validate=None):
        identifier(operation_id, "protocol operation")
        key = "protocol:" + contract.lease_id + ":" + operation_id
        request = {"contract_binding": contract.binding, **request}
        # Returning a bound recorded result is not permission for a new action.
        # Do not heartbeat or reject replay because of a later hold/lease expiry.
        old = self.journal.replay_outcome(key, request)
        if old is not None:
            # Reconcile a recovery copy before returning it; never invoke RPC.
            if self.journal.outcomes.get(key) is not None:
                await asyncio.to_thread(self.journal.save_outcome,key,old)
            self.journal.schedule_telemetry(key, rpc.audit)
            return self._known_result(old['result'], old['checkpoint'])
        expires = self._live(contract)
        admission = ActionAdmission(self.team, contract.ticket, project=contract.project, validate=validate)
        # Initial admission comes BEFORE intent and any discovery/initialization.
        # Per-wire admission remains in place after every local queue/rate wait.
        admission.admit(getattr(rpc.transport,'endpoint','owner-adapter'),operation_id)
        old = self.journal.begin(key, request)
        if old is not None:  # Another controller completed between read and begin.
            record = self.journal.get(key)
            self.journal.schedule_telemetry(key, rpc.audit)
            return self._known_result(old,record['checkpoint'])
        with action_scope(admission), deferred_telemetry() as events:
            try:
                async with asyncio.timeout(max(0, expires - time.time())):
                    result = await invoke()
            except RemoteError as exc:
                # A validated remote rejection is a known result. Store it and
                # rethrow its typed form on replay without issuing another RPC.
                stored = {'code': exc.code, 'status': getattr(exc, 'status', None),
                          'message': exc.remote_message, 'data': exc.data}
                value = self.journal.outcomes.remember(key,request,{},events,{'known_remote_error':stored})
                await asyncio.to_thread(self.journal.save_outcome,key,value)
                self.journal.schedule_telemetry(key, rpc.audit)
                raise
            except BaseException:
                # Cancellation/transport failure may have caused remote effects.
                # Keep the intent pending; telemetry must not mask this exception.
                try:
                    await asyncio.to_thread(self.journal.queue_telemetry,key,events)
                    self.journal.schedule_telemetry(key, rpc.audit)
                except Exception:
                    pass
                raise
            # One SQLite commit durably stores result plus telemetry outbox.
            # Only afterwards can the optional exporter observe the events.
            value = self.journal.outcomes.remember(key,request,result,events,None)
            await asyncio.to_thread(self.journal.save_outcome,key,value)
        self.journal.schedule_telemetry(key, rpc.audit)
        return result

    @staticmethod
    def _known_result(result, checkpoint=None):
        # Error metadata belongs to the trusted journal, never to remote JSON.
        error = (checkpoint or {}).get('known_remote_error')
        if error is not None:
            exc = RemoteError(error['code'], error['message'], error.get('data'))
            exc.status = error.get('status')
            raise exc
        return result

    async def call_tool(self, contract: TaskContract, operation_id: str, client: MCPClient,
                        name: str, arguments: dict):
        arguments = decode(encode(arguments))
        if client.rpc.trace_id != contract.trace_id:
            raise PolicyError("MCP client trace must match the task")
        endpoint = getattr(client.rpc.transport, "endpoint", None)
        if endpoint is None:
            raise PolicyError("Protocol services require an explicitly pinned transport endpoint")
        return await self._once(contract, operation_id,
            {"kind": "mcp", "endpoint": endpoint, "version": client.version, "tool": name, "arguments": arguments},
            lambda: client.call_tool(name, arguments), rpc=client.rpc)

    async def delegate(self, contract: TaskContract, operation_id: str, client: A2AClient, payload: dict):
        payload = decode(encode(payload))
        if client.rpc.trace_id != contract.trace_id:
            raise PolicyError("A2A client trace must match the task")
        # Stable message ID assists reconciliation; A2A does not promise that a
        # server will deduplicate SendMessage. We never rely on that assumption.
        return await self._once(contract, operation_id,
            {"kind": "a2a", "selection": asdict(client.selection), "payload": payload},
            lambda: client.send(payload, message_id=contract.lease_id + "-" + operation_id), rpc=client.rpc,
            validate=lambda db: self.registry.validate(client.selection, role=contract.ticket["role"],db=db))

    async def poll(self, contract: TaskContract, operation_id: str, client: A2AClient):
        expires = self._live(contract)
        self.registry.validate(client.selection, role=contract.ticket["role"])
        record = self.journal.get("protocol:" + contract.lease_id + ":" + operation_id)
        # JSON storage changes tuples to lists, so compare canonical digests.
        from ..models import digest
        if (not record or record["status"] != "complete" or record["request"].get("kind") != "a2a"
                or record["request"].get("contract_binding") != contract.binding
                or digest(record["request"].get("selection")) != digest(asdict(client.selection))
                or client.rpc.trace_id != contract.trace_id):
            raise PolicyError("No matching acknowledged delegation to poll")
        original = record["result"].get("task")
        if original is None:
            return record["result"]
        admission = ActionAdmission(self.team, contract.ticket, project=contract.project,
            validate=lambda db: self.registry.validate(client.selection, role=contract.ticket['role'],db=db))
        with action_scope(admission):
            async with asyncio.timeout(max(0, expires - time.time())):
                task = await client.get_task(original["id"])
        if task.get("contextId") != original.get("contextId"):
            raise PolicyError("Remote task context changed")
        return {"task": task}
