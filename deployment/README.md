# Local Adaptive AI Team deployment

This directory installs the **1.1.1 core** on the current Windows/Docker Desktop
machine and connects a bounded calculator coding/review adapter to **DeepSeek
Flash**. It is an integration deployment, not a new core release or a general
unattended developer service.

## Daily use

Run these scripts from PowerShell:

```powershell
& .\Start-Team.ps1
& .\Open-Dashboard.ps1
```

The dashboard is at http://127.0.0.1:8765. `Open-Dashboard.ps1` copies its login
token to your clipboard; paste it into the login form. The UI's inherited
`1.1.0-rc1` caption is historical; the installed package is 1.1.1.

`Connect-DeepSeek.ps1` securely prompts for a key and stores it in a private Docker
volume. Do not paste keys into chat, GitHub or a shell command. `Run-Calculator.ps1`
starts the bounded project pump. It selects the recovery task when one exists;
it does not reset history, quotas, exhausted attempts or unknown calls.

`Stop-Team.ps1` stops the UI and watchdog, preserving volumes. Stop the runner
first; the watchdog must remain alive while candidate containers are running.

## Boundaries

- Project lifetime budget: **$5**. Per-role daily limits: developer $3.50,
  reviewer $1.50. Project lifetime accounting still applies after midnight.
- All provider requests reserve token cost before admission. Known responses use
  an encrypted recovery outbox. Unknown calls remain reserved and need maintenance.
- Conservative tariff: $0.30 per million input and $1.20 per million output tokens,
  DeepSeek peak/cache-miss prices checked on 2026-09-23. Displayed cost is an upper
  estimate; provider invoices can be lower due to caching/off-peak rates.
- Two active specializations, one worker at a time. The portable catalog contains
  102 roles but is not fully activated for a small calculator.
- Model output can change exactly five `app/` files. It has no shell, keys, Docker
  endpoint, control database, Git credentials or publishing authority.
- Trusted tests execute against an immutable snapshot in a non-root, read-only,
  resource-limited Docker container with no external network. An independent
  supervised watchdog reaps expired candidate containers.
- Tests and code review reduce risk; they do not prove arbitrary hostile code is
  safe or establish enterprise readiness. The local HTTP preview is a test app.

## State and portability

Docker volumes `ai-test-app_state`, `ai-test-app_credentials`, `ai-test-app_work`
hold operational state, keys and Git/worktrees separately. Never commit or expose
these volumes. The verified export contains `.ai-team/` with the portable runtime,
role catalog and prompts; it deliberately excludes live leases, bills and keys.
The private SQLite state remains authoritative for this machine's run history.

## Rebuild on this machine

`python build_local.py` builds offline from the pinned previously qualified local
controller image and the `wheels/` directory. Wheels are excluded from Git. This is
an offline deployment bundle for this PC; a different machine must provision the
base images and trusted wheels before building. The calculator itself has no
third-party runtime dependencies.

## Recovery

Do not delete SQLite rows or repeatedly restart a failed paid call. Review its
encrypted operation journal and token reservation first. The checked-in
`reconcile_review.py` is a narrowly bound maintenance command for one observed
malformed review; it is not an automatic recovery service. Recovery attempts are
separate tasks created through the validated plan-extension API. Failed attempts
and their costs remain visible.

The first experiment demonstrated why human/architect supervision is still
necessary: non-thinking review produced contradictory findings. Its rejected
proposals were retained; a bounded follow-up used the tested baseline and enabled
reasoning for the independent review. No malformed response was converted into an
approval.

Pricing source: https://api-docs.deepseek.com/quick_start/pricing/
