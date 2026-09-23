"""Durable human Inbox in the SAME transaction as Team admission holds.

This is a service API, not an unauthenticated HTTP endpoint. The API gateway
authenticates the human and signs the exact response off-repository. Workers
must never receive the gateway key or access Team's database.
"""
from __future__ import annotations
import copy
import re
from ..models import PolicyError, digest, identifier
from ..evolution import verify


def held_tasks(state):
    """Transitive closure also blocks descendants of an already accepted node."""
    held = set(state.get('human_holds', {}))
    changed = True
    while changed:
        before = len(held)
        held.update(key for key, task in state['tasks'].items()
                    if held.intersection(task['spec']['dependencies']))
        changed = len(held) != before
    return held


def binding(state, task_id):
    task = state['tasks'][task_id]
    return digest({'project': state['project'], 'plan': state['plan_digest'],
        'policy': state['policy_digest'], 'roles': {
            role: state['role_registry'][role]['generation'] for role in
            {task['spec']['role'],task['spec']['reviewer_role']}},
        'task': task})


class HumanRequests:
    def __init__(self, team, authorities):
        self.team, self.authorities = team, authorities

    def request(self, request_id, task_id, *, reason, subject_digest, now=None):
        identifier(request_id, 'human request'); identifier(task_id, 'task')
        if not isinstance(reason, str) or not 1 <= len(reason) <= 2000:
            raise PolicyError('A bounded human-readable reason is required')
        if not isinstance(subject_digest, str) or not re.fullmatch('[0-9a-f]{64}', subject_digest):
            raise PolicyError('Human request must bind the reviewed plan/artifact')
        with self.team._edit(now) as (state, db, at):
            requests = state.setdefault('human_requests', {})
            holds = state.setdefault('human_holds', {})
            proposed = {'task_id': task_id, 'reason': reason, 'subject_digest': subject_digest}
            if request_id in requests:
                old = requests[request_id]
                if any(old[k] != v for k, v in proposed.items()):
                    raise PolicyError('Conflicting human-request replay')
                return copy.deepcopy(old)
            if task_id not in state['tasks'] or task_id in holds:
                raise PolicyError('Unknown task or existing hold; use replace_request')
            if state.get('integration_inflight'):
                raise PolicyError('Integration is already admitted; wait before placing a new hold')
            if any(lease['task_id'] == task_id for lease in self.team._active(state)):
                raise PolicyError('Stop and reconcile the worker before requesting human input')
            if len(requests) >= 10000:
                raise PolicyError('Inbox maintenance required')
            item = {'id': request_id, 'project': state['project'], **proposed,
                'binding': binding(state, task_id), 'status': 'pending',
                'created_at': at, 'choices': ['approve', 'reject', 'return']}
            requests[request_id] = item
            holds[task_id] = request_id
            state['action_generation'] = state.get('action_generation', 0) + 1
            self.team._event(db, at, 'human.requested', {'request_id': request_id, 'task': task_id})
            return copy.deepcopy(item)

    def respond(self, envelope, *, now=None):
        actor, answer = verify(envelope, self.authorities, 'human_decision')
        required = {'request_id', 'binding', 'subject_digest', 'decision', 'comment'}
        if set(answer) not in (required, required | {'display_digest'}):
            raise PolicyError('Human response fields differ from schema')
        if answer['decision'] not in {'approve', 'reject', 'return'} or not isinstance(answer['comment'], str) or len(answer['comment']) > 2000:
            raise PolicyError('Invalid decision or comment')
        with self.team._edit(now) as (state, db, at):
            item = state.get('human_requests', {}).get(answer['request_id'])
            if not item or any(item[k] != answer[k] for k in ('binding', 'subject_digest')):
                raise PolicyError('Response does not bind this Inbox item')
            if 'display_digest' in answer:
                # Check the exact displayed evidence under the SAME write lock as
                # hold release. A newer view must never be silently substituted.
                evidence = db.execute('SELECT binding,subject,display_digest FROM ui_evidence WHERE request_id=?',
                    (answer['request_id'],)).fetchone()
                if not evidence or tuple(evidence) != (answer['binding'],answer['subject_digest'],answer['display_digest']):
                    raise PolicyError('Displayed evidence changed; review again')
            response_digest = digest(envelope)
            if item['status'] != 'pending':
                if item.get('response_digest') == response_digest:
                    return copy.deepcopy(item)
                raise PolicyError('Human response already finalized')
            task_id = item['task_id']
            if binding(state, task_id) != item['binding'] or state['human_holds'].get(task_id) != item['id']:
                raise PolicyError('Task/plan changed; renew human approval')
            item.update(status=answer['decision'], responder=actor,
                        response_digest=response_digest, comment=answer['comment'], resolved_at=at)
            item['response_envelope'] = copy.deepcopy(envelope)
            if answer['decision'] == 'approve':
                del state['human_holds'][task_id]
                state['action_generation'] = state.get('action_generation', 0) + 1
                task = state['tasks'][task_id]
                if task['status'] == 'blocked' and task['blocker'] == 'external_blocker':
                    if task['result'] is not None or task['attempts'] < task['spec']['max_attempts']:
                        task['status'] = 'review_ready' if task['result'] else 'pending'
                        task['ready_after'], task['blocker'], task['reason'] = at, None, 'Human approved: ' + item['id']
                # Approval only releases this hold. It cannot alter budgets,
                # revive failed work, approve code, or bypass any existing gate.
            self.team._event(db, at, 'human.responded', {'request_id': item['id'], 'decision': answer['decision']})
            return copy.deepcopy(item)

    def replace_request(self, old_id, new_id, *, reason, subject_digest, now=None):
        """Owner maintenance after reject/return/stale binding; never opens a gap.

        Nested request transactions are avoided: replace under the Team lock.
        A replacement is always pending and must receive a fresh signed response.
        """
        identifier(new_id, 'human request')
        if not isinstance(reason, str) or not 1 <= len(reason) <= 2000 or not isinstance(subject_digest, str) or not re.fullmatch('[0-9a-f]{64}', subject_digest):
            raise PolicyError('Invalid replacement request')
        with self.team._edit(now) as (state, db, at):
            requests = state.get('human_requests', {})
            old = requests.get(old_id)
            if not old or new_id in requests or state.get('human_holds', {}).get(old['task_id']) != old_id:
                raise PolicyError('Replacement requires the current held request and a new ID')
            if len(requests) >= 10000:
                raise PolicyError('Inbox maintenance required')
            item = {'id': new_id, 'project': state['project'], 'task_id': old['task_id'],
                'reason': reason, 'subject_digest': subject_digest,
                'binding': binding(state, old['task_id']), 'status': 'pending',
                'created_at': at, 'choices': ['approve', 'reject', 'return']}
            old['superseded_by'] = new_id
            old['status'] = 'superseded'
            requests[new_id] = item
            state['human_holds'][old['task_id']] = new_id
            self.team._event(db, at, 'human.replaced', {'request_id': new_id, 'previous': old_id})
            return copy.deepcopy(item)

    def inbox(self):
        return [copy.deepcopy(item) for item in self.team.snapshot().get('human_requests', {}).values()
                if item['status'] in {'pending', 'reject', 'return'}]
