"""Trusted local provisioning. Credentials never enter project Git or worker input."""
import json
import os
import secrets
import sys
from pathlib import Path

from adaptive_team.engine import Team
from adaptive_team.models import Policy, Task, plan_dict
from adaptive_team.llmops.token_ledger import TokenLedger

DATABASE = Path('/state/team.sqlite')

def provision():
    from adaptive_team.security.key_provider import create_keyring
    Path('/state').mkdir(exist_ok=True)
    Path('/credentials').mkdir(mode=0o700, exist_ok=True)
    key = Path('/credentials/keyring.json')
    if not key.exists():
        create_keyring(key)
    login = Path('/credentials/dashboard.json')
    if not login.exists():
        with os.fdopen(os.open(login, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as f:
            json.dump({'actor': 'local-owner', 'login_token': secrets.token_urlsafe(32),
                       'signing_key': secrets.token_hex(32)}, f)
    if not DATABASE.exists():
        team = Team(DATABASE)
        team.initialize(plan_dict('ai-test-app', Policy(
            budget_cents=500, max_active=1, max_implementers=1, max_reviewers=1,
            allowed_roles=['fullstack_engineer', 'code_reviewer'],
            retry_backoff_seconds=2), [Task(
                id='calculator', title='Build an accessible web calculator',
                role='fullstack_engineer', reviewer_role='code_reviewer',
                acceptance=['Decimal arithmetic with + - * /, parentheses and unary signs',
                            'No eval/exec; reject invalid expressions and division by zero',
                            'Responsive English UI with keyboard support and clear errors'],
                checks=['unit', 'scope'], write_scopes=['app'],
                attempt_budget_cents=100, review_budget_cents=50, max_attempts=3,
                context={'product': 'Local test web calculator',
                         'repository': 'https://github.com/al-gri/ai-test-app',
                         'model': 'deepseek-flash'})]))
        team.pause(True, 'Waiting for local DeepSeek key and verified executor setup')
        ledger = TokenLedger(DATABASE)
        ledger.set_limit('fullstack_engineer', 3_500_000)
        ledger.set_limit('code_reviewer', 1_500_000)
    print('Local project initialized; total project budget $5. No provider call made.')

def set_key():
    value = sys.stdin.readline().strip()
    if not 12 <= len(value) <= 512 or any(c.isspace() for c in value):
        raise SystemExit('Invalid key format; nothing saved')
    target = Path('/credentials/deepseek.key')
    temp = target.with_suffix('.new')
    with os.fdopen(os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as f:
        f.write(value); f.flush(); os.fsync(f.fileno())
    os.replace(temp, target)
    print('DeepSeek key stored privately. No paid request made.')

def serve():
    from adaptive_team.control_plane.app import create_app
    import uvicorn
    data = json.loads(Path('/credentials/dashboard.json').read_text())
    app = create_app(DATABASE, login_token=data['login_token'],
                     signing_key=bytes.fromhex(data['signing_key']), actor=data['actor'])
    # The container listens internally; Compose publishes only host loopback.
    uvicorn.run(app, host='0.0.0.0', port=8765, access_log=False,
                proxy_headers=False, workers=1, log_level='warning')

if __name__ == '__main__':
    {'init': provision, 'set-key': set_key, 'serve': serve}[sys.argv[1]]()
