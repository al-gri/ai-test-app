CREATE TABLE IF NOT EXISTS token_limits (
 role TEXT PRIMARY KEY REFERENCES roles(id), amount INTEGER NOT NULL CHECK(amount>=0));
CREATE TABLE IF NOT EXISTS token_calls (
 id TEXT PRIMARY KEY, role TEXT NOT NULL REFERENCES roles(id), lease TEXT NOT NULL,
 day TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('reserved','settled')),
 reserved INTEGER NOT NULL CHECK(reserved>=0), actual INTEGER NOT NULL CHECK(actual>=0),
 FOREIGN KEY(lease,role) REFERENCES leases(id,role));
CREATE TABLE IF NOT EXISTS token_requests (
 call_id TEXT PRIMARY KEY REFERENCES token_calls(id), request TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS token_usage (
 call_id TEXT PRIMARY KEY REFERENCES token_calls(id), input_tokens INTEGER NOT NULL CHECK(input_tokens>=0),
 output_tokens INTEGER NOT NULL CHECK(output_tokens>=0), receipt_digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS token_alerts (
 id TEXT PRIMARY KEY, role TEXT NOT NULL REFERENCES roles(id), task TEXT REFERENCES tasks(id),
 reason TEXT NOT NULL, resolved INTEGER DEFAULT 0 CHECK(resolved IN (0,1)), resolution TEXT);
CREATE INDEX IF NOT EXISTS token_scope ON token_calls(role,day,status);
CREATE INDEX IF NOT EXISTS token_lease ON token_calls(lease,status);
