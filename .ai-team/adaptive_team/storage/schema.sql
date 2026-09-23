-- One operational database per project. Explicit keys survive a PostgreSQL port.
-- Cold JSON documents are bounded records, never the complete project aggregate.
CREATE TABLE IF NOT EXISTS schema_version (component TEXT PRIMARY KEY, version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS projects (
 id INTEGER PRIMARY KEY CHECK(id=1), project TEXT NOT NULL UNIQUE,
 schema_version INTEGER NOT NULL, created_at REAL NOT NULL, last_time REAL NOT NULL,
 paused INTEGER NOT NULL CHECK(paused IN (0,1)), pause_reason TEXT NOT NULL,
 spent_cents INTEGER NOT NULL CHECK(spent_cents>=0),
 budget_exceeded INTEGER NOT NULL CHECK(budget_exceeded IN (0,1)),
 plan_digest TEXT NOT NULL, policy_digest TEXT NOT NULL, catalog_digest TEXT NOT NULL,
 action_generation INTEGER NOT NULL DEFAULT 0 CHECK(action_generation>=0));
CREATE TABLE IF NOT EXISTS project_documents (
 project TEXT NOT NULL REFERENCES projects(project), name TEXT NOT NULL,
 document TEXT NOT NULL, PRIMARY KEY(project,name));
CREATE TABLE IF NOT EXISTS roles (
 id TEXT PRIMARY KEY, generation INTEGER NOT NULL CHECK(generation>=1),
 version INTEGER NOT NULL CHECK(version>=1), status TEXT NOT NULL CHECK(status IN ('active','trial','deprecated','retired')),
 revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN (0,1)),
 FOREIGN KEY(id,generation) REFERENCES role_versions(role_id,generation) DEFERRABLE INITIALLY DEFERRED);
CREATE TABLE IF NOT EXISTS role_versions (
 role_id TEXT NOT NULL REFERENCES roles(id), generation INTEGER NOT NULL CHECK(generation>=1),
 version INTEGER NOT NULL CHECK(version>=1), prompt_digest TEXT NOT NULL,
 card TEXT NOT NULL, bundle TEXT NOT NULL, reason TEXT NOT NULL,
 PRIMARY KEY(role_id,generation));
CREATE TABLE IF NOT EXISTS tasks (
 id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(project),
 role TEXT NOT NULL REFERENCES roles(id), reviewer_role TEXT NOT NULL REFERENCES roles(id),
 status TEXT NOT NULL CHECK(status IN ('pending','running','review_ready','reviewing','accepted','blocked','escalated')),
 attempts INTEGER NOT NULL CHECK(attempts>=0), created_at REAL NOT NULL,
 ready_after REAL NOT NULL, reason TEXT NOT NULL, blocker TEXT);
CREATE INDEX IF NOT EXISTS tasks_ready ON tasks(status,ready_after);
CREATE TABLE IF NOT EXISTS task_specs (task_id TEXT PRIMARY KEY REFERENCES tasks(id), document TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS task_dependencies (
 task_id TEXT NOT NULL REFERENCES tasks(id), parent_id TEXT NOT NULL REFERENCES tasks(id),
 ordinal INTEGER NOT NULL CHECK(ordinal>=0), CHECK(task_id<>parent_id),
 PRIMARY KEY(task_id,parent_id), UNIQUE(task_id,ordinal));
CREATE INDEX IF NOT EXISTS task_children ON task_dependencies(parent_id,task_id);
CREATE TABLE IF NOT EXISTS task_results (task_id TEXT PRIMARY KEY REFERENCES tasks(id), document TEXT NOT NULL);
-- Last coordinator-confirmed integration receipt, not a live Git HEAD assertion.
CREATE TABLE IF NOT EXISTS task_integrations (
 task_id TEXT PRIMARY KEY REFERENCES tasks(id), artifact_digest TEXT NOT NULL, commit_oid TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS task_history (
 task_id TEXT NOT NULL REFERENCES tasks(id), ordinal INTEGER NOT NULL,
 document TEXT NOT NULL, PRIMARY KEY(task_id,ordinal));
CREATE TABLE IF NOT EXISTS task_findings (
 task_id TEXT NOT NULL REFERENCES tasks(id), ordinal INTEGER NOT NULL,
 document TEXT NOT NULL, PRIMARY KEY(task_id,ordinal));
CREATE TABLE IF NOT EXISTS leases (
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), role TEXT NOT NULL,
 generation INTEGER NOT NULL, actor_id TEXT NOT NULL UNIQUE,
 phase TEXT NOT NULL CHECK(phase IN ('implement','review')),
 status TEXT NOT NULL CHECK(status IN ('active','expired','complete','abandoned')),
 expires_at REAL NOT NULL, reserved_cents INTEGER NOT NULL CHECK(reserved_cents>=0),
 receipt_digest TEXT, result_status TEXT, UNIQUE(id,role),
 FOREIGN KEY(role,generation) REFERENCES role_versions(role_id,generation));
CREATE INDEX IF NOT EXISTS leases_live ON leases(status,role,expires_at);
CREATE UNIQUE INDEX IF NOT EXISTS task_single_live_lease ON leases(task_id) WHERE status IN ('active','expired');
CREATE TABLE IF NOT EXISTS lease_inputs (lease_id TEXT PRIMARY KEY REFERENCES leases(id), document TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS human_requests (
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), status TEXT NOT NULL,
 document TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS human_holds (
 task_id TEXT PRIMARY KEY REFERENCES tasks(id), request_id TEXT NOT NULL REFERENCES human_requests(id));
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, at REAL NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS role_changes (
 id TEXT PRIMARY KEY, role_id TEXT NOT NULL, generation INTEGER NOT NULL, at REAL NOT NULL, reason TEXT NOT NULL,
 FOREIGN KEY(role_id,generation) REFERENCES role_versions(role_id,generation));
