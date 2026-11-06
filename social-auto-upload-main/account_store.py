# -*- coding: utf-8 -*-
"""账号服务（Account Store）——把"登录"从"扫码写一行"升级成"账号身份 + 凭据 + 健康度"。

为什么要有这一层
----------------
历史实现里，账号只是一行 ``user_info(type, filePath, userName, status)``，
在 4 个文件、12 处 SQL 里被反复 INSERT/UPDATE。由此带来三个典型问题：

1. **没有身份**：同一个视频号重新扫码会新增一行，同一行也可能被换成另一个
   账号的 Cookie（手工填名字时最容易发生）。这类"串号"在发布侧表现为
   "明明选的是 A 号，发出去变成 B 号"。
2. **没有健康度**：``status`` 只有 1/0/-1，不记录"什么时候校验的、为什么失效"，
   用户只能看到一个红点，排障无从下手。
3. **没有审计**：授权会话只活在内存在里，进程一重启就彻底消失。

这一层把上面三件事收敛到唯一入口，采用"云账号 → N 个媒体号"的账号矩阵模型：

    operator（使用者）
      └─ account（媒体号：平台 + 平台侧唯一身份 + 凭据）
           ├─ credentials  —— cookiesFile/<name>.json（storage_state）
           ├─ health       —— status / last_check_at / fail_reason
           └─ auth_sessions—— 授权过程审计（谁在什么时间、从哪台机器授权的）

约定
----
* 身份（``platform_user_id``）只采信**平台侧强 ID**（视频号 finder_username /
  wxuin、抖音 sec_user_id、小红书 x-user-id-creator…）。拿不到就留空，
  由调用方按 ``account_id`` 覆盖或新建——**宁可漏合并，不可误合并**：
  误合并会把 A 的 Cookie 写进 B 的槽位，那是真正的线上事故。
* 迁移是幂等的：老库只有 5 列，``ensure_schema()`` 会补齐缺失列并回填身份，
  不改动任何已有 Cookie 文件。
* 本模块不依赖 Flask，也不依赖 login/interactive_login，避免循环导入。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from loguru import logger

from db_utils import connect as db_connect
from db_utils import record_schema_version
from utils.account_environment import public_environment

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

SCHEMA_SCOPE = "user_info"
SCHEMA_VERSION = 2

#: 连续多少次校验失败才把账号判为异常。视频号校验要开浏览器访问平台后台，
#: 平台限流/网络抖动都会让单次校验失败；一次就拉红会让状态来回跳。
DEFAULT_FAIL_THRESHOLD = 2


STATUS_VERIFYING = -1
STATUS_EXPIRED = 0
STATUS_ONLINE = 1

STATUS_TEXT = {STATUS_ONLINE: "正常", STATUS_EXPIRED: "异常", STATUS_VERIFYING: "验证中"}

#: 平台元数据。``id_storage`` / ``id_cookies`` 只放"账号级稳定标识"，
#: 不放 sessionid 之类的会话易变值（否则每次登录都判成新账号）。
PLATFORMS: dict[int, dict[str, Any]] = {
    1: {
        "key": "xiaohongshu",
        "label": "小红书",
        "id_storage": ("user_id", "USER_ID", "userId"),
        "id_cookies": ("x-user-id-creator.xiaohongshu.com",),
    },
    2: {
        "key": "tencent",
        "label": "视频号",
        "id_storage": ("finder_username",),
        "id_cookies": ("wxuin",),
    },
    3: {
        "key": "douyin",
        "label": "抖音",
        "id_storage": ("user_id", "sec_user_id", "USER_ID"),
        "id_cookies": ("sec_user_id", "uid_tt"),
    },
    4: {
        "key": "kuaishou",
        "label": "快手",
        "id_storage": ("userId", "user_id", "userInfo"),
        "id_cookies": ("userId",),
    },
    5: {
        "key": "weibo",
        "label": "微博",
        "id_storage": ("uid", "config"),
        "id_cookies": ("SUB",),
    },
    6: {
        "key": "baijiahao",
        "label": "百家号",
        "id_storage": ("userId", "user_id"),
        "id_cookies": ("BDUSS",),
    },
}

PLATFORM_BY_KEY = {meta["key"]: pid for pid, meta in PLATFORMS.items()}

ACCOUNT_COLUMNS: dict[str, str] = {
    "platform_user_id": "TEXT NOT NULL DEFAULT ''",
    "identity_source": "TEXT NOT NULL DEFAULT ''",
    "nickname": "TEXT NOT NULL DEFAULT ''",
    "avatar": "TEXT NOT NULL DEFAULT ''",
    "last_check_at": "TEXT NOT NULL DEFAULT ''",
    "fail_reason": "TEXT NOT NULL DEFAULT ''",
    "cookie_size": "INTEGER NOT NULL DEFAULT 0",
    "fail_streak": "INTEGER NOT NULL DEFAULT 0",
    "created_at": "TEXT NOT NULL DEFAULT ''",
    "updated_at": "TEXT NOT NULL DEFAULT ''",
}

AUTH_SESSION_DDL = """
CREATE TABLE IF NOT EXISTS auth_sessions (
    id TEXT PRIMARY KEY,
    platform_type INTEGER NOT NULL,
    platform_key TEXT NOT NULL DEFAULT '',
    account_id INTEGER,
    account_name TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL DEFAULT 'create',
    state TEXT NOT NULL DEFAULT 'running',
    message TEXT NOT NULL DEFAULT '',
    cookie_name TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '',
    finished_at TEXT NOT NULL DEFAULT ''
)
"""

AUTH_SESSION_FIELDS = (
    "id", "platform_type", "platform_key", "account_id", "account_name",
    "mode", "state", "message", "cookie_name", "actor", "created_at", "finished_at",
)

_COOKIE_FILE_RE = re.compile(r"^[0-9a-f]{32}\.json$")


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def platform_meta(platform_type: int | str | None) -> dict[str, Any]:
    """按 type 编号取平台元数据，未知平台返回一个安全默认值。"""
    try:
        pid = int(platform_type)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        pid = -1
    meta = PLATFORMS.get(pid)
    if meta:
        return {"type": pid, **meta}
    return {"type": pid, "key": "", "label": "未知平台", "id_storage": (), "id_cookies": ()}


def platform_type_of(platform: str | int | None) -> int | None:
    """把 ``tencent`` / ``视频号`` / ``2`` 统一成 type 编号。"""
    if platform is None:
        return None
    text = str(platform).strip()
    if not text:
        return None
    if text.isdigit():
        pid = int(text)
        return pid if pid in PLATFORMS else None
    lowered = text.lower()
    if lowered in PLATFORM_BY_KEY:
        return PLATFORM_BY_KEY[lowered]
    for pid, meta in PLATFORMS.items():
        if meta["label"] == text:
            return pid
    return None


# ---------------------------------------------------------------------------
# 凭据读取与身份提取
# ---------------------------------------------------------------------------


def read_storage_state(path: str | Path) -> dict[str, Any] | None:
    """读取 Playwright storage_state；兼容"裸 cookie 列表"的老文件。"""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if isinstance(payload, list):
        return {"cookies": payload, "origins": []}
    if isinstance(payload, dict):
        cookies = payload.get("cookies")
        origins = payload.get("origins")
        return {
            "cookies": cookies if isinstance(cookies, list) else [],
            "origins": origins if isinstance(origins, list) else [],
        }
    return None


def _iter_storage_items(state: dict[str, Any]) -> Iterable[tuple[str, str]]:
    for origin in state.get("origins") or []:
        if not isinstance(origin, dict):
            continue
        for item in origin.get("localStorage") or []:
            if isinstance(item, dict):
                yield str(item.get("name") or ""), str(item.get("value") or "")


def _iter_cookies(state: dict[str, Any]) -> Iterable[tuple[str, str]]:
    for cookie in state.get("cookies") or []:
        if isinstance(cookie, dict):
            yield str(cookie.get("name") or ""), str(cookie.get("value") or "")


def extract_identity(
    storage_state: dict[str, Any] | None, platform_type: int | str | None
) -> tuple[str, str]:
    """返回 ``(identity, source)``；拿不到强身份时返回 ``("", "")``。

    优先用 localStorage 里的账号 id（视频号 ``finder_username``、抖音 ``user_id``），
    其次用账号级稳定 Cookie（视频号 ``wxuin``、百家号 ``BDUSS``…）。
    两者都没有时返回空——不做"弱身份"兜底，避免把不同账号误判成同一个。
    """
    if not storage_state:
        return "", ""
    meta = platform_meta(platform_type)

    storage_values = {name: value for name, value in _iter_storage_items(storage_state) if value}
    for key in meta["id_storage"]:
        value = storage_values.get(key)
        identity = _normalize_identity(value)
        if identity:
            return identity, f"localStorage:{key}"

    cookie_values = {name: value for name, value in _iter_cookies(storage_state) if value}
    for key in meta["id_cookies"]:
        value = cookie_values.get(key)
        identity = _normalize_identity(value)
        if identity:
            return identity, f"cookie:{key}"
    return "", ""


def _normalize_identity(value: str | None) -> str:
    text = str(value or "").strip()
    if not text or text.lower() in {"undefined", "null", "0"}:
        return ""
    return text[:128]


def file_identity(path: str | Path, platform_type: int | str | None) -> tuple[str, str]:
    """按 Cookie 文件推导身份，失败只返回空（不抛异常）。"""
    state = read_storage_state(path)
    if not state:
        return "", ""
    return extract_identity(state, platform_type)


def mask_identity(identity: str) -> str:
    """脱敏：前 6 位 + 指纹长度，用于前端展示与排障。"""
    text = str(identity or "")
    if not text:
        return ""
    if len(text) <= 8:
        return text[:3] + "…"
    return f"{text[:6]}…{len(text)}"


def _atomic_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


# ---------------------------------------------------------------------------
# AccountStore
# ---------------------------------------------------------------------------


class AccountStore:
    """账号的唯一读写入口：身份去重、凭据落盘、健康度、会话审计。"""

    def __init__(self, base_dir: str | Path):
        self.base_dir = Path(base_dir)
        self.db_path = self.base_dir / "db" / "database.db"
        self.cookies_dir = self.base_dir / "cookiesFile"
        self._schema_ready = False

    # -- schema ------------------------------------------------------------
    def ensure_schema(self, force: bool = False) -> bool:
        """幂等补齐列 / 索引 / 审计表；老库首次调用时会回填身份。"""
        if self._schema_ready and not force:
            return True
        try:
            with db_connect(self.db_path) as conn:
                existing = {
                    str(row[0])
                    for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
                if "user_info" not in existing:
                    # 全新库：一次建到位，字段与历史 5 列保持兼容（前缀顺序不变）。
                    conn.execute(
                        """CREATE TABLE IF NOT EXISTS user_info (
                               id INTEGER PRIMARY KEY AUTOINCREMENT,
                               type INTEGER NOT NULL,
                               filePath TEXT NOT NULL DEFAULT '',
                               userName TEXT NOT NULL DEFAULT '',
                               status INTEGER DEFAULT 0,
                               platform_user_id TEXT NOT NULL DEFAULT '',
                               identity_source TEXT NOT NULL DEFAULT '',
                               nickname TEXT NOT NULL DEFAULT '',
                               avatar TEXT NOT NULL DEFAULT '',
                               last_check_at TEXT NOT NULL DEFAULT '',
                               fail_reason TEXT NOT NULL DEFAULT '',
                               cookie_size INTEGER NOT NULL DEFAULT 0,
                               fail_streak INTEGER NOT NULL DEFAULT 0,
                               created_at TEXT NOT NULL DEFAULT '',
                               updated_at TEXT NOT NULL DEFAULT ''
                           )"""
                    )
                else:
                    columns = {
                        str(row[1])
                        for row in conn.execute("PRAGMA table_info(user_info)")
                    }
                    for name, ddl in ACCOUNT_COLUMNS.items():
                        if name not in columns:
                            conn.execute(f"ALTER TABLE user_info ADD COLUMN {name} {ddl}")
                conn.execute(AUTH_SESSION_DDL)
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ux_user_info_identity "
                    "ON user_info(type, platform_user_id) "
                    "WHERE platform_user_id IS NOT NULL AND platform_user_id != ''"
                )
                conn.commit()
                record_schema_version(conn, SCHEMA_SCOPE, SCHEMA_VERSION)
                conn.commit()
            self._schema_ready = True
        except Exception as exc:  # 数据库被锁/只读时不阻塞登录主流程
            logger.warning("账号表结构初始化失败: {}", exc)
            return False
        self.backfill_identities()
        return True

    # -- 读取 --------------------------------------------------------------
    def _row_dict(self, row) -> dict[str, Any]:
        keys = row.keys()
        data = {key: row[key] for key in keys}
        pid = data.get("type")
        meta = platform_meta(pid)
        cookie_name = str(data.get("filePath") or "")
        cookie_path = self.cookies_dir / cookie_name if cookie_name else None
        size = 0
        mtime = ""
        session_group = ""
        if cookie_path and cookie_path.is_file():
            try:
                stat = cookie_path.stat()
                size = int(stat.st_size)
                mtime = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            except OSError:
                size = 0
            # 会话指纹：同一次扫码保存出来的多个频道共享同一个 sessionid。
            # 前端用它判断"这两个号是一起登录的"——它们会一起失效，也一起修好。
            state = read_storage_state(cookie_path)
            if state:
                for cookie in state.get("cookies") or []:
                    if (
                        isinstance(cookie, dict)
                        and cookie.get("name") == "sessionid"
                        and cookie.get("value")
                    ):
                        session_group = hashlib.sha256(
                            str(cookie["value"]).encode()
                        ).hexdigest()[:8]
                        break
        status = int(data.get("status") if data.get("status") is not None else STATUS_EXPIRED)
        identity = str(data.get("platform_user_id") or "")
        return {
            "id": int(data.get("id") or 0),
            "platform": {"type": int(pid or 0), "key": meta["key"], "label": meta["label"]},
            "name": str(data.get("userName") or ""),
            "nickname": str(data.get("nickname") or ""),
            "avatar": str(data.get("avatar") or ""),
            "status": status,
            "statusText": STATUS_TEXT.get(status, "未知"),
            "failReason": str(data.get("fail_reason") or ""),
            "lastCheckAt": str(data.get("last_check_at") or ""),
            "failStreak": int(data.get("fail_streak") or 0),
            "identity": {
                "value": identity,
                "masked": mask_identity(identity),
                "source": str(data.get("identity_source") or ""),
                "bound": bool(identity),
            },
            "cookie": {
                "name": cookie_name,
                "size": size,
                "exists": bool(cookie_path and cookie_path.is_file()),
                "updatedAt": mtime,
                "sessionGroup": session_group,
            },
            "environment": (
                public_environment(cookie_path, int(data.get("id") or 0) or None)
                if cookie_path else {
                    "persistent": True,
                    "profileExists": False,
                    "proxyEnabled": False,
                    "proxyServer": "",
                    "proxyUsername": "",
                    "proxyConfigured": False,
                }
            ),
            "createdAt": str(data.get("created_at") or ""),
            "updatedAt": str(data.get("updated_at") or ""),
        }

    def list(self, platform_type: int | str | None = None) -> list[dict[str, Any]]:
        self.ensure_schema()
        sql = "SELECT * FROM user_info"
        params: tuple[Any, ...] = ()
        pid = platform_type_of(platform_type)
        if pid is not None:
            sql += " WHERE type=?"
            params = (pid,)
        sql += " ORDER BY id"
        try:
            with db_connect(self.db_path) as conn:
                rows = conn.execute(sql, params).fetchall()
        except Exception as exc:
            logger.warning("读取账号列表失败: {}", exc)
            return []
        return [self._row_dict(row) for row in rows]

    def health_snapshot(self) -> list[dict[str, Any]]:
        """返回现存账号的当前健康快照，供巡检、Todo 和状态接口共用。

        历史巡检表只记录检查过程，不能作为账号是否存在、当前是否正常的
        事实来源。这里始终以 ``user_info`` 为准，已删除账号不会复活；
        ``account_patrol.alive`` 也不能覆盖后续手动复检写回的新状态。
        """
        self.ensure_schema()
        with db_connect(self.db_path) as conn:
            rows = conn.execute(
                """SELECT id, userName, type, status, last_check_at
                   FROM user_info ORDER BY id"""
            ).fetchall()
        return [
            {
                "account_id": int(row["id"]),
                "account_name": str(row["userName"] or ""),
                "type": int(row["type"] or 0),
                "alive": int(row["status"] if row["status"] is not None else STATUS_EXPIRED)
                == STATUS_ONLINE,
                "checked_at": str(row["last_check_at"] or ""),
            }
            for row in rows
        ]

    def get(self, account_id: int) -> dict[str, Any] | None:
        self.ensure_schema()
        try:
            with db_connect(self.db_path) as conn:
                row = conn.execute("SELECT * FROM user_info WHERE id=?", (account_id,)).fetchone()
        except Exception as exc:
            logger.warning("读取账号 {} 失败: {}", account_id, exc)
            return None
        return self._row_dict(row) if row else None

    def legacy_rows(self, platform_type: int | str | None = None) -> list[list[Any]]:
        """给老接口用的 5 列结构（id, type, filePath, userName, status）。"""
        rows = [self._row_dict(row) for row in self._raw_rows(platform_type)]
        return [
            [row["id"], row["platform"]["type"], row["cookie"]["name"], row["name"], row["status"]]
            for row in rows
        ]

    def _raw_rows(self, platform_type: int | str | None = None):
        sql = "SELECT * FROM user_info"
        params: tuple[Any, ...] = ()
        pid = platform_type_of(platform_type)
        if pid is not None:
            sql += " WHERE type=?"
            params = (pid,)
        sql += " ORDER BY id"
        with db_connect(self.db_path) as conn:
            return conn.execute(sql, params).fetchall()

    # -- 写入 --------------------------------------------------------------
    def upsert(
        self,
        platform_type: int | str,
        *,
        cookie_name: str,
        name: str = "",
        account_id: int | None = None,
        identity: str = "",
        identity_source: str = "",
        nickname: str = "",
        avatar: str = "",
        status: int = STATUS_ONLINE,
    ) -> dict[str, Any]:
        """保存一次登录结果，返回 ``{"action", "id", "merged"}``。

        命中顺序：身份（同平台同账号）→ 指定槽位 ``account_id`` → 新建。
        身份命中优先于槽位，是为了防止"用户手滑选错覆盖目标"把 A 的凭据
        写进 B 的记录里——这是历史版本最常见的串号来源。
        """
        self.ensure_schema()
        pid = platform_type_of(platform_type)
        if pid is None:
            raise ValueError(f"未知平台: {platform_type!r}")
        identity = _normalize_identity(identity)
        now = now_text()
        cookie_size = 0
        cookie_path = self.cookies_dir / cookie_name if cookie_name else None
        if cookie_path and cookie_path.is_file():
            try:
                cookie_size = int(cookie_path.stat().st_size)
            except OSError:
                cookie_size = 0

        with db_connect(self.db_path) as conn:
            target = None
            action = "created"
            if identity:
                target = conn.execute(
                    "SELECT id, filePath, userName FROM user_info WHERE type=? AND platform_user_id=?",
                    (pid, identity),
                ).fetchone()
                if target is not None:
                    action = "merged"
            if target is None and account_id:
                target = conn.execute(
                    "SELECT id, filePath, userName FROM user_info WHERE id=?",
                    (account_id,),
                ).fetchone()
                if target is not None:
                    action = "updated"

            if target is not None:
                old_cookie = str(target["filePath"] or "")
                # 身份命中（同一频道再次登录/补登）时保留原名称：
                # 否则"补登频道"这类会话会用临时名字（seedtest）把用户起的名字改掉。
                keep_name = (
                    str(target["userName"] or "").strip()
                    if action == "merged"
                    else ((name or str(target["userName"] or "")).strip()[:64])
                )
                keep_name = keep_name[:64]
                conn.execute(
                    """UPDATE user_info
                          SET type=?, filePath=?, userName=?, status=?,
                              platform_user_id=?, identity_source=?,
                              nickname=?, avatar=?, cookie_size=?,
                              last_check_at=?, fail_reason='', updated_at=?,
                              created_at=CASE WHEN COALESCE(created_at,'')='' THEN ? ELSE created_at END
                        WHERE id=?""",
                    (
                        pid, cookie_name, keep_name, int(status),
                        identity, identity_source or ("manual" if identity else ""),
                        nickname, avatar, cookie_size,
                        now, now, now, int(target["id"]),
                    ),
                )
                conn.commit()
                new_id = int(target["id"])
                if old_cookie and old_cookie != cookie_name:
                    # 覆盖登录后旧文件不再被引用，立刻删掉，避免 per-account 目录膨胀
                    self._remove_cookie(old_cookie)
            else:
                cursor = conn.execute(
                    """INSERT INTO user_info
                       (type, filePath, userName, status, platform_user_id, identity_source,
                        nickname, avatar, last_check_at, fail_reason, cookie_size,
                        created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,'',?,?,?)""",
                    (
                        pid, cookie_name, (name or "").strip()[:64], int(status),
                        identity, identity_source or ("manual" if identity else ""),
                        nickname, avatar, now, cookie_size, now, now,
                    ),
                )
                conn.commit()
                new_id = int(cursor.lastrowid)
        logger.info("账号保存完成 action={} id={} platform={}", action, new_id, pid)
        return {"action": action, "id": new_id, "identity": identity}

    def mark_status(self, account_id: int, status: int, reason: str = "") -> bool:
        self.ensure_schema()
        try:
            with db_connect(self.db_path) as conn:
                conn.execute(
                    "UPDATE user_info SET status=?, fail_reason=?, updated_at=? WHERE id=?",
                    (int(status), (reason or "")[:240], now_text(), int(account_id)),
                )
                conn.commit()
            return True
        except Exception as exc:
            logger.warning("更新账号 {} 状态失败: {}", account_id, exc)
            return False

    def mark_checked(
        self,
        account_id: int,
        alive: bool,
        reason: str = "",
        threshold: int = DEFAULT_FAIL_THRESHOLD,
    ) -> bool:
        """记录一次健康检查结果（同时写 last_check_at）。

        去抖：单次校验失败**不立刻**把账号拉红。视频号校验要开浏览器访问平台
        后台，平台限流、网络抖动、并发自查被踢都会让某一次失败；直接置异常会
        让列表状态来回跳（"一会儿正常一会儿失效"）。
        这里改成"连续 ``threshold`` 次失败才判异常"，期间只在 ``fail_reason``
        里留下第几次未通过的痕迹。``alive=True`` 立刻清零。
        """
        self.ensure_schema()
        threshold = max(1, int(threshold or 1))
        now = now_text()
        try:
            with db_connect(self.db_path) as conn:
                row = conn.execute(
                    "SELECT status, fail_streak FROM user_info WHERE id=?",
                    (int(account_id),),
                ).fetchone()
                if row is None:
                    return False
                if alive:
                    conn.execute(
                        "UPDATE user_info SET status=?, fail_streak=0, fail_reason='',"
                        " last_check_at=?, updated_at=? WHERE id=?",
                        (STATUS_ONLINE, now, now, int(account_id)),
                    )
                else:
                    streak = int(row["fail_streak"] or 0) + 1
                    if streak >= threshold:
                        final_reason = reason or "登录态失效，请重新授权"
                        conn.execute(
                            "UPDATE user_info SET status=?, fail_streak=?, fail_reason=?,"
                            " last_check_at=?, updated_at=? WHERE id=?",
                            (STATUS_EXPIRED, streak, final_reason[:240], now, now, int(account_id)),
                        )
                    else:
                        pending = f"校验未通过（{streak}/{threshold}），下次仍失败将标记异常"
                        conn.execute(
                            "UPDATE user_info SET fail_streak=?, fail_reason=?,"
                            " last_check_at=?, updated_at=? WHERE id=?",
                            (streak, pending[:240], now, now, int(account_id)),
                        )
                conn.commit()
            return True
        except Exception as exc:
            logger.warning("回写账号 {} 健康度失败: {}", account_id, exc)
            return False

    def update_cookie_name(self, account_id: int, cookie_name: str) -> bool:
        self.ensure_schema()
        try:
            with db_connect(self.db_path) as conn:
                conn.execute(
                    "UPDATE user_info SET filePath=?, updated_at=? WHERE id=?",
                    (cookie_name, now_text(), int(account_id)),
                )
                conn.commit()
            return True
        except Exception as exc:
            logger.warning("更新账号 {} 凭据路径失败: {}", account_id, exc)
            return False

    # -- 维护 --------------------------------------------------------------
    def backfill_identities(self, force: bool = False) -> dict[str, int]:
        """为历史账号补 ``platform_user_id``；同一身份重复时保留最新一条。

        冲突处理：把较早那条的 ``fail_reason`` 标成 ``duplicate-of:<id>`` 并把
        status 置为异常，**不删数据**——用户可能还指望用它回滚。
        """
        self.ensure_schema()
        filled = 0
        conflicts = 0
        try:
            with db_connect(self.db_path) as conn:
                rows = conn.execute(
                    "SELECT id, type, filePath, platform_user_id FROM user_info ORDER BY id"
                ).fetchall()
                for row in rows:
                    if row["platform_user_id"] and not force:
                        continue
                    identity, source = file_identity(
                        self.cookies_dir / str(row["filePath"] or ""), row["type"]
                    )
                    if not identity:
                        continue
                    holder = conn.execute(
                        "SELECT id FROM user_info WHERE type=? AND platform_user_id=? AND id<>?",
                        (row["type"], identity, row["id"]),
                    ).fetchone()
                    if holder is not None:
                        keep_id = int(holder["id"])
                        conn.execute(
                            "UPDATE user_info SET platform_user_id='', identity_source='',"
                            " status=?, fail_reason=? WHERE id=?",
                            (STATUS_EXPIRED, f"duplicate-of:{keep_id}", int(row["id"])),
                        )
                        conflicts += 1
                        continue
                    conn.execute(
                        "UPDATE user_info SET platform_user_id=?, identity_source=?,"
                        " updated_at=CASE WHEN COALESCE(updated_at,'')='' THEN ? ELSE updated_at END"
                        " WHERE id=?",
                        (identity, source, now_text(), int(row["id"])),
                    )
                    filled += 1
                conn.commit()
        except Exception as exc:
            logger.warning("账号身份回填失败: {}", exc)
            return {"filled": filled, "conflicts": conflicts}
        if filled or conflicts:
            logger.info("账号身份回填：补全 {} 条，重复 {} 条", filled, conflicts)
        return {"filled": filled, "conflicts": conflicts}

    def orphan_cookies(self) -> list[Path]:
        """返回没有被任何账号引用的 Cookie 文件（只看 <32hex>.json）。"""
        self.ensure_schema()
        try:
            with db_connect(self.db_path) as conn:
                used = {
                    str(row[0])
                    for row in conn.execute("SELECT filePath FROM user_info")
                    if row[0]
                }
        except Exception:
            return []
        if not self.cookies_dir.is_dir():
            return []
        return [
            path
            for path in sorted(self.cookies_dir.iterdir())
            if path.is_file() and _COOKIE_FILE_RE.match(path.name) and path.name not in used
        ]

    def prune_orphans(self, dry_run: bool = True) -> dict[str, Any]:
        orphans = self.orphan_cookies()
        removed: list[str] = []
        if not dry_run:
            for path in orphans:
                if self._remove_cookie(path.name):
                    removed.append(path.name)
        return {
            "dryRun": dry_run,
            "count": len(orphans),
            "files": [path.name for path in orphans],
            "removed": removed,
        }

    def _remove_cookie(self, cookie_name: str) -> bool:
        if not cookie_name or not _COOKIE_FILE_RE.match(cookie_name):
            return False
        path = self.cookies_dir / cookie_name
        try:
            path.unlink(missing_ok=True)
        except OSError:
            return False
        return True

    def stats(self) -> dict[str, Any]:
        self.ensure_schema()
        accounts = self.list()
        by_platform: dict[str, int] = {}
        by_status: dict[str, int] = {}
        for item in accounts:
            by_platform[item["platform"]["label"]] = by_platform.get(item["platform"]["label"], 0) + 1
            by_status[item["statusText"]] = by_status.get(item["statusText"], 0) + 1
        orphans = self.orphan_cookies()
        return {
            "total": len(accounts),
            "byPlatform": by_platform,
            "byStatus": by_status,
            "unboundIdentity": sum(1 for a in accounts if not a["identity"]["bound"]),
            "missingCookie": sum(1 for a in accounts if not a["cookie"]["exists"]),
            "orphanCookies": len(orphans),
            "authSessions": len(self.list_auth_sessions(limit=500)),
        }

    # -- 授权会话审计 ------------------------------------------------------
    def record_auth_session(
        self,
        session_id: str,
        platform_type: int | str,
        *,
        account_name: str = "",
        account_id: int | None = None,
        mode: str = "create",
        actor: str = "",
    ) -> None:
        self.ensure_schema()
        meta = platform_meta(platform_type)
        try:
            with db_connect(self.db_path) as conn:
                conn.execute(
                    """INSERT OR REPLACE INTO auth_sessions
                       (id, platform_type, platform_key, account_id, account_name,
                        mode, state, message, cookie_name, actor, created_at, finished_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        session_id, meta["type"], meta["key"], account_id, account_name[:64],
                        mode, "running", "", "", actor[:64], now_text(), "",
                    ),
                )
                conn.commit()
        except Exception as exc:
            logger.warning("写入授权会话审计失败: {}", exc)

    def finish_auth_session(
        self, session_id: str, state: str, message: str = "", cookie_name: str = ""
    ) -> None:
        self.ensure_schema()
        try:
            with db_connect(self.db_path) as conn:
                conn.execute(
                    """UPDATE auth_sessions
                          SET state=?, message=?, cookie_name=?,
                              finished_at=CASE WHEN ?='running' THEN finished_at ELSE ? END
                        WHERE id=?""",
                    (state, message[:240], cookie_name, state, now_text(), session_id),
                )
                conn.commit()
        except Exception as exc:
            logger.warning("更新授权会话审计失败: {}", exc)

    def list_auth_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        self.ensure_schema()
        try:
            with db_connect(self.db_path) as conn:
                rows = conn.execute(
                    "SELECT * FROM auth_sessions ORDER BY created_at DESC LIMIT ?",
                    (max(1, int(limit)),),
                ).fetchall()
        except Exception as exc:
            logger.warning("读取授权会话失败: {}", exc)
            return []
        sessions = []
        for row in rows:
            item = {field: row[field] for field in AUTH_SESSION_FIELDS if field in row.keys()}
            meta = platform_meta(item.get("platform_type"))
            item["platform"] = {"type": meta["type"], "key": meta["key"], "label": meta["label"]}
            sessions.append(item)
        return sessions


# ---------------------------------------------------------------------------
# 单例
# ---------------------------------------------------------------------------

_STORE: AccountStore | None = None
_STORE_BASE: Path | None = None


def get_store(base_dir: str | Path | None = None) -> AccountStore:
    """取账号服务单例；``base_dir`` 变化时（测试 monkeypatch BASE_DIR）自动重建。"""
    global _STORE, _STORE_BASE
    if base_dir is None:
        from conf import BASE_DIR as _BASE  # 延迟导入，避免测试期循环依赖

        base_dir = _BASE
    base = Path(base_dir)
    if _STORE is None or _STORE_BASE != base:
        _STORE = AccountStore(base)
        _STORE_BASE = base
    return _STORE
