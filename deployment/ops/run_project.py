"""Single trusted project pump. Re-entry never reissues an uncertain paid call."""
import json
from pathlib import Path
import sys
import time
import uuid

from adaptive_team.code_integration._git import Git
from adaptive_team.code_integration.worktrees import WorktreeManager
from adaptive_team.code_integration.commits import CommitManager
from adaptive_team.engine import Team
from adaptive_team.models import PolicyError, digest
from adaptive_team.orchestration.coordinator import Coordinator
from adaptive_team.orchestration.task_lifecycle import TaskLifecycle
from adaptive_team.portable import install_team
from adaptive_team.security.sandbox import DockerCLI, Sandbox, SandboxPolicy
from adaptive_team.security.input_snapshot import measure_input
from adaptive_team.security._store import SecurityStore
from deepseek_backend import Backend, IMAGE

REPOSITORY=Path('/work/repository')
WORKSPACES=Path('/work/workspaces')
CONTROL=Path('/state/git-control')

def prepare():
    REPOSITORY.mkdir(parents=True,exist_ok=True)
    git=Git(CONTROL)
    if not (REPOSITORY/'.git').exists():
        git.run(REPOSITORY,'init','-b','main')
        git.run(REPOSITORY,'remote','add','origin','https://github.com/al-gri/ai-test-app.git')
        (REPOSITORY/'README.md').write_text('# AI Test App\n\nA bounded DeepSeek-generated web calculator experiment.\n')
        (REPOSITORY/'.gitignore').write_text('.ai-team-state/\n.env*\n*.key\n*.sqlite*\n__pycache__/\n')
        install_team(REPOSITORY,'ai-test-app')
        (REPOSITORY/'PRODUCT_SPEC.md').write_text(Path('/opt/deployment/specification.txt').read_text())
        git.run(REPOSITORY,'add','--all')
        git.run(REPOSITORY,'commit','-m','Initialize portable team and owner-approved calculator brief')
    manager=WorktreeManager(REPOSITORY,WORKSPACES,CONTROL)
    commits=CommitManager(manager)
    return manager,commits

def sandbox(manager):
    docker=DockerCLI(endpoint='tcp://host.docker.internal:2375',
        config_directory='/opt/docker-empty',allow_local_tcp=True)
    return Sandbox(docker,manager.journal,workspace_root=WORKSPACES,
                   allowed_images=frozenset({IMAGE}))

def qualify():
    manager,commits=prepare()
    root=WORKSPACES/('qualification-'+uuid.uuid4().hex)
    root.mkdir()
    (root/'marker.txt').write_text('isolated')
    code="""from pathlib import Path
import socket
assert Path('/workspace/marker.txt').read_text() == 'isolated'
assert not Path('/credentials').exists()
assert not Path('/state/team.sqlite').exists()
assert not Path('/var/run/docker.sock').exists()
try:
 Path('/workspace/marker.txt').write_text('modified')
except OSError: pass
else: raise AssertionError('workspace is writable')
assert len(Path('/proc/net/route').read_text().splitlines()) == 1
print('readonly snapshot, absent secrets/state/socket, and no network routes: PASS')
"""
    policy=SandboxPolicy(IMAGE,('python','-c',code),timeout_seconds=15)
    evidence=sandbox(manager).run(root,policy,subject_digest=digest({'qualification':1}),
        input_digest=measure_input(root),trace_id=uuid.uuid4().hex)
    if evidence.console.exit_code or not evidence.stopped:
        raise PolicyError('Real Docker qualification failed')
    print(json.dumps({'qualification':'PASS','run_id':evidence.run_id,
                      'console':evidence.agent_result()}))
    (root/'app').mkdir()
    (root/'app/calculator.py').write_text('import os; os._exit(0)\n')
    policy=SandboxPolicy(IMAGE,('python','-c',Path('/opt/deployment/harness.py').read_text()),timeout_seconds=15)
    negative=sandbox(manager).run(root,policy,subject_digest=digest({'exit_bypass':1}),
        input_digest=measure_input(root),trace_id=uuid.uuid4().hex)
    if not negative.stopped or negative.console.exit_code==0:
        raise PolicyError('Early-exit acceptance bypass detected')
    print('Early-exit candidate cannot produce a passing test receipt: PASS')

