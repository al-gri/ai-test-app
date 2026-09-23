from __future__ import annotations

from dataclasses import asdict

from .engine import catalog
from .models import Policy, PolicyError, Task, identifier, integer, plan_dict, validate_plan


FEATURE_ROLES = {
    "android": "android_engineer", "ios": "ios_engineer", "web": "frontend_engineer",
    "backend": "backend_engineer", "data": "data_engineer", "ai": "ml_engineer",
    "desktop": "desktop_engineer", "embedded": "embedded_engineer", "plc": "plc_engineer",
    "integration": "integration_engineer", "graphics": "graphics_engineer",
}
SENSITIVE = {"payments", "personal_data", "authentication", "destructive", "production_access"}
EXTRA = {"accessibility", "localization", "performance", "offline"}


def plan_brief(brief: dict, *, role_catalog=None) -> dict:
    """Deterministic baseline; the architect may replace it with a validated task DAG.

    Classification is explicit. Natural-language goal text never grants rights.
    These plans are templates, not a substitute for project-specific acceptance.
    """
    allowed = {"project", "goal", "kind", "features", "risk", "budget_cents", "max_active"}
    if set(brief) - allowed:
        raise PolicyError("Unknown brief fields")
    project = identifier(brief.get("project"), "project")
    goal = brief.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        raise PolicyError("A concrete goal is required")
    kind = brief.get("kind")
    if kind not in {"script", "calculator", "website", "android", "large_product"}:
        raise PolicyError("Unsupported kind; use an explicit validated plan for other projects")
    features = brief.get("features", [])
    if not isinstance(features, list) or not all(isinstance(x, str) for x in features):
        raise PolicyError("features must be a list")
    if set(features) - (FEATURE_ROLES.keys() | SENSITIVE | EXTRA):
        raise PolicyError("Unknown feature: declare its role in an explicit plan")
    features = set(features)
    risk = brief.get("risk", "low")
    if risk not in {"low", "medium", "high"}:
        raise PolicyError("Unknown risk")
    if features & SENSITIVE:
        risk = "high" if features & {"destructive", "production_access", "payments"} else "medium" if risk == "low" else risk
    maximum = {"script": 1, "calculator": 1, "website": 3, "android": 3, "large_product": 8}[kind]
    # Requested maximum is an owner-approved ceiling, not the desired team size.
    maximum = brief.get("max_active", maximum)
    integer(maximum, "max_active", 1)
    policy = Policy(max_active=maximum, max_implementers=max(1, maximum - 1),
                    max_reviewers=max(1, min(3, maximum)),
                    budget_cents=brief.get("budget_cents", 2000 if kind != "large_product" else 20000))
    tasks: list[Task] = []
    if kind in {"script", "calculator"}:
        policy.max_active = policy.max_implementers = policy.max_reviewers = 1
        role = "developer" if kind == "script" else "android_engineer" if "android" in features else "frontend_engineer"
        tasks.append(Task(
            id="implement", title=goal, role=role, write_scopes=["src", "tests"], risk=risk,
            review_required=(kind != "script" or risk != "low"),
            acceptance=[goal, "Explicit input/output behavior and negative cases are demonstrated",
                        "No changes outside the bounded scope; no secrets or production access"],
            checks=["unit", "scope"],
        ))
    else:
        if kind == "website":
            features.add("web")
        if kind == "android":
            features.add("android")
        if kind == "large_product" and not features & FEATURE_ROLES.keys():
            features |= {"web", "backend", "data"}
        tasks.append(Task(
            id="contracts", title="Fix component contracts for: " + goal, role="api_architect",
            write_scopes=["specs"], risk="medium", reviewer_role="architecture_reviewer",
            acceptance=["Interfaces, owners, scope and explicit non-goals are defined", "Each downstream task has measurable acceptance"],
            checks=["contract_consistency"], group="architecture", priority=100,
        ))
        for feature in sorted(features & FEATURE_ROLES.keys()):
            tasks.append(Task(
                id="build-" + feature, title=f"Implement the bounded {feature} slice: {goal}",
                role=FEATURE_ROLES[feature], dependencies=["contracts"], risk=risk,
                write_scopes=[f"src/{feature}", f"tests/{feature}"], group=feature,
                acceptance=["Implementation matches the accepted contracts", "Feature-specific success and failure cases pass"],
                checks=["unit", "contract", "scope"], attempt_budget_cents=200,
            ))
        implementations = [t.id for t in tasks if t.id.startswith("build-")]
        extras = []
        if features & SENSITIVE:
            extras.append(("security-check", "security_reviewer", "security_evidence"))
        if "accessibility" in features:
            extras.append(("accessibility-check", "accessibility_specialist", "accessibility_evidence"))
        if "performance" in features:
            extras.append(("performance-check", "performance_engineer", "performance_evidence"))
        if "localization" in features:
            extras.append(("localization-check", "localization_specialist", "localization_evidence"))
        if "offline" in features:
            extras.append(("offline-check", "mobile_qa", "offline_evidence"))
        for task_id, role, check in extras:
            tasks.append(Task(id=task_id, title=task_id + " for the implemented slices", role=role,
                              dependencies=implementations, acceptance=["Evidence covers the declared requirements and exact artifacts"],
                              checks=[check], risk="medium", reviewer_role="acceptance_reviewer", group="quality",
                              write_scopes=["locales"] if task_id == "localization-check" else []))
        tasks.append(Task(
            id="integration", title="Verify the combined product: " + goal, role="test_automation_engineer",
            write_scopes=["tests/integration"], dependencies=implementations + [x[0] for x in extras],
            acceptance=["Combined behavior satisfies the goal", "Results identify exact artifact versions and limitations"],
            checks=["integration", "acceptance", "scope"], risk="medium", reviewer_role="acceptance_reviewer", group="quality",
        ))
    plan = plan_dict(project, policy, tasks)
    validate_plan(plan, catalog() if role_catalog is None else role_catalog)
    return plan


def explain_plan(plan: dict, *, role_catalog=None) -> dict:
    selected_catalog = catalog() if role_catalog is None else role_catalog
    policy, tasks = validate_plan(plan, selected_catalog)
    roles = sorted({t.role for t in tasks} | {t.reviewer_role for t in tasks if t.review_required})
    return {
        "project": plan["project"], "tasks": len(tasks), "selected_roles": roles,
        "catalog_roles": len(selected_catalog), "active_ceiling": policy.max_active,
        "budget_ceiling_cents": policy.budget_cents,
        "solo_tasks": [t.id for t in tasks if not t.review_required],
        "note": "Roles are activated on demand; the ceiling is not a staffing target. Check goal-specific acceptance before live execution.",
    }
