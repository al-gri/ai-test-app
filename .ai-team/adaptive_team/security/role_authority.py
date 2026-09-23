"""Operational role authority. Call only as owner-governed maintenance.

No worker, model proposal, remote Agent Card or learning restore may write these
tables. CAS generation + immutable revision + fencing commit in ONE transaction.
This counter is deliberately absent from the rollbackable learning capsule.
"""
import copy
import json
import uuid
from ..models import PolicyError, canonical, digest, identifier

def current(db,role_id,expected_generation):
    identifier(role_id,'role')
    if type(expected_generation) is not int or expected_generation<0:
        raise PolicyError('Expected exact nonnegative generation')
    row=db.execute('SELECT r.*,v.bundle,v.card FROM roles r JOIN role_versions v ON v.role_id=r.id AND v.generation=r.generation WHERE r.id=?',(role_id,)).fetchone()
    if (row['generation'] if row else 0)!=expected_generation:
        raise PolicyError('Role generation changed; stale maintenance request')
    return row

def install_role(db,role_id,bundle,*,expected_generation,reason,at,
                 allow_authority_change=False,require_revision=True,revoked=False):
    if not isinstance(reason,str) or not 1<=len(reason)<=2000:raise PolicyError('Bounded maintenance reason required')
    old=current(db,role_id,expected_generation)
    bundle=copy.deepcopy(bundle);card=bundle.get('role',{})
    if (card.get('id')!=role_id or type(card.get('version')) is not int or card['version']<1
            or card.get('status') not in {'active','trial','deprecated','retired'}
            or not isinstance(bundle.get('instructions'),str) or not 1<=len(bundle['instructions'])<=24000):
        raise PolicyError('Invalid role revision')
    if old:
        baseline=json.loads(old['bundle'])
        if require_revision and card['version']!=old['version']+1:raise PolicyError('Role version must advance exactly once')
        if not allow_authority_change:
            before,after=copy.deepcopy(baseline),copy.deepcopy(bundle)
            for item in (before,after):
                item.pop('instructions',None);item['role'].pop('version',None)
            if before!=after:raise PolicyError('Prompt deployment cannot expand or alter authority')
    elif not allow_authority_change:
        raise PolicyError('New role requires explicit owner catalog governance')
    generation=expected_generation+1
    if old:
        changed=db.execute('UPDATE roles SET generation=?,version=?,status=?,revoked=? WHERE id=? AND generation=?',
            (generation,card['version'],card['status'],int(revoked),role_id,expected_generation)).rowcount
        if changed!=1:raise PolicyError('Concurrent role update')
    else:db.execute('INSERT INTO roles VALUES(?,?,?,?,?)',(role_id,generation,card['version'],card['status'],int(revoked)))
    prompt_digest=digest(bundle)
    published={**card,'generation':generation,'prompt_digest':prompt_digest}
    db.execute('INSERT INTO role_versions VALUES(?,?,?,?,?,?,?)',
        (role_id,generation,card['version'],prompt_digest,canonical(published),canonical(bundle),reason))
    # Retain resources and all money until independently confirmed termination.
    # Expired is intentionally still a live resource owner in Team._active.
    db.execute("UPDATE leases SET status='expired' WHERE role=? AND status='active'",(role_id,))
    db.execute('INSERT INTO role_changes VALUES(?,?,?,?,?)',(uuid.uuid4().hex,role_id,generation,at,reason))
    return {**published,'generation':generation,'revoked':bool(revoked)}

def revoke_role(db,role_id,*,expected_generation,reason,at):
    old=current(db,role_id,expected_generation)
    if not old:raise PolicyError('Unknown role')
    return install_role(db,role_id,json.loads(old['bundle']),expected_generation=expected_generation,
        reason=reason,at=at,require_revision=False,revoked=True)
