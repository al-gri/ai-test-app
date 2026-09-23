# Calculator experiment — 2026-09-23

Final outcome: accepted, independently reviewed, merged locally, and exported.

- Core: adaptive-ai-team 1.1.1.
- Model: deepseek-flash, real authenticated API calls (not a simulation).
- Local verified merge: `3a4b1623318730da373b6c95929c12e6a0746903`.
- Accepted artifact: `f9f21297d5bb374cedbe36ba3c9966aef7c943cfdcceb7bf04830560a77d7d19`.
- Seven deployment adapter boundary tests passed.
- Real Docker qualification passed: read-only snapshot, absent keys/state/socket,
  no network routes, and rejection of an early-exit test-bypass fixture.
- Six application acceptance tests passed for the accepted candidate and merged
  revision. Parameterized cases include 11 arithmetic expressions and 14 invalid
  inputs. HTTP tests cover assets, calculations, bad requests and inaccessible files.
- Browser check passed: button-based `0.1+0.2` produced `0.3`; keyboard `1/0`
  displayed `Division by zero`; Escape cleared the expression. Desktop appearance
  was visually inspected. Mobile CSS is present; no physical-device test is claimed.
- Dashboard login, DAG, ledger and Inbox API reads returned HTTP 200.

## Spending

Eight paid calls: four implementation calls and four review calls. Provider usage:
73,019 input and 54,955 output tokens, including the truncated reasoning attempt.
Conservative peak/cache-miss ledger estimate: **$0.087856**, of the authorized **$5**.
Role estimates: developer $0.044847; reviewer $0.043009. Team's integer-cent
per-lease accounting reports $0.13 because it rounds each lease upward. Neither
number is an exact provider invoice; caching and off-peak discounts are not applied.

## Failures retained in the audit trail

The original task stopped after three implementation attempts. The initial
candidate passed tests, but contradictory non-thinking review suggestions led to
failed repairs. A separate bounded recovery task restarted from the verified
baseline without deleting history or increasing the project budget. One malformed
review and one output-truncated review were classified as missing evidence and
charged. A fresh review with low reasoning effort and a larger bounded output
allowance approved the exact candidate. No failed review was converted to approval.

The original escalated task remains visible in the dashboard. The accepted
recovery task supersedes it for this experiment. The controller is now paused;
there are no unattended model requests. Persistent UI and watchdog remain running.

This report covers this deployment run, not a rerun of the core's entire release
test suite and not an enterprise-readiness certification. The public GitHub commit
can differ from the local merge because the publisher adds documentation and the
deployment package; the five application files retain their verified contents.
