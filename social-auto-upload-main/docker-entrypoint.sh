#!/bin/sh
# FlowOps 容器启动入口：初始化数据库 + 启动虚拟显示器与 noVNC + 运行后端
set -e

cd /app/db
python /app/_init_db.py

cd /app

# 虚拟显示器：登录窗口的有头 Chromium 跑在这上面
Xvfb :99 -screen 0 1440x1000x24 -nolisten tcp >/tmp/xvfb.log 2>&1 &
export DISPLAY=:99
sleep 1

# noVNC：把 Xvfb 桌面通过网页 VNC 暴露（密码走 RFB 协议层，不依赖 nginx Basic Auth）
x11vnc -display :99 -forever -shared -passwd "$YUNCE_VNC_PASSWORD" -localhost -rfbport 5900 -xkb >/tmp/x11vnc.log 2>&1 &
websockify --web=/usr/share/novnc 6080 localhost:5900 >/tmp/websockify.log 2>&1 &

exec python sau_backend.py

