"""Project intake, research dossier and authenticated owner acceptance.

Produces research assignments/templates, never fabricated market research.
Trusted production adapters should enter via start_approved, not raw Team.init.
"""
from __future__ import annotations

# Keep product-discovery APIs intact. Protocol discovery is a distinct registry
# whose trust and matching rules live in protocols/a2a/discovery.py.
from .protocols.a2a.discovery import AgentRegistry, Requirements, Selection

import math
import time
from datetime import date
from dataclasses import asdict
from urllib.parse import urlparse

from .evolution import verify
from .models import PolicyError, digest, identifier, integer, validate_plan


TRACKS = {
    "users": ("user_researcher", "Audience, user tasks, problems, and demand-validation methods"),
    "market": ("market_researcher", "Market segments, geography, trends, and audience reachability"),
    "competition": ("competitive_analyst", "Direct competitors, indirect solutions, substitutes, prices, and unmet needs"),
    "strategy": ("product_strategy_analyst", "Product differentiation, value, revenue model, and testable hypotheses"),
    "technology": ("technology_researcher", "Feasibility, stack options, versions, licenses, and total cost of ownership"),
    "experience": ("ux_designer", "Key scenarios, UX, accessibility, and localization"),
    "security": ("security_architect", "Data, threats, access boundaries, privacy, and applicable requirements"),
    "economics": ("cost_analyst", "Development and operating costs, limitations, and estimate sensitivity"),
    "delivery": ("chief_architect", "Risks, dependencies, MVP, stages, team, checks, release, and recovery"),
    "existing_system": ("solution_architect", "Existing-project inventory, behavior, tests, dependencies, and a safe change path"),
}
SECTIONS = ("problem", "audience", "value", "differentiation", "mvp", "non_goals", "journeys",
            "options_and_tradeoffs", "architecture", "security_privacy", "success_metrics",
            "acceptance", "budget_and_schedule", "delivery_and_team", "risks", "release_and_rollback")


def intake(brief: dict) -> dict:
    if not isinstance(brief, dict):
        raise PolicyError("Brief must be an object")
    project = identifier(brief.get("project"), "project")
    if not isinstance(brief.get("goal"), str) or not brief["goal"].strip():
        raise PolicyError("Describe what result the project should achieve")
    mode, scale = brief.get("mode", "greenfield"), brief.get("scale", "product")
    if mode not in {"greenfield", "brownfield"} or scale not in {"script", "small", "product", "enterprise"}:
        raise PolicyError("Unknown project mode/scale")
    questions = [{"field": key, "question": label} for key, label in (
        ("users", "Who is this for, and what problem are we solving?"),
        ("constraints", "What are the timeline, budget, constraints, and required platforms?"),
        ("success", "How will we verify usefulness and task completion?"),
    ) if not brief.get(key)]
    selected = set(TRACKS) - {"existing_system"}
    if scale == "script":
        selected = {"users", "technology", "security", "delivery"}
    if mode == "brownfield":
        selected.add("existing_system")
    tracks = [{"id": key, "role": role, "question": question, "status": "pending" if key in selected else "not_applicable",
               "rationale": "" if key in selected else "Reduced research route for a bounded script" if scale == "script" else "New project with no existing system",
               "evidence_ids": [], "conclusion": ""} for key, (role, question) in TRACKS.items()]
    return {"schema_version": 1, "project": project, "mode": mode, "scale": scale,
            "brief": brief, "questions": questions, "research_tracks": tracks,
            "research_budget_cents": integer(brief.get("research_budget_cents", 1000), "research budget", 1),
            "research_rules": ["Label facts, inferences, and hypotheses separately", "Provide sources and dates for changing information",
                               "Do not invent interviews, figures, competitors, or verification results",
                               "Research does not authorize product release or changes to a live system"]}


def dossier_template(brief: dict) -> dict:
    request = intake(brief)
    return {"schema_version": 1, "project": request["project"], "mode": request["mode"],
            "brief": brief, "research": request["research_tracks"], "evidence": [],
            "sections": {key: "" for key in SECTIONS}, "open_questions": request["questions"],
            "blocking_gaps": ["Complete the research and specification for approval"],
            "assumptions": [], "decision": "draft"}


