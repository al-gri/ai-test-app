from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

from .engine import Team, catalog
from .architect import OpenRouterPlanner, propose
from .models import PolicyError
from .planner import explain_plan, plan_brief
from .runner import simulate
from .portable import install_team, install_evolved_team, export_team, import_team, verify_team
from .discovery import intake, dossier_template, render_dossier, start_approved
from .evolution import Evolution


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def output(value, path=None):
    text = json.dumps(value, indent=2, ensure_ascii=False) + "\n"
    if path:
        Path(path).write_text(text, encoding="utf-8")
    else:
        print(text)


def authorities(path):
    result = {}
    for actor, config in read(path).items():
        key = os.environ.get(config["key_env"], "")
        if len(key) < 32:
            raise PolicyError("Host authority key must be at least 32 characters: " + config["key_env"])
        result[actor] = {"key": key.encode(), "permissions": config["permissions"]}
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Adaptive AI team — local trusted control plane")
    parser.add_argument("--registry", help="Approved Evolution database (trusted control plane only)")
    parser.add_argument("--authorities", help="Actor permissions + environment-variable names; never raw keys")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("roles")
    p = sub.add_parser("plan")
    p.add_argument("brief")
    p.add_argument("--out")
    p = sub.add_parser("explain")
    p.add_argument("plan")
    p = sub.add_parser("propose")
    p.add_argument("brief")
    p.add_argument("--model", required=True)
    p.add_argument("--api-key-env", default="OPENROUTER_API_KEY")
    p.add_argument("--out")
    p = sub.add_parser("init")
    p.add_argument("database")
    p.add_argument("plan")
    for name in ("status", "events", "dispatch", "simulate"):
        p = sub.add_parser(name)
        p.add_argument("database")
        p.add_argument("--out")
    p = sub.add_parser("finish")
    p.add_argument("database")
    p.add_argument("receipt")
    p = sub.add_parser("heartbeat")
    p.add_argument("database")
    p.add_argument("lease_id")
    p.add_argument("actor_id")
    p = sub.add_parser("recover")
    p.add_argument("database")
    p.add_argument("lease_id")
    p.add_argument("--termination-evidence", required=True)
    p = sub.add_parser("resume-task")
    p.add_argument("database")
    p.add_argument("task_id")
    p.add_argument("--reason", required=True)
    p = sub.add_parser("authorize-roles")
    p.add_argument("database")
    p.add_argument("roles", nargs="+")
    p.add_argument("--expected-policy-digest", required=True)
    p.add_argument("--reason", required=True)
    p = sub.add_parser("install-evolved")
    p.add_argument("repository")
    p.add_argument("project_id")
    p.add_argument("attestation")
    p.add_argument("--mode", choices=["greenfield", "brownfield"], default="brownfield")
    for name in ("install", "import"):
        p = sub.add_parser(name)
        if name == "import":
            p.add_argument("archive")
        p.add_argument("repository")
        p.add_argument("project_id")
        p.add_argument("--mode", choices=["greenfield", "brownfield"], default="brownfield")
    p = sub.add_parser("export")
    p.add_argument("repository")
    p.add_argument("archive")
    p = sub.add_parser("verify-install")
    p.add_argument("repository")
    for name in ("intake", "dossier"):
        p = sub.add_parser(name)
        p.add_argument("brief")
        p.add_argument("--out")
    p = sub.add_parser("render-dossier")
    p.add_argument("dossier")
    p.add_argument("out")
    p = sub.add_parser("start-approved")
    p.add_argument("database")
    p.add_argument("plan")
    p.add_argument("dossier")
    p.add_argument("approval")
    p = sub.add_parser("evolve")
    p.add_argument("request", help="JSON operation: seed/status/propose/evaluate/lifecycle/activate/rollback")
    p.add_argument("--out")
    p = sub.add_parser("adopt-catalog")
    p.add_argument("database")
    p.add_argument("expected_digest")
    p.add_argument("--reason", required=True)
    for name in ("pause", "unpause"):
        p = sub.add_parser(name)
        p.add_argument("database")
        p.add_argument("--reason", required=True)
    p = sub.add_parser("extend")
    p.add_argument("database")
    p.add_argument("tasks")
    p.add_argument("--expected-plan-digest", required=True)
    p.add_argument("--reason", required=True)
    args = parser.parse_args(argv)
    try:
        auth = authorities(args.authorities) if args.authorities else {}
        registry = Evolution(args.registry, auth) if args.registry else None
        selected_catalog = Team(":memory:", evolution=registry).roles if registry and args.command in {"roles", "plan", "explain", "propose"} else None
        if args.command == "roles":
            result = list((selected_catalog if selected_catalog is not None else catalog()).values())
        elif args.command == "plan":
            result = plan_brief(read(args.brief), role_catalog=selected_catalog)
        elif args.command == "explain":
            result = explain_plan(read(args.plan), role_catalog=selected_catalog)
        elif args.command == "propose":
            chief_prompt = registry.snapshot()["active"]["chief_architect"]["instructions"] if registry else None
            result = propose(read(args.brief), OpenRouterPlanner(args.model, args.api_key_env), role_catalog=selected_catalog, chief_prompt=chief_prompt)
        elif args.command == "install":
            result = install_team(args.repository, args.project_id, args.mode)
        elif args.command == "install-evolved":
            if registry is None:
                raise PolicyError("--registry is required")
            result = install_evolved_team(registry, args.repository, args.project_id, read(args.attestation), auth, args.mode)
        elif args.command == "export":
            result = export_team(args.repository, args.archive)
        elif args.command == "import":
            result = import_team(args.archive, args.repository, args.project_id, args.mode)
        elif args.command == "verify-install":
            result = verify_team(args.repository)
        elif args.command in {"intake", "dossier"}:
            result = (intake if args.command == "intake" else dossier_template)(read(args.brief))
        elif args.command == "render-dossier":
            Path(args.out).write_text(render_dossier(read(args.dossier)), encoding="utf-8")
            print(args.out)
            return 0
        elif args.command == "evolve":
            if registry is None:
                raise PolicyError("--registry is required")
            request = read(args.request)
            operation = request.get("operation")
            if operation == "seed":
                registry.seed(Team(":memory:").bundles)
                result = {"seeded": True}
            elif operation == "status":
                result = registry.snapshot()
            elif operation == "audit-queue":
                result = registry.audit_queue()
            elif operation in {"observe", "close-audit"}:
                result = getattr(registry, "close_audit" if operation == "close-audit" else operation)(request["attestation"])
            elif operation == "propose":
                result = registry.propose(request["bundle"], request["author"], request["reason"])
            elif operation in {"evaluate", "lifecycle", "activate", "rollback"}:
                method = getattr(registry, "evaluate_lifecycle" if operation == "lifecycle" else operation)
                result = method(request["candidate_id"], request["attestation"])
            else:
                raise PolicyError("Unknown evolution operation")
        else:
            team = Team(args.database, evolution=registry)
            match args.command:
                case "init":
                    team.initialize(read(args.plan))
                    result = team.snapshot()["summary"]
                case "start-approved":
                    start_approved(team, read(args.plan), read(args.dossier), read(args.approval), auth)
                    result = team.snapshot()["summary"]
                case "adopt-catalog":
                    result = team.adopt_catalog(args.expected_digest, args.reason)
                case "authorize-roles":
                    result = team.authorize_roles(args.roles, args.expected_policy_digest, args.reason)
                case "status":
                    result = team.snapshot()
                case "events":
                    result = team.events()
                case "dispatch":
                    result = team.dispatch()
                case "simulate":
                    if team.events() != [] and any(event["kind"] not in {"initialized"} for event in team.events()):
                        raise PolicyError("Simulation requires a fresh dedicated database; never mix real and synthetic receipts")
                    result = simulate(team)
                case "finish":
                    result = team.finish(read(args.receipt))
                case "heartbeat":
                    result = team.heartbeat(args.lease_id, args.actor_id)
                case "recover":
                    result = team.recover_expired(args.lease_id, args.termination_evidence)
                case "resume-task":
                    team.resume_task(args.task_id, args.reason)
                    result = {"resumed": args.task_id}
                case "pause" | "unpause":
                    team.pause(args.command == "pause", args.reason)
                    result = {"paused": args.command == "pause"}
                case "extend":
                    team.extend(read(args.tasks), args.expected_plan_digest, args.reason)
                    result = {"extended": True}
        output(result, getattr(args, "out", None))
        return 0
    except (PolicyError, ValueError, OSError, sqlite3.Error, KeyError, TypeError) as exc:
        print(f"adaptive-team: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
