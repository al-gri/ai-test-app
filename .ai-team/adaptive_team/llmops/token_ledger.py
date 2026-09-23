"""Per-role USD micro-unit accounting with durable per-call reservations.

Use Team's operational database, not the learning capsule. A lease is not a
provider call: several calls may be billed to one lease. The gateway must enforce
the reserved token maxima at the provider, and settle authenticated usage.
"""
from __future__ import annotations
import json
import time
import uuid
from datetime import datetime, timezone
from dataclasses import asdict
from ..models import PolicyError, canonical, digest, identifier
from ..security._store import SecurityStore, timestamp
from ..storage import relational as rows
from ..observability.run_identity import execution_trace


class QuotaExceeded(PolicyError):
    pass


class CallAlreadyReserved(PolicyError):
    """Reconcile the original call; do not issue another paid request."""


def units(value, name):
    if type(value) is not int or not 0 <= value <= 10**12:
        raise PolicyError('Invalid bounded integer ' + name)
    return value


def utc_day(at):
    return datetime.fromtimestamp(timestamp(at), timezone.utc).strftime('%Y-%m-%d')


def charge(input_tokens, output_tokens, prices):
    a, b = units(input_tokens, 'input tokens'), units(output_tokens, 'output tokens')
    # One rounding step per call. 1 USD = 1,000,000 micro-USD.
    result = (a * prices[0] + b * prices[1] + 999999) // 1000000
    return units(result, 'charge')


def alert(db, role, task, reason):
    # Each new incident has a fresh ID. An approval for yesterday's incident
    # must never auto-resolve a later quota failure of the same role/task.
    if not db.execute('SELECT 1 FROM token_alerts WHERE role=? AND task=? AND reason=? AND resolved=0', (role,task,reason)).fetchone():
        db.execute('INSERT INTO token_alerts(id,role,task,reason) VALUES(?,?,?,?)',
                   ('quota-' + uuid.uuid4().hex, role, task, reason))


