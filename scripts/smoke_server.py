"""真实端口启动冒烟：沙箱化 BASE_DIR 后运行 Flask 服务并请求关键接口。"""
from __future__ import annotations

import os
import pathlib
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1] / "social-auto-upload-main"
sys.path.insert(0, str(ROOT))

PORT = 5433
os.environ.update({
    "WB_API_KEY": "",
    "YUNCE_ACCESS_TOKEN": "",
    "YUNCE_HOST": "127.0.0.1",
    "YUNCE_LOG_LEVEL": "WARNING",
    "FLOWOPS_SQLITE_WAL": "0",
})


def main() -> int:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="flowops_boot_"))
    for name in ("db", "cookiesFile", "logs", "static", "uploadFile"):
        (tmp / name).mkdir(parents=True, exist_ok=True)
    import conf

    conf.BASE_DIR = tmp
    with sqlite3.connect(tmp / "db" / "database.db") as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS user_info (
            id INTEGER PRIMARY KEY AUTOINCREMENT, type INTEGER NOT NULL,
            filePath TEXT NOT NULL, userName TEXT NOT NULL, status INTEGER DEFAULT 0)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS file_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT, filename TEXT NOT NULL, filesize REAL,
            upload_time DATETIME DEFAULT CURRENT_TIMESTAMP, file_path TEXT)""")

    import sau_backend
    from werkzeug.serving import make_server

    server = make_server("127.0.0.1", PORT, sau_backend.app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(1.5)

    results: list[tuple[str, bool, str]] = []

    def call(path: str) -> None:
        url = f"http://127.0.0.1:{PORT}{path}"
        try:
            with urllib.request.urlopen(url, timeout=20) as resp:
                body = resp.read(400)
                results.append((path, resp.status == 200, f"{resp.status} {body[:120]!r}"))
        except urllib.error.HTTPError as exc:
            results.append((path, exc.code < 500, f"HTTP {exc.code}"))
        except Exception as exc:  # noqa: BLE001
            results.append((path, False, f"{exc.__class__.__name__}: {exc}"))

    for path in ("/health", "/dm/sync/status", "/dm/xhs/status", "/dm/channels/status",
                 "/dm/priority-leads", "/todo/summary", "/dm/analytics"):
        call(path)

    # 后台同步线程应已启动（三平台各一条）
    names = [t.name for t in threading.enumerate()]
    sync_threads = [n for n in names if n.endswith("-dm-sync")]
    results.append(("后台同步线程", len(sync_threads) == 3, ",".join(sorted(sync_threads))))

    server.shutdown()

    ok = sum(1 for _, passed, _ in results if passed)
    print(f"\n服务启动冒烟：{ok}/{len(results)} 通过")
    for path, passed, detail in results:
        print(f"{'✅' if passed else '❌'} {path:<26} {detail}")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
