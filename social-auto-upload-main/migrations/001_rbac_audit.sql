-- 001_rbac_audit.sql（P0-5）：RBAC 四表，幂等
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL, password_hash TEXT DEFAULT '',
    role TEXT DEFAULT 'operator', scope TEXT DEFAULT '',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS roles (
    name TEXT PRIMARY KEY, permissions TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY, user_id INTEGER,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP, expires_at DATETIME);
CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user TEXT DEFAULT '',
    action TEXT NOT NULL, target TEXT DEFAULT '', detail TEXT DEFAULT '',
    ok INTEGER DEFAULT 1, created_at DATETIME DEFAULT CURRENT_TIMESTAMP);
INSERT OR IGNORE INTO roles (name, permissions) VALUES
 ('admin','read,publish,message,account,team'),
 ('operator','read,publish,message'),
 ('viewer','read');