def recover_plan():
    from dataclasses import asdict
    from adaptive_team.models import Task
    team=Team('/state/team.sqlite');state=team.snapshot()
    if 'calculator-recovery' in state['tasks']:
        raise PolicyError('Recovery plan already exists; preserve attempt limits')
    original=state['tasks']['calculator']
    if original['status']!='escalated' or any(x['status']=='active' for x in state['leases'].values()):
        raise PolicyError('Recovery requires stopped exhausted original task')
    manager,commits=prepare()
    first=next(h for h in original['history'] if h['phase']=='implement' and h['outcome']=='success')
    receipt=manager.journal.get('execute:'+first['lease'])['result']
    spec=dict(original['spec'])
    spec.update(id='calculator-recovery',title='Complete calculator from the passing baseline',max_attempts=2,
        context={'seed_artifact':receipt['artifact_digest'],
                 'diagnosis':'Original reviewer included withdrawn hypotheses as defects. Start from the first '
                 'tested baseline. Make minimal justified changes only. Never replace `with localcontext() as ctx` '
                 'with `ctx=localcontext(); ctx.prec=...`: ctx is a manager, not decimal.Context. '
                 'Avoid broad speculative rewrites. All original acceptance checks still apply.'})
    team.extend([asdict(Task(**spec))],state['plan_digest'],
        'Bounded architect recovery after model review contradictions; keep original history and total $5 cap')
    print('Added bounded recovery task; previous attempts and billing preserved.')

def select_task(state, requested=None):
    task_id=requested or ('calculator-recovery' if 'calculator-recovery' in state['tasks'] else 'calculator')
    task=state['tasks'].get(task_id)
    if task is None or task['status'] in ('blocked','escalated','running','reviewing'):
        raise PolicyError('Selected task is not runnable; no work dispatched')
    return task_id

def run(task_id=None):
    if not Path('/credentials/deepseek.key').is_file():
        raise PolicyError('DeepSeek key must be provisioned locally first')
    # One pump across all launches. This is an owner-process mutex, not the
    # operational Team database lock, and never blocks UI transactions.
    with SecurityStore('/state/project-pump.sqlite').edit():
        team=Team('/state/team.sqlite')
        state=team.snapshot()
        task_id=select_task(state,task_id)
        if any(x['status']=='active' for x in state['leases'].values()):
            raise PolicyError('Existing active lease requires reconciliation, not a new paid call')
        manager,commits=prepare()
        target='refs/heads/ai-team/ai-test-app'
        existing=manager.git.run(REPOSITORY,'for-each-ref','--format=%(refname)',target).decode().strip()
        if not existing:
            commits.initialize_target('ai-test-app',manager.resolve('main'))
        backend=Backend(team,manager.journal,sandbox(manager),commits)
        coordinator=Coordinator(team,commits,backend,target_ref=target)
        team.pause(False,'Owner authorized calculator and total $5 budget; bounded backend ready')
        try:
            deadline=time.monotonic()+1200
            while time.monotonic()<deadline:
                result=coordinator.tick()
                if result['errors']:
                    raise PolicyError('Executor error retained in its journal; no blind retry')
                state=team.snapshot()
                task=state['tasks'][task_id]
                if task['status']=='accepted':
                    def verify_merge(path,commit,head,checks):
                        if set(checks) != {'unit','scope'}:
                            raise PolicyError('Unsupported integration verification check')
                        tree=manager.git.run(path,'rev-parse','HEAD^{tree}').decode().strip()
                        ev,passed=backend.test(path,digest({'commit':commit,'tree':tree}),uuid.uuid4().hex)
                        return {'commit':commit,'tree':tree,'expected_head':head,'actual_cost_cents':0,
                            'checks':{name:'PASS' if passed else 'FAIL' for name in checks},
                            'evidence':['sandbox://'+ev.run_id]}
                    integrated=coordinator.integrate_accepted(task_id,
                        required_checks=('unit',),verify_merged=verify_merge)
                    # Export only the verified tree, never the controller database
                    # or private keys. Publisher later handles remote GitHub state.
                    manager.git.run(REPOSITORY,'archive','--format=tar',
                        '--output=/work/verified-project.tar',integrated['commit'])
                    Path('/state/result.json').write_text(json.dumps({
                        'status':'accepted','commit':integrated['commit'],
                        'artifact_digest':integrated['artifact_digest'],
                        'spent_cents_upper_estimate':state['spent_cents']}))
                    print('Calculator accepted and integrated. Verified export ready.')
                    return
                if task['status'] in ('blocked','escalated'):
                    raise PolicyError('Task needs owner review; automatic work stopped')
                time.sleep(.3)
            raise PolicyError('Controller run deadline reached; reconcile outstanding work')
        finally:
            team.pause(True,'Test run stopped; no unattended provider calls')
            coordinator.close(wait=True)

if __name__=='__main__':
    try:
        command=sys.argv[1] if len(sys.argv)>1 else 'run'
        if command=='run': run(sys.argv[2] if len(sys.argv)>2 else None)
        else: {'prepare':prepare,'qualify':qualify,'recover-plan':recover_plan}[command]()
    except Exception as exc:
        # Raw provider text and credentials never appear in the console.
        print('Stopped safely: '+type(exc).__name__+'. Inspect private state for recovery.',file=sys.stderr)
        raise SystemExit(1)
