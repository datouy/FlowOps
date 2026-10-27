#!/usr/bin/env bash
# 云策 YunCe 裸机安装脚本（Ubuntu 22.04 / 24.04，root 执行）
#
#   sudo bash deploy/install-baremetal.sh
#
# 做的事：系统依赖 -> 建用户 -> 代码放 /opt/yunce/app -> venv + 依赖
#         -> Playwright Chromium -> 配 .env -> 构建前端 -> systemd 服务 -> 健康检查
# 不做的事：申请域名证书（后面单独 certbot），也不覆盖已存在的 .env / 数据目录。

set -euo pipefail

APP_ROOT=/opt/yunce
APP_DIR="$APP_ROOT/app"
VENV="$APP_ROOT/venv"
SVC_USER=yunce
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/social-auto-upload-main"

log() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31m[ERROR] %s\033[0m\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "请用 root 运行：sudo bash $0"
[[ -d "$SRC_DIR" ]] || die "找不到源码目录 $SRC_DIR（脚本要放在项目 deploy/ 下）"

log "1/9 安装系统依赖"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  python3 python3-venv python3-pip ca-certificates tzdata rsync curl openssl \
  nginx fonts-noto-cjk libnss3 libnspr4 libdbus-1-3 libatk1.0-0 \
  libatk-bridge2.0-0 libatspi2.0-0 libxcomposite1 libxdamage1 libxfixes3 \
  libxrandr2 libgbm1 libxkbcommon0 libasound2 libcups2 libpango-1.0-0 \
  libcairo2 libx11-6 libxcb1 libxext6 libglib2.0-0

log "2/9 创建运行用户 $SVC_USER"
id -u "$SVC_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin "$SVC_USER"

log "3/9 同步代码到 $APP_DIR"
mkdir -p "$APP_DIR"
rsync -a \
  --exclude '.git' --exclude 'venv' --exclude '.venv' --exclude '__pycache__' \
  --exclude 'node_modules' --exclude '.pytest_cache' --exclude '.ruff_cache' \
  --exclude 'logs' --exclude '*.log' \
  "$SRC_DIR"/ "$APP_DIR"/

# 数据目录：已存在就保留
install -d -o "$SVC_USER" -g "$SVC_USER" \
  "$APP_DIR/db" "$APP_DIR/cookies" "$APP_DIR/cookiesFile" \
  "$APP_DIR/videoFile" "$APP_DIR/uploadFile" "$APP_DIR/logs" "$APP_ROOT/.playwright"

log "4/9 创建虚拟环境并安装依赖"
[[ -x "$VENV/bin/python" ]] || python3 -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip
"$VENV/bin/pip" install -r "$APP_DIR/requirements.txt"

log "5/9 安装 Playwright Chromium（扫码登录、发布、私信同步都要用）"
PLAYWRIGHT_BROWSERS_PATH="$APP_ROOT/.playwright" "$VENV/bin/playwright" install --with-deps chromium

log "6/9 写入配置"
[[ -f "$APP_DIR/conf.py" ]] || cp "$APP_DIR/conf.example.py" "$APP_DIR/conf.py"

if [[ ! -f "$APP_DIR/.env" ]]; then
  TOKEN="$(openssl rand -hex 24)"
  cat > "$APP_DIR/.env" <<EOF
YUNCE_HOST=0.0.0.0
YUNCE_PORT=5409
YUNCE_ALLOW_REMOTE=1
YUNCE_ACCESS_TOKEN=$TOKEN
YUNCE_CORS_ORIGINS=
YUNCE_LOG_LEVEL=INFO

WB_API_KEY=
WB_BASE_URL=
WB_MODEL=
WB_REALM=cn

OPS_WEBHOOK_URL=
OPS_WEBHOOK_TYPE=auto
EOF
  echo "   访问令牌（浏览器 localStorage 的 yunce_access_token 用它）：$TOKEN"
else
  echo "   $APP_DIR/.env 已存在，保持不变"
fi

chown -R "$SVC_USER:$SVC_USER" "$APP_DIR" "$APP_ROOT/.playwright"
chmod 600 "$APP_DIR/.env"

log "7/9 构建前端并摆到 Flask 的静态路由位置"
if command -v npm >/dev/null 2>&1; then
  API_BASE="${VITE_API_BASE_URL:-/}"
  (
    cd "$APP_DIR/sau_frontend"
    npm config set registry "${NPM_REGISTRY:-https://registry.npmmirror.com}"
    npm ci || npm install
    printf 'VITE_API_BASE_URL=%s\nVITE_USE_MOCK=false\n' "$API_BASE" > .env.production
    npm run build
    cp -f dist/index.html "$APP_DIR/index.html"
    rm -rf "$APP_DIR/assets"
    cp -rf dist/assets "$APP_DIR/assets"
    cp -f dist/vite.svg "$APP_DIR/assets/vite.svg"
  )
  chown -R "$SVC_USER:$SVC_USER" "$APP_DIR/index.html" "$APP_DIR/assets"
  echo "   前端已就位：$APP_DIR/index.html + $APP_DIR/assets"
else
  echo "   [跳过] 服务器没有 npm。请在本机 npm run build 后把 dist/index.html、"
  echo "          dist/assets/、dist/vite.svg 传到 $APP_DIR/ 下"
fi

log "8/9 初始化业务库并安装 systemd 服务"
# database.db 的 user_info / file_records 只有 createTable.py 会建，其余库导入时自建
( cd "$APP_DIR/db" && "$VENV/bin/python" createTable.py )
chown -R "$SVC_USER:$SVC_USER" "$APP_DIR/db"

install -m 644 "$(dirname "${BASH_SOURCE[0]}")/yunce.service" /etc/systemd/system/yunce.service
systemctl daemon-reload
systemctl enable yunce
systemctl restart yunce

log "9/9 等待健康检查"
for i in $(seq 1 40); do
  if curl -fsS --max-time 2 http://127.0.0.1:5409/health >/dev/null 2>&1; then
    echo "   后端已就绪：http://127.0.0.1:5409/health"
    break
  fi
  sleep 1
  [[ $i -eq 40 ]] && die "后端 40 秒内没起来，看日志：journalctl -u yunce -n 100 --no-pager"
done

cat <<'NEXT'

下一步：
  1) Nginx：cp deploy/nginx-yunce.conf /etc/nginx/conf.d/yunce.conf
            改域名 -> nginx -t -> systemctl reload nginx
  2) 证书：certbot --nginx -d your.domain
  3) 浏览器打开 https://your.domain/，控制台写入令牌：
            localStorage.setItem('yunce_access_token','<上面那串>'); location.reload()
  4) 备份：crontab -e 加一行
            20 3 * * * /root/flowOps/deploy/backup.sh >> /var/log/yunce-backup.log 2>&1
            （裸机路径与 Docker 不同，按脚本内 DATA_DIR 说明改）

NEXT
