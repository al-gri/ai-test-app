-- Archived provenance lives WITH learning, not as mutable authority. Cross-file
-- foreign keys to operational tasks would be invalid after a learning restore.
CREATE TABLE IF NOT EXISTS memory_subjects (
 project TEXT NOT NULL, task_id TEXT NOT NULL, role_id TEXT NOT NULL,
 PRIMARY KEY(project,task_id,role_id));
CREATE TABLE IF NOT EXISTS post_mortems (
 id TEXT PRIMARY KEY, project TEXT NOT NULL, task_id TEXT NOT NULL, role_id TEXT NOT NULL,
 artifact_digest TEXT NOT NULL, prompt_digest TEXT NOT NULL, document TEXT NOT NULL,
 FOREIGN KEY(project,task_id,role_id) REFERENCES memory_subjects(project,task_id,role_id));
CREATE INDEX IF NOT EXISTS post_mortem_source ON post_mortems(project,role_id,task_id);
CREATE TABLE IF NOT EXISTS memory_vectors (
 id TEXT PRIMARY KEY REFERENCES post_mortems(id), vector TEXT NOT NULL,
 embedding TEXT NOT NULL, dimensions INTEGER NOT NULL CHECK(dimensions>=8));
CREATE VIEW IF NOT EXISTS lessons AS
 SELECT p.id,p.document,v.vector,v.embedding,v.dimensions FROM post_mortems p JOIN memory_vectors v ON v.id=p.id;