def validate_dossier(dossier: dict) -> None:
    identifier(dossier.get("project"), "project")
    request = intake(dossier.get("brief"))
    if request["project"] != dossier["project"] or request["mode"] != dossier.get("mode"):
        raise PolicyError("Dossier identity differs from its brief")
    if dossier.get("schema_version") != 1 or dossier.get("mode") not in {"greenfield", "brownfield"}:
        raise PolicyError("Invalid dossier schema/mode")
    if dossier.get("decision") != "ready_for_approval" or dossier.get("blocking_gaps") or dossier.get("open_questions"):
        raise PolicyError("Resolve blocking gaps/questions before approval")
    sections = dossier.get("sections", {})
    if any(not isinstance(sections.get(key), str) or not sections[key].strip() for key in SECTIONS):
        raise PolicyError("Approval packet is incomplete")
    evidence = dossier.get("evidence", [])
    if not isinstance(evidence, list):
        raise PolicyError("Evidence must be a list")
    ids = set()
    for item in evidence:
        eid = identifier(item.get("id"), "evidence")
        if eid in ids:
            raise PolicyError("Duplicate evidence ID")
        ids.add(eid)
        if item.get("kind") not in {"fact", "inference", "hypothesis"} or not item.get("claim"):
            raise PolicyError("Evidence must distinguish facts, inferences and hypotheses")
        parsed = urlparse(item.get("source", ""))
        if parsed.scheme not in {"https", "repo", "interview"} or not (parsed.netloc or parsed.path):
            raise PolicyError("Evidence needs an accessible source reference")
        try:
            checked = date.fromisoformat(item.get("checked_at", ""))
        except (ValueError, TypeError) as exc:
            raise PolicyError("Evidence needs an ISO verification date") from exc
        if checked > date.today():
            raise PolicyError("Evidence cannot be verified in the future")
        integer(item.get("max_age_days", 30), "source freshness window", 1)
        if item.get("max_age_days", 30) > 90:
            raise PolicyError("Freshness window exceeds trusted maximum of 90 days")
        if (date.today() - checked).days > item.get("max_age_days", 30):
            raise PolicyError("Evidence freshness window expired; recheck the claim")
    tracks = dossier.get("research", [])
    if not isinstance(tracks, list) or len(tracks) != len(TRACKS) or {x.get("id") for x in tracks} != set(TRACKS):
        raise PolicyError("Every research direction needs a conclusion or explicit exclusion")
    for track in tracks:
        if track.get("status") == "not_applicable":
            if not track.get("rationale") or (dossier["mode"] == "brownfield" and track["id"] == "existing_system"):
                raise PolicyError("Research exclusion needs rationale; brownfield inventory is mandatory")
        elif track.get("status") == "complete":
            refs = track.get("evidence_ids", [])
            if not track.get("conclusion") or not refs or set(refs) - ids:
                raise PolicyError("Research conclusion lacks evidence")
        else:
            raise PolicyError("Research remains incomplete")


def render_dossier(dossier: dict) -> str:
    lines = ["# Product Proposal: " + dossier["project"], "", "Revision for approval: `" + digest(dossier) + "`", ""]
    for key, value in dossier["sections"].items():
        lines += ["## " + key.replace("_", " "), "", value or "To be completed", ""]
    lines += ["## Research and Rationale", ""]
    for track in dossier["research"]:
        lines += [f"- {track['id']}: {track['status']}. {track.get('conclusion') or track.get('rationale') or 'Research pending'}"]
    lines += ["", "## Sources", ""]
    for item in dossier.get("evidence", []):
        lines.append(f"- {item['id']} — {item['kind']}: {item['claim']} [{item['checked_at']}]({item['source']})")
    lines += ["", "## Open Questions and Limitations", "", str(dossier.get("open_questions", [])), str(dossier.get("blocking_gaps", [])), ""]
    return "\n".join(lines)


def start_approved(team, plan: dict, dossier: dict, approval: dict, authorities: dict, *, now=None,
                   plan_evidence: dict | None = None) -> dict:
    """Authenticate owner acceptance of *both* dossier and exact executable plan.

    Low-level Team remains a trusted library, not an authorization boundary.
    Never expose raw init/extend/DB/signing helpers as tools to model workers.
    """
    validate_dossier(dossier)
    policy, tasks = validate_plan(plan, team.roles)
    if asdict(policy) != plan["policy"] or [asdict(task) for task in tasks] != plan["tasks"]:
        raise PolicyError("Owner must approve a fully expanded plan with explicit policy/task defaults")
    owner, payload = verify(approval, authorities, "approve_product")
    now = time.time() if now is None else now
    expiry = payload.get("expires_at")
    if type(expiry) not in (int, float) or not math.isfinite(expiry) or expiry <= now:
        raise PolicyError("Owner acceptance expired")
    if payload.get("dossier_digest") != digest(dossier) or payload.get("plan_digest") != digest(plan) or plan.get("project") != dossier["project"] or payload.get("project") != dossier["project"]:
        raise PolicyError("Owner acceptance does not bind this product and plan")
    if payload.get("decision") != "approve":
        raise PolicyError("Owner has not approved development")
    if payload.get("catalog_digest") != digest(team.roles):
        raise PolicyError("Role/prompt catalog changed since owner approval")
    return team.initialize(plan, now=now, plan_evidence=plan_evidence,
        approval_record={"owner": owner, "dossier_digest": digest(dossier),
        "plan_digest": digest(plan), "attestation_digest": digest(approval)},
        project_context={"mode": dossier["mode"], "dossier_digest": digest(dossier),
                         "goal": dossier["brief"]["goal"], "constraints": dossier["brief"].get("constraints", ""),
                         "mvp": dossier["sections"]["mvp"], "non_goals": dossier["sections"]["non_goals"]})
