"""A2A 1.0 JSON-RPC send/get/cancel. Task results remain untrusted proposals."""
from __future__ import annotations

import uuid

from ...models import PolicyError
from ..jsonrpc import ProtocolError, RpcClient, encode, text
from .discovery import Selection

STATES = {"TASK_STATE_" + name for name in ("UNSPECIFIED", "SUBMITTED", "WORKING", "COMPLETED",
    "FAILED", "CANCELED", "INPUT_REQUIRED", "REJECTED", "AUTH_REQUIRED")}


def validate_parts(parts):
    if not isinstance(parts, list) or not parts or len(parts) > 1000:
        raise ProtocolError("A2A parts require a bounded nonempty list")
    for part in parts:
        if not isinstance(part, dict) or len(set(part) & {"text", "raw", "url", "data"}) != 1:
            raise ProtocolError("A2A 1.0 Part requires exactly one content variant")
        if "kind" in part:
            raise ProtocolError("Legacy A2A kind discriminators are unsupported")
        for name in ("text", "raw", "url"):
            if name in part and not isinstance(part[name], str):
                raise ProtocolError("Invalid A2A content value")


def validate_message(value):
    if not isinstance(value, dict) or value.get("role") != "ROLE_AGENT" or "kind" in value:
        raise ProtocolError("Expected an A2A 1.0 agent message")
    text(value.get("messageId"), "message ID")
    text(value.get("contextId"), "server message context ID")
    validate_parts(value.get("parts"))
    return value


def validate_task(value, expected_id=None):
    if not isinstance(value, dict) or "kind" in value:
        raise ProtocolError("Malformed A2A task")
    task_id = text(value.get("id"), "remote task ID")
    if expected_id is not None and task_id != expected_id:
        raise ProtocolError("Remote task ID changed")
    status = value.get("status")
    if not isinstance(status, dict) or status.get("state") not in STATES:
        raise ProtocolError("Unsupported A2A task state")
    artifacts = value.get("artifacts", [])
    if not isinstance(artifacts, list) or len(artifacts) > 1000:
        raise ProtocolError("Malformed task artifacts")
    ids = set()
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise ProtocolError("Malformed task artifact")
        identity = text(artifact.get("artifactId"), "artifact ID")
        if identity in ids:
            raise ProtocolError("Duplicate task artifact ID")
        ids.add(identity)
        validate_parts(artifact.get("parts"))
    return value


class A2AClient:
    def __init__(self, rpc: RpcClient, selection: Selection):
        # Injected transports are trusted dependencies. Production HTTP transports
        # must remain pinned to the selected interface, not a URL from task text.
        endpoint = getattr(rpc.transport, "endpoint", selection.endpoint)
        if endpoint != selection.endpoint:
            raise PolicyError("A2A transport does not match selected agent")
        self.rpc, self.selection = rpc, selection

    async def _call(self, method, params):
        if self.selection.tenant:
            params = {**params, "tenant": self.selection.tenant}
        wire = await self.rpc.request(method, params, headers={"A2A-Version": "1.0"})
        return wire.message["result"]

    async def send(self, payload: dict, *, message_id: str | None = None,
                   task_id: str | None = None, context_id: str | None = None) -> dict:
        if not isinstance(payload, dict) or len(encode(payload)) > 1024 * 1024:
            raise PolicyError("A2A delegation requires a bounded data object")
        message = {"messageId": text(message_id or uuid.uuid4().hex, "message ID"), "role": "ROLE_USER",
                   "parts": [{"data": payload, "mediaType": "application/json"}]}
        if task_id:
            message["taskId"] = text(task_id, "task ID")
        if context_id:
            message["contextId"] = text(context_id, "context ID")
        result = await self._call("SendMessage", {"message": message,
            "configuration": {"acceptedOutputModes": ["application/json"], "returnImmediately": True},
            "metadata": {"trace_id": self.rpc.trace_id, "selectedSkillIds": list(self.selection.skill_ids)}})
        if ("task" in result) == ("message" in result):
            raise ProtocolError("SendMessageResponse requires exactly one task or message")
        if "task" in result:
            validate_task(result["task"], task_id)
            if context_id and result["task"].get("contextId") != context_id:
                raise ProtocolError("Remote task context changed")
        else:
            validate_message(result["message"])
            if task_id and result["message"].get("taskId") != task_id:
                raise ProtocolError("Remote message task changed")
            if context_id and result["message"].get("contextId") != context_id:
                raise ProtocolError("Remote message context changed")
        return result

    async def get_task(self, task_id: str) -> dict:
        return validate_task(await self._call("GetTask", {"id": text(task_id, "task ID"), "historyLength": 0}), task_id)

    async def cancel_task(self, task_id: str) -> dict:
        # Cancellation acknowledgment is a remote claim, not local proof that all
        # processes stopped or a reason to release Team's budget/lease reservation.
        return validate_task(await self._call("CancelTask", {"id": text(task_id, "task ID")}), task_id)
