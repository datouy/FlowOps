-- 002_contents_roi.sql（P1-1/P1-4）：内容一等对象 + 变体 + ROI
CREATE TABLE IF NOT EXISTS contents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL, body TEXT DEFAULT '', files TEXT DEFAULT '[]',
    created_by TEXT DEFAULT '', created_at DATETIME DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS content_variants (
    id INTEGER PRIMARY KEY AUTOINCREMENT, content_id INTEGER NOT NULL,
    platform TEXT NOT NULL, title TEXT DEFAULT '', body TEXT DEFAULT '',
    status TEXT DEFAULT 'draft',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(content_id, platform));
CREATE TABLE IF NOT EXISTS campaigns (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
    content_id INTEGER, budget REAL DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS ai_usage_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id INTEGER,
    kind TEXT DEFAULT '', tokens INTEGER DEFAULT 0, cost REAL DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP);
-- 线索归因：dm_contacts 加 deal 列（列存在则忽略，靠应用层 ensure）
