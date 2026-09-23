"""Paired prompt evaluation using trusted black-box cases and real sandboxes.

The generator is an owner-owned, metered provider adapter. It gets the task and
prompt, never expected output. Generated code is data written to a fresh worktree;
it is executed ONLY by Sandbox. Approval remains a separate signed operation.
"""
from __future__ import annotations
import copy
import json
import re
import time
import uuid
from pathlib import Path
from dataclasses import dataclass, asdict, replace
from ..models import PolicyError, digest, canonical, identifier
from ..evolution import attest
from ..memory.supervisor import assert_same_authority
from ..security.sandbox import Sandbox, SandboxPolicy
from ..code_integration._git import checked_path
from ..orchestration.task_lifecycle import TaskLifecycle, RecoveryRequired
from .token_ledger import units


@dataclass(frozen=True)
class BenchmarkCase:
    id: str
    problem: str
    argv: tuple[str, ...]
    expected_stdout: str
    critical: bool = False

    def __post_init__(self):
        identifier(self.id, 'benchmark case')
        if not isinstance(self.problem, str) or not 1 <= len(self.problem) <= 16000:
            raise PolicyError('Bounded benchmark problem required')
        if not isinstance(self.argv, tuple) or len(self.argv) > 50 or any(not isinstance(x,str) or len(x)>1000 or '\0' in x for x in self.argv):
            raise PolicyError('Bounded benchmark arguments required')
        if not isinstance(self.expected_stdout, str) or len(self.expected_stdout) > 16000 or type(self.critical) is not bool:
            raise PolicyError('Invalid expected output or critical flag')


@dataclass(frozen=True)
class GeneratedProgram:
    source: str
    cost_micro_usd: int
    provider_receipt_digest: str

    def __post_init__(self):
        if not isinstance(self.source, str) or not 1 <= len(self.source.encode('utf-8')) <= 256000:
            raise PolicyError('Generated program must be bounded UTF-8 source')
        units(self.cost_micro_usd, 'generation cost')
        if not isinstance(self.provider_receipt_digest, str) or not re.fullmatch('[0-9a-f]{64}', self.provider_receipt_digest):
            raise PolicyError('Generation must bind verified provider usage')