def admission(db, role, at, task_id=None):
    """Called within Team.dispatch's lock. No configured ledger means legacy mode."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='token_limits'").fetchone():
        return True
    row = db.execute('SELECT amount FROM token_limits WHERE role=?', (role,)).fetchone()
    spent = db.execute("SELECT COALESCE(SUM(actual),0) FROM token_calls WHERE role=? AND day=? AND status='settled'", (role, utc_day(at))).fetchone()[0]
    reserved = db.execute("SELECT COALESCE(SUM(reserved),0) FROM token_calls WHERE role=? AND status='reserved'", (role,)).fetchone()[0]
    over = db.execute('SELECT 1 FROM token_alerts WHERE role=? AND resolved=0', (role,)).fetchone()
    allowed = bool(row) and not over and spent + reserved < row[0]
    if not allowed and task_id is not None:
        alert(db, role, task_id, 'admission_quota')
    return allowed


def cost_floor(db, lease_id):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='token_calls'").fetchone():
        return 0
    if db.execute("SELECT 1 FROM token_calls WHERE lease=? AND status='reserved'", (lease_id,)).fetchone():
        raise PolicyError('Provider usage unknown; retain lease until reconciliation')
    total = db.execute('SELECT COALESCE(SUM(actual),0) FROM token_calls WHERE lease=?', (lease_id,)).fetchone()[0]
    return (total + 9999) // 10000


def verify_final_cost(db, lease_id, cost_cents):
    if cost_cents < cost_floor(db, lease_id):
        raise PolicyError('Team receipt underreports the token ledger')


def call_record(db,call_id):
    row=db.execute('SELECT c.*,r.request FROM token_calls c JOIN token_requests r ON r.call_id=c.id WHERE c.id=?',(call_id,)).fetchone()
    if not row:return None
    value=dict(row)
    usage=db.execute('SELECT input_tokens,output_tokens,receipt_digest FROM token_usage WHERE call_id=?',(call_id,)).fetchone()
    value['usage']=canonical(dict(usage)) if usage else None
    return value


class TokenLedger:
    def __init__(self, database, *, clock=time.time):
        self.store, self.clock = SecurityStore(database), clock
        with self.store.edit() as db:
            rows.require(db)
            from pathlib import Path
            ddl = Path(rows.__file__).with_name('billing.sql').read_text()
            for statement in ddl.split(';'):
                if statement.strip():db.execute(statement)

    def set_limit(self, role, daily_micro_usd):
        """Trusted owner maintenance, not an agent-accessible budget increase."""
        identifier(role, 'role'); units(daily_micro_usd, 'daily quota')
        with self.store.edit() as db:
            db.execute('INSERT INTO token_limits VALUES(?,?) ON CONFLICT(role) DO UPDATE SET amount=excluded.amount', (role, daily_micro_usd))

    def reserve(self, call_id, ticket, route, *, max_input_tokens, max_output_tokens):
        identifier(call_id, 'call')
        units(max_input_tokens, 'max input'); units(max_output_tokens, 'max output')
        profile = route.profile
        prices = ((profile.batch_input_price, profile.batch_output_price) if route.mode == 'batch'
                  else (profile.input_price, profile.output_price))
        if None in prices:
            raise PolicyError('Selected model/mode has no approved pricing')
        maximum = charge(max_input_tokens, max_output_tokens, prices)
        request = {'lease': ticket['id'], 'input_digest': ticket['input_digest'],
            'role': ticket['role'], 'model': asdict(profile), 'mode': route.mode,
            'routing_policy': route.policy_digest, 'max_input': max_input_tokens,
            'max_output': max_output_tokens, 'prices': list(prices)}
        denied = False
        with self.store.edit() as db:
            # UTC day, expiry and rollback checks share the post-lock instant.
            now = timestamp(self.clock()); day = utc_day(now)
            old = call_record(db,call_id)
            if old:
                if old['request'] != canonical(request):
                    raise PolicyError('Call ID reused with different immutable input')
                raise CallAlreadyReserved('Call already recorded; read/reconcile it')
            state = db.execute('SELECT * FROM projects WHERE id=1').fetchone()
            if not state or now < state['last_time']:
                raise PolicyError('Metering clock moved behind Team clock')
            lease = rows.lease(db, ticket['id'])
            # Heartbeats extend the stored deadline while the worker retains
            # its original immutable ticket. Only that field may differ.
            same_ticket = lease is not None and {k:v for k,v in lease.items() if k != 'expires_at'} == {k:v for k,v in ticket.items() if k != 'expires_at'}
            if not same_ticket or lease['status'] != 'active' or lease['expires_at'] <= now:
                raise PolicyError('Metered call requires the exact live Team lease')
            rows.fence(db, ticket)
            if state['paused'] or state['budget_exceeded'] or rows.held(db,lease['task_id']):
                raise PolicyError('Task is held for a human')
            role = ticket['role']
            limit = db.execute('SELECT amount FROM token_limits WHERE role=?', (role,)).fetchone()
            spent = db.execute("SELECT COALESCE(SUM(actual),0) FROM token_calls WHERE role=? AND day=? AND status='settled'", (role,day)).fetchone()[0]
            outstanding = db.execute("SELECT COALESCE(SUM(reserved),0) FROM token_calls WHERE role=? AND status='reserved'", (role,)).fetchone()[0]
            lease_spent = db.execute("SELECT COALESCE(SUM(CASE WHEN status='reserved' THEN reserved ELSE actual END),0) FROM token_calls WHERE lease=?", (ticket['id'],)).fetchone()[0]
            blocked = db.execute('SELECT 1 FROM token_alerts WHERE role=? AND resolved=0', (role,)).fetchone()
            if not limit or blocked or spent + outstanding + maximum > limit[0] or lease_spent + maximum > ticket['reserved_cents'] * 10000:
                alert(db, role, ticket['task_id'], 'quota_exhausted')
                denied = True
            else:
                db.execute('INSERT INTO token_calls VALUES(?,?,?,?,?,?,?)',
                    (call_id, role, ticket['id'], day, 'reserved', maximum, 0))
                db.execute('INSERT INTO token_requests VALUES(?,?)',(call_id,canonical(request)))
        # Raising outside the transaction preserves the escalation event.
        if denied:
            raise QuotaExceeded('Role/day or Team lease budget exhausted; human decision required')
        return {'call_id': call_id, 'reserved_micro_usd': maximum}

    def settle(self, call_id, *, input_tokens, output_tokens, provider_receipt):
        """Trusted gateway only. Record overrun fully, then block future calls."""
        units(input_tokens, 'input tokens'); units(output_tokens, 'output tokens')
        if not isinstance(provider_receipt, str) or not 1 <= len(provider_receipt) <= 500:
            raise PolicyError('Verified provider receipt required')
        with self.store.edit() as db:
            row = call_record(db,call_id)
            if not row: raise PolicyError('Unknown call')
            usage = canonical({'input_tokens': input_tokens, 'output_tokens': output_tokens,
                               'receipt_digest': digest(provider_receipt)})
            if row['status'] == 'settled':
                if row['usage'] != usage: raise PolicyError('Conflicting usage replay')
                return row['actual']
            request = json.loads(row['request'])
            actual = charge(input_tokens, output_tokens, request['prices'])
            db.execute("UPDATE token_calls SET status='settled',actual=? WHERE id=?", (actual,call_id))
            db.execute('INSERT INTO token_usage VALUES(?,?,?,?)',(call_id,input_tokens,output_tokens,digest(provider_receipt)))
            if actual > row['reserved'] or input_tokens > request['max_input'] or output_tokens > request['max_output']:
                task = db.execute('SELECT task_id FROM leases WHERE id=?',(row['lease'],)).fetchone()[0]
                alert(db, row['role'], task, 'provider_overrun')
            return actual

    def get(self, call_id):
        with self.store.edit() as db:
            row = call_record(db,call_id)
            return dict(row) if row else None

    def lease_cost_cents(self, lease_id):
        with self.store.edit() as db:
            if db.execute("SELECT 1 FROM token_calls WHERE lease=? AND status='reserved'", (lease_id,)).fetchone():
                raise PolicyError('Unknown provider usage: reconcile before final receipt')
            value = db.execute('SELECT COALESCE(SUM(actual),0) FROM token_calls WHERE lease=?', (lease_id,)).fetchone()[0]
            return (value + 9999) // 10000

    def alerts(self):
        with self.store.edit() as db:
            return [dict(row) for row in db.execute('SELECT * FROM token_alerts WHERE resolved=0 ORDER BY id')]

    def resolve_alert(self, call_id, *, reason):
        if not isinstance(reason, str) or not reason.strip(): raise PolicyError('Owner rationale required')
        with self.store.edit() as db:
            if db.execute('UPDATE token_alerts SET resolved=1,resolution=? WHERE id=?', (reason,call_id)).rowcount != 1:
                raise PolicyError('Unknown quota alert')


class MeteredGateway:
    def __init__(self, ledger, router, *, team=None):
        from ..engine import Team
        self.ledger, self.router = ledger, router
        # Inject the owning Team when using an approved evolution catalog.
        # Packaged-catalog callers retain their existing two-argument API.
        self.team = team if team is not None else Team(ledger.store.database)
        from pathlib import Path
        if Path(self.team.database).resolve() != Path(ledger.store.database).resolve():
            raise PolicyError('Metering and action admission must share the Team database')

    from ..observability.otel import instrument
    @instrument('model.call', lambda self,call_id,ticket,metadata,**kw: dict(
        trace_id=execution_trace(ticket),
        attributes={'role':ticket['role'],'lease_id':ticket['id'],'generation':ticket['generation']}))
    async def call(self, call_id, ticket, metadata, *, max_input_tokens, max_output_tokens, invoke):
        route = self.router.route(metadata)
        if route.mode != 'realtime':
            raise PolicyError('Batch submission must use its durable outbox and explicit reservation')
        self.ledger.reserve(call_id, ticket, route, max_input_tokens=max_input_tokens, max_output_tokens=max_output_tokens)
        # invoke is an owner-owned provider adapter. It MUST enforce both token
        # ceilings and return authenticated usage, not model-supplied counters.
        # Any exception keeps the reservation: the provider might have charged.
        from ..security.action_admission import ActionAdmission, action_scope, admit_current
        admission = ActionAdmission(self.team, ticket, clock=self.ledger.clock)
        with action_scope(admission):
            # invoke begins an admitted in-flight request. A provider adapter with
            # its own queue MUST call admit_current again after that local wait.
            try:
                admit_current('provider:' + route.profile.name, 'model.call')
            except PolicyError:
                # The provider adapter has demonstrably not been entered. This
                # local rejection is distinct from every exception AFTER invoke.
                self.ledger.settle(call_id, input_tokens=0, output_tokens=0,
                    provider_receipt='controller:no-send:' + call_id)
                raise
            result, usage = await invoke(route, max_input_tokens, max_output_tokens)
        self.ledger.settle(call_id, **usage)
        return result
