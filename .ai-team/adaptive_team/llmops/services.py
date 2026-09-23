"""Coordinator pump for quota escalation; never waits for a human synchronously."""
from pathlib import Path
from ..models import PolicyError, digest


class LLMOpsServices:
    def __init__(self, team, inbox, ledger, *, evaluations=None):
        if inbox.team is not team or Path(ledger.store.database).resolve() != Path(team.database).resolve():
            raise PolicyError('Inbox, ledger and Team must share operational state')
        self.team, self.inbox, self.ledger = team, inbox, ledger
        self.evaluations = evaluations

    def sync(self):
        for alert in self.ledger.alerts():
            state = self.team.snapshot()
            request_id = 'budget-' + digest(alert['id'])[:40]
            old = state.get('human_requests', {}).get(request_id)
            if old and old['status'] == 'approve':
                # Approval acknowledges the alert; it does not increase a limit.
                # If budget is still unavailable the next call remains blocked.
                self.ledger.resolve_alert(alert['id'], reason='Human approval: ' + old['response_digest'])
                continue
            if old or alert['task'] in state.get('human_holds', {}):
                continue
            if any(x['task_id'] == alert['task'] for x in self.team._active(state)):
                continue  # Stop/reconcile first; never release a live worker.
            try:
                self.inbox.request(request_id, alert['task'],
                    reason='Role token quota requires owner review: ' + alert['role'],
                    subject_digest=digest(alert))
            except PolicyError:
                # A dispatch/request may have won the transaction after snapshot.
                # The durable alert is retained for the next pump.
                pass
        result = {'inbox': self.inbox.inbox(), 'quota_alerts': self.ledger.alerts()}
        if self.evaluations is not None:
            result['evaluations'] = self.evaluations.tick()
        return result

    def close(self, *, wait=True):
        if self.evaluations is not None:
            self.evaluations.close(wait=wait)
