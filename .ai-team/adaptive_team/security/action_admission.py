"""Short, durable admission transactions shared by protocol/provider gateways.

The commit of action.admitted is the linearization point, not receipt of remote
bytes. A later hold fences NEW admissions; it cannot recall an in-flight action.
Adapters must finish their local queues/rate waits before calling admit_current.
Never retain a database lock over network IO. ContextVars isolate concurrent tasks
without changing a shared client's transport or authorization callback.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import uuid
import math
import time
from contextlib import closing
from ..storage import relational as rows

from ..models import PolicyError, digest

_current = ContextVar('adaptive_team_action_admission', default=None)


class ActionAdmission:
    def __init__(self, team, ticket, *, project=None, validate=None, clock=None):
        from ..models import canonical
        import json
        self.team, self.ticket = team, json.loads(canonical(ticket))
        self.project, self.validate, self.clock = project, validate, clock

    def admit(self, endpoint, operation):
        # Production Team samples time after BEGIN IMMEDIATE. The optional clock
        # is solely for deterministic adapter tests; it is sampled under that lock.
        with closing(self.team._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            rows.require(db)
            at = (self.clock or time.time)()
            state = db.execute('SELECT * FROM projects WHERE id=1').fetchone()
            if type(at) not in (int,float) or not math.isfinite(at) or at < state['last_time']:
                raise PolicyError('Invalid or backwards admission clock')
            ticket = self.ticket
            lease = rows.lease(db, ticket['id'])
            immutable = lambda value: {k: v for k, v in value.items() if k != 'expires_at'}
            if (lease is None or immutable(lease) != immutable(ticket)
                    or (self.project is not None and state['project'] != self.project)):
                raise PolicyError('Action requires the current authenticated assignment')
            if lease['status'] != 'active' or lease['expires_at'] <= at:
                raise PolicyError('Stale/expired lease')
            rows.fence(db, ticket)
            if state['paused'] or state['budget_exceeded'] or rows.held(db, ticket['task_id']):
                raise PolicyError('Action blocked by project pause, budget or human hold')
            if self.validate is not None:
                self.validate(db)  # Durable remote generation in this SAME transaction.
            permit = {'id': uuid.uuid4().hex, 'lease': ticket['id'],
                'task': ticket['task_id'], 'generation': ticket['generation'],
                'hold_generation': state['action_generation'],
                'endpoint_digest': digest(endpoint), 'operation': operation}
            self.team._event(db, at, 'action.admitted', permit)
            db.execute('UPDATE projects SET last_time=? WHERE id=1',(at,))
            db.commit()
        return permit


@contextmanager
def action_scope(admission):
    token = _current.set(admission)
    try:
        yield
    finally:
        _current.reset(token)


def admit_current(endpoint, operation):
    """Owner adapter dispatch boundary, after local awaits and before send.

    Standalone low-level protocol clients have no Team authority. Production
    task execution must enter through ProtocolServices or MeteredGateway.
    """
    admission = _current.get()
    return admission.admit(endpoint, operation) if admission is not None else None
