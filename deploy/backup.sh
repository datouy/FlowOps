#!/usr/bin/env bash
# 云策 YunCe 数据备份：SQLite 库 + 平台 Cookie + 部署环境变量
#
# Docker 方案（默认，在 deploy/ 目录下跑）：
#   bash backup.sh
# 裸机方案：
#   DATA_DIR=/opt/yunce/app ENV_FILE=/opt/yunce/app/.env bash backup.sh
#
# crontab 示例（每天 03:20）：
#   20 3 * * * cd /root/flowOps/deploy && bash backup.sh >> /var/log/yunce-backup.log 2>&1

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

DATA_DIR="${DATA_DIR:-./data}"
ENV_FILE="${ENV_FILE:-./.env.deploy}"
OUT_DIR="${OUT_DIR:-./backups}"
KEEP_DAYS="${KEEP_DAYS:-14}"

[[ -d "$DATA_DIR" ]] || { echo "[ERROR] 数据目录不存在：$DATA_DIR" >&2; exit 1; }

mkdir -p "$OUT_DIR"
STAMP="$(date +%Y%m%d-%H%M%S)"
TARGET="$OUT_DIR/yunce-$STAMP.tar.gz"

# SQLite 用 WAL 模式，直接拷 .db 可能拿到半截事务；
# 先让容器里的进程落盘（有 docker 就顺便 checkpoint），再打包。
if command -v docker >/dev/null 2>&1 && docker ps --format '{{.Names}}' | grep -qx yunce; then
  docker exec yunce python - <<'PY' || true
import sqlite3, glob, os
for path in glob.glob('/app/db/*.db'):
    try:
        with sqlite3.connect(path) as conn:
            conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    except Exception as exc:
        print('skip', path, exc)
PY
fi

FILES=("$DATA_DIR/db" "$DATA_DIR/cookiesFile" "$DATA_DIR/cookies")
[[ -d "$DATA_DIR/videoFile" ]] && FILES+=("$DATA_DIR/videoFile")
[[ -f "$ENV_FILE" ]] && FILES+=("$ENV_FILE")

tar -czf "$TARGET" "${FILES[@]}"
chmod 600 "$TARGET"

echo "已生成：$TARGET（$(du -h "$TARGET" | cut -f1)）"

find "$OUT_DIR" -name 'yunce-*.tar.gz' -type f -mtime "+$KEEP_DAYS" -print -delete \
  | sed 's/^/已清理过期备份：/'

# 恢复方法（先停服务，再解包，再起服务；覆盖前务必手工留一份）：
#   docker compose stop yunce
#   tar -xzf backups/yunce-YYYYmmdd-HHMMSS.tar.gz -C ./      # 解到 deploy/ 下
#   docker compose start yunce
