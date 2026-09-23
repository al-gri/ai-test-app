"""Compose per-wire-attempt rate limits with semantic-operation circuit results."""
from ._store import scope_key


class RequestGuard:
    def __init__(self, breaker, limiter, *, project, principal):
        self.breaker, self.limiter = breaker, limiter
        self.project, self.principal = project, principal

    def key(self, endpoint, tool):
        return scope_key(self.project, self.principal, endpoint, tool)

    async def call(self, endpoint, tool, invoke):
        key = self.key(endpoint, tool)
        self.limiter.acquire(key)
        permit = self.breaker.acquire(key)
        try:
            result = await invoke()
        except BaseException:
            self.breaker.finish(permit, success=False)
            raise
        self.breaker.finish(permit, success=True)
        return result


class LimitedTransport:
    """Limit actual HTTP requests, including MCP discovery pagination."""
    def __init__(self, transport, limiter, *, project, principal):
        self.transport, self.limiter = transport, limiter
        self.project, self.principal, self.endpoint = project, principal, transport.endpoint

    handles_action_admission = True

    async def exchange(self, message, *, headers, timeout):
        # Aggregate endpoint limit prevents cycling tool names to avoid the quota.
        self.limiter.acquire(scope_key(self.project, self.principal, self.endpoint, 'all-wire-requests'))
        if not getattr(self.transport, 'handles_action_admission', False):
            from .action_admission import admit_current
            admit_current(self.endpoint, message['method'])
        return await self.transport.exchange(message, headers=headers, timeout=timeout)