class Evaluations:
    def __init__(self, evolution, sandbox: Sandbox, journal: TaskLifecycle, *,
                 policy: SandboxPolicy, cases: tuple[BenchmarkCase, ...],
                 conditions: dict, evaluator: str, signing_key: bytes):
        if not isinstance(sandbox, Sandbox):
            raise PolicyError('A real Sandbox adapter is required')
        if not isinstance(cases, tuple) or not 2 <= len(cases) <= 1000 or any(not isinstance(c, BenchmarkCase) for c in cases) or len({c.id for c in cases}) != len(cases):
            raise PolicyError('At least two unique owner-held benchmark cases required')
        authority = evolution.authorities.get(evaluator, {})
        if authority.get('key') != signing_key or 'evaluate' not in authority.get('permissions', []):
            raise PolicyError('Independent evaluation credentials required')
        if not isinstance(conditions, dict) or not conditions or len(canonical(conditions)) > 32000:
            raise PolicyError('Pin model, tools and generation budgets in conditions')
        self.evolution, self.sandbox, self.journal = evolution, sandbox, journal
        self.policy, self.cases = policy, cases
        self.conditions = copy.deepcopy(conditions)
        self.evaluator, self.key = evaluator, signing_key

    def _epoch(self):
        state = getattr(self.evolution, 'learning_state', None)
        return state.epoch if state is not None else None

    def run(self, run_id, candidate_id, *, generate, trace_id):
        """generate(bundle, problem, operation_id) -> authenticated GeneratedProgram.

        No retries after a crash/unknown provider outcome. A persisted report can
        be resubmitted without regenerating code; incomplete runs need reconciliation.
        """
        identifier(run_id, 'evaluation run')
        state = self.evolution.snapshot()
        candidate = state['candidates'].get(candidate_id)
        if not candidate or candidate['author'] == self.evaluator:
            raise PolicyError('Unknown candidate or author cannot self-evaluate')
        baseline = state['active'].get(candidate['role_id'])
        if digest(baseline) != candidate['baseline_digest']:
            raise PolicyError('Rebase stale candidate before evaluating')
        assert_same_authority(baseline, candidate['bundle'])
        epoch = self._epoch()
        request = {'candidate_id': candidate_id, 'candidate_digest': candidate['candidate_digest'],
            'baseline_digest': candidate['baseline_digest'], 'epoch': epoch,
            'dataset_digest': digest([asdict(c) for c in self.cases]),
            'conditions_digest': digest({'conditions': self.conditions, 'policy': asdict(self.policy)}),
            'trace_id': trace_id}
        operation = 'evaluation:' + run_id
        previous = self.journal.get(operation)
        if previous:
            if previous['request'] != request: raise PolicyError('Evaluation run ID reused')
            if previous['status'] == 'complete': return previous['result']
            if (previous.get('checkpoint') or {}).get('report'):
                return copy.deepcopy(previous['checkpoint']['report'])
            raise RecoveryRequired('Evaluation outcome unknown: reconcile without rerunning paid work')
        if candidate['status'] != 'proposed':
            raise PolicyError('Only proposed prompts may start evaluation')
        saved = self.journal.begin(operation, request)
        if saved is not None:
            return saved  # Another controller completed after our optimistic read.
        rows, refs = [], []
        for index, case in enumerate(self.cases):
            results = {}
            # Alternate order to reduce systematic warm-up bias. This is not a
            # substitute for owner-designed sampling, repetitions and held-out data.
            for variant in (('baseline','candidate') if index % 2 == 0 else ('candidate','baseline')):
                if self._epoch() != epoch: raise PolicyError('Learning rollback invalidated evaluation')
                bundle = baseline if variant == 'baseline' else candidate['bundle']
                call_id = 'eval-' + digest({'run': run_id, 'case': case.id, 'variant': variant})[:40]
                started = time.monotonic()
                program = generate(copy.deepcopy(bundle), case.problem, call_id)
                if not isinstance(program, GeneratedProgram): raise PolicyError('Trusted generation receipt required')
                generation_seconds = time.monotonic() - started
                workspace = checked_path(self.sandbox.workspace_root / ('eval-' + uuid.uuid4().hex))
                workspace.mkdir()
                (workspace / 'solution.py').write_text(program.source, encoding='utf-8')
                subject = digest({'source': program.source, 'prompt': digest(bundle), 'case': case.id,
                                  'dataset': request['dataset_digest']})
                policy = replace(self.policy, command=('python', '/workspace/solution.py', *case.argv))
                started = time.monotonic()
                from ..security.input_snapshot import measure_input
                expected_input = measure_input(workspace)
                evidence = self.sandbox.run(workspace, policy, subject_digest=subject, input_digest=expected_input, trace_id=trace_id)
                elapsed = generation_seconds + time.monotonic() - started
                valid = (evidence.input_digest == expected_input and evidence.subject_digest == subject and evidence.policy_digest == digest(asdict(policy))
                         and evidence.stopped and not evidence.timed_out and not evidence.output_limited)
                passed = valid and evidence.console.exit_code == 0 and evidence.console.stdout == case.expected_stdout
                results[variant] = {'score': int(passed), 'cost': program.cost_micro_usd, 'latency': elapsed,
                    'valid': valid, 'run_id': evidence.run_id, 'source_digest': digest(program.source),
                    'receipt': program.provider_receipt_digest}
                refs.append('sandbox://' + evidence.run_id)
            row = {'id': case.id, 'baseline': results['baseline']['score'], 'candidate': results['candidate']['score'],
                'baseline_cost': results['baseline']['cost'], 'candidate_cost': results['candidate']['cost'],
                'baseline_latency': results['baseline']['latency'], 'candidate_latency': results['candidate']['latency'],
                'critical_pass': results['baseline']['valid'] and results['candidate']['valid']
                                 and (not case.critical or results['candidate']['score'] == 1)}
            rows.append(row)
            self.journal.checkpoint(operation, {'completed_cases': len(rows), 'last_evidence': results})
        if self._epoch() != epoch: raise PolicyError('Learning rollback invalidated evaluation')
        report = {'candidate_id': candidate_id, 'baseline_digest': request['baseline_digest'],
            'candidate_digest': request['candidate_digest'], 'conditions_digest': request['conditions_digest'],
            'dataset_digest': request['dataset_digest'], 'held_out': True, 'trusted_checks': True,
            'evidence_refs': refs, 'cases': rows}
        if epoch is not None: report['epoch'] = epoch
        envelope = attest(report, self.evaluator, self.key)
        # Store BEFORE acknowledgment. No repeated generation if finish crashes.
        self.journal.checkpoint(operation, {'report': envelope})
        self.journal.finish(operation, envelope)
        return envelope

    def submit(self, envelope):
        """Idempotent delivery after crash; activation remains separate."""
        from ..evolution import verify
        actor, report = verify(envelope, self.evolution.authorities, 'evaluate')
        epoch = self._epoch()
        if epoch is not None and report.get('epoch') != epoch:
            raise PolicyError('Stale evaluation epoch')
        candidate_id = report['candidate_id']
        candidate = self.evolution.snapshot()['candidates'].get(candidate_id)
        normalized = attest({k:v for k,v in report.items() if k != 'epoch'}, actor,
                            self.evolution.authorities[actor]['key']) if epoch is not None else envelope
        if candidate and candidate.get('evaluation_digest') == digest(normalized):
            return {'candidate_id': candidate_id, 'status': candidate['status'],
                    'evaluation_digest': candidate['evaluation_digest']}
        self.evolution.evaluate(candidate_id, envelope)
        stored = self.evolution.snapshot()['candidates'][candidate_id]
        return {'candidate_id': candidate_id, 'status': stored['status'], 'evaluation_digest': stored['evaluation_digest']}


