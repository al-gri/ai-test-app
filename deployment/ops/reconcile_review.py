"""One explicit recovery for the observed completed-but-malformed review.

Never converts model text to approval. The next review has a fresh lease/context.
"""
import json
import sys
from adaptive_team.engine import Team
from adaptive_team.llmops.token_ledger import TokenLedger
from adaptive_team.orchestration.task_lifecycle import TaskLifecycle
from adaptive_team.models import PolicyError
from adaptive_team.security.sandbox import DockerCLI

docker=DockerCLI(endpoint='tcp://host.docker.internal:2375',config_directory='/opt/docker-empty',allow_local_tcp=True)
stopped_run=sys.argv[1] if len(sys.argv)>1 else 'ai-test-app-reviewed-run'
if stopped_run not in ('ai-test-app-recovery-execution','ai-test-app-reviewed-run'):
    raise PolicyError('Unknown maintenance target')
container=json.loads(docker.command(['inspect',stopped_run]))[0]
if container['State']['Running'] or container['State']['ExitCode']!=1:
    raise PolicyError('Original controller termination has not been confirmed')
team=Team('/state/team.sqlite');state=team.snapshot()
active=[x for x in state['leases'].values() if x['status']=='active']
if len(active)!=1 or active[0]['phase']!='review' or active[0]['task_id']!='calculator-recovery':
    raise PolicyError('Only the observed incomplete review may be reconciled')
lease=active[0];ledger=TokenLedger(team.database)
journal=TaskLifecycle('/state/git-control/operations.sqlite')
call=journal.get('deepseek-'+lease['id'])
if not call or call['status']!='complete' or ledger.get('deepseek-'+lease['id'])['status']!='settled':
    raise PolicyError('Provider response and usage must already be known')
choice=call['result']['result']['choices'][0]
proposal=json.loads(choice['message']['content']) if choice['finish_reason']=='stop' else {}
if set(proposal)=={'verdict','findings'}:
    raise PolicyError('This recovery only classifies a malformed review')
receipt={'lease_id':lease['id'],'actor_id':lease['actor_id'],'input_digest':lease['input_digest'],
    'actual_cost_cents':ledger.lease_cost_cents(lease['id']),'outcome':'missing_evidence',
    'evidence':['journal://deepseek-'+lease['id'],'docker://'+stopped_run+'/exited-1'],
    'termination_evidence':'Docker inspection confirms controller stopped; provider response completed and usage settled',
    'summary':'Malformed contradictory review rejected; exact tested candidate preserved'}
journal.finish('execute:'+lease['id'],receipt)
team.finish(receipt)
if sum(h['phase']=='review' for h in team.snapshot()['tasks']['calculator-recovery']['history'])>=3:
    raise PolicyError('Maintenance review retry cap reached; keep task blocked')
team.resume_task('calculator-recovery','Bounded independent review with low reasoning effort, explicit output budget and strict output contract')
print('Malformed review recorded as missing evidence. No approval synthesized and no bill discarded.')
