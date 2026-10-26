from __future__ import annotations

from db_utils import apply_migrations, connect, schema_version


def test_connect_enables_wal_and_foreign_keys(tmp_path):
    conn = connect(tmp_path / "test.db")
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()


def test_migrations_run_once_and_record_version(tmp_path):
    calls = []
    migrations = {1: lambda conn: calls.append(1), 2: lambda conn: calls.append(2)}
    conn = connect(tmp_path / "migrate.db")
    try:
        assert apply_migrations(conn, "demo", migrations) == 2
        assert calls == [1, 2]
        assert apply_migrations(conn, "demo", migrations) == 2
        assert calls == [1, 2]
        assert schema_version(conn, "demo") == 2
    finally:
        conn.close()