class EvaluationQueue:
    """One bounded background evaluator; Coordinator admission never awaits models.

    Queue claims and outcomes are durable. A crash/unknown result is retained for
    maintenance, not automatically retried. Successful evaluation still requires
    a separate independent activation signature.
    """
    def __init__(self, evaluations: Evaluations, generate):
        from concurrent.futures import ThreadPoolExecutor
        self.evaluations, self.generate = evaluations, generate
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='prompt-eval')
        self.future = None

    def tick(self):
        if self.future is not None:
            if not self.future.done(): return {'status': 'running'}
            self.future.result()  # Infrastructure persistence errors are visible.
            self.future = None
        evaluator = self.evaluations
        for candidate_id, candidate in evaluator.evolution.snapshot()['candidates'].items():
            if candidate['status'] != 'proposed': continue
            request = {'candidate': candidate_id, 'epoch': evaluator._epoch(),
                'cases': digest([asdict(c) for c in evaluator.cases]),
                'conditions': digest(evaluator.conditions), 'policy': digest(asdict(evaluator.policy))}
            # This operational claim survives learning rollback AND changes of
            # dataset/policy. A new epoch cannot authorize a second paid attempt.
            # Owner reconciliation may explicitly run a new manual run ID.
            run_id = 'auto-' + candidate_id
            key = 'eval-queue:' + run_id
            if evaluator.journal.get(key): continue
            try:
                existing = evaluator.journal.begin(key, request)
                if existing is not None: continue
            except RecoveryRequired:
                continue  # Another controller owns this exact evaluation.
            self.future = self.pool.submit(self._run, key, run_id, candidate_id)
            return {'status': 'submitted', 'candidate_id': candidate_id, 'run_id': run_id}
        return {'status': 'idle'}

    def _run(self, key, run_id, candidate_id):
        evaluator = self.evaluations
        try:
            report = evaluator.run(run_id, candidate_id, generate=self.generate,
                                   trace_id=digest({'evaluation': run_id})[:32])
            result = evaluator.submit(report)
        except Exception as exc:
            # Never store provider text/keys or manufacture successful scores.
            result = {'candidate_id': candidate_id, 'status': 'needs_review',
                      'error_type': type(exc).__name__}
        evaluator.journal.finish(key, result)
        return result

    def close(self, *, wait=True):
        self.pool.shutdown(wait=wait, cancel_futures=False)


def main():
    """Owner CI entry point. Factory code is trusted deployment configuration."""
    import argparse
    import importlib
    parser = argparse.ArgumentParser(description='Run and submit an independently signed paired prompt evaluation')
    parser.add_argument('--factory', required=True, help='Owner module:function returning (Evaluations, generate)')
    parser.add_argument('--candidate', required=True)
    parser.add_argument('--run', required=True)
    parser.add_argument('--trace', required=True)
    args = parser.parse_args()
    module, name = args.factory.split(':', 1)
    evaluator, generate = getattr(importlib.import_module(module), name)()
    if not isinstance(evaluator, Evaluations): raise PolicyError('Factory did not supply Evaluations')
    envelope = evaluator.run(args.run, args.candidate, generate=generate, trace_id=args.trace)
    print(json.dumps(evaluator.submit(envelope), sort_keys=True))


if __name__ == '__main__':
    main()
