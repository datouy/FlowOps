# 云策 YunCe 生产镜像
# 构建上下文必须是 social-auto-upload-main/（docker-compose.yml 已经这样配好）
#   前端 vite build -> Flask 同源托管 + Playwright Chromium
#
# 为什么前端要塞进后端目录：sau_backend.py 自带 '/'、'/assets/<file>'、'/favicon.ico'
# 三个静态路由，把 dist 的内容放到 /app 根上就是一个端口跑起整站，不需要 CORS。

# ---------- 阶段 1：构建前端 ----------
FROM node:20-bookworm-slim AS web

WORKDIR /web

# 国内服务器可换源；海外服务器用 --build-arg NPM_REGISTRY=https://registry.npmjs.org
ARG NPM_REGISTRY=https://registry.npmmirror.com
# 前端打包时写死的后端地址：
#   "/"      -> 跟随当前域名，前后端同源（默认，推荐）
#   "http://api.example.com" -> 前后端分域部署时填绝对地址
ARG VITE_API_BASE_URL=/
# 整站挂在 Nginx 子路径时同时设置，例如 /flowops/；根路径部署保持 "/"。
ARG VITE_PUBLIC_BASE=/

COPY sau_frontend/package.json sau_frontend/package-lock.json ./
RUN npm config set registry "$NPM_REGISTRY" \
 && (npm ci || npm install)

COPY sau_frontend/ ./
# .env.production 会被 vite build 读取；直接覆盖，避免沿用本地开发地址
RUN printf 'VITE_API_BASE_URL=%s\nVITE_USE_MOCK=false\n' "$VITE_API_BASE_URL" > .env.production
RUN npm run build -- --base="$VITE_PUBLIC_BASE" && test -f dist/index.html

# ---------- 阶段 2：运行时 ----------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONIOENCODING=utf-8 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright \
    PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright \
    DOUYIN_FORM_LOGIN_HEADLESS=false \
    TZ=Asia/Shanghai

WORKDIR /app

ARG PIP_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple

# fonts-noto-cjk：扫码页/发布页截图里的中文不能变方块
# 国内云服务器直连 deb.debian.org 常会长时间卡住，改用腾讯云 Debian 镜像。
RUN sed -i 's#deb.debian.org#mirrors.cloud.tencent.com#g' /etc/apt/sources.list.d/debian.sources \
 && apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates tzdata fonts-noto-cjk xvfb xauth x11vnc novnc websockify \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip config set global.index-url "$PIP_INDEX_URL" \
 && pip install --no-cache-dir -r requirements.txt

# 代码里大量用 channel="chromium"，必须有完整 chromium；--with-deps 顺手补齐系统库
RUN playwright install --with-deps chromium

# .dockerignore 已经把 db/、cookiesFile/、videoFile/、logs/、.env 挡在外面
COPY . .

# conf.py 被上游 .gitignore 忽略，镜像里用 example 兜底；已存在则不覆盖
RUN cp -n conf.example.py conf.py || true \
 && mkdir -p db cookies cookiesFile videoFile uploadFile logs

# /app/db 运行时会被数据卷整目录覆盖，所以建表的脚本先复制一份到镜像根上
RUN cp db/createTable.py /app/_init_db.py

# 前端产物按 Flask 的静态路由位置摆放
COPY --from=web /web/dist/index.html /app/index.html
COPY --from=web /web/dist/assets /app/assets
COPY --from=web /web/dist/vite.svg /app/assets/vite.svg

EXPOSE 5409

# /health 不校验令牌，专门给健康检查用
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=5 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:5409/health',timeout=4)" || exit 1

# 单进程：内存队列、发布 worker、巡检线程、SSE 都在进程内，别改成多 worker
# db/database.db 的 user_info / file_records 只有 createTable.py 会建，
# 其余库都是模块导入时自建表；所以启动前必须先跑一次这个幂等脚本。
# 脚本按 CWD 找 ./database.db，因此要 cd 到 /app/db 再执行。
CMD ["sh", "-c", "cd /app/db && python /app/_init_db.py; test $? -eq 0 || exit $?; cd /app; Xvfb :99 -screen 0 1440x1000x24 -nolisten tcp >/tmp/xvfb.log 2>&1 & export DISPLAY=:99; sleep 1; x11vnc -display :99 -forever -shared -nopw -localhost -rfbport 5900 -xkb >/tmp/x11vnc.log 2>&1 & websockify --web=/usr/share/novnc 6080 localhost:5900 >/tmp/websockify.log 2>&1 & exec python sau_backend.py"]
