# 云策 YunCe 服务器部署指南

给 `flowOps`（`social-auto-upload-main` 那套东西）的**生产部署**说明。本地双击 `start.bat` 那套是开发模式（Vite 5173 + Flask 5409），服务器上不要用。

---

## 0. 先认清这套系统的形态

| 事实 | 对部署的含义 |
| --- | --- |
| 后端是 Flask 单文件应用 `sau_backend.py`，端口 5409，`threaded=True` | 一个进程扛全部请求 |
| 后端自带静态路由：`/`、`/assets/<file>`、`/favicon.ico`、`/vite.svg` | 前端 `dist` 丢进后端目录 → **一个端口跑整站**，不需要 CORS |
| 前端 Vue3 + Vite，路由是 **hash 模式** | 不需要 Nginx 做 SPA history fallback |
| 前端 `src/config.js`：`VITE_API_BASE_URL` 为空/相对路径时**跟随当前域名** | 生产构建必须设 `VITE_API_BASE_URL=/`，否则打包进 `localhost:5409` |
| 浏览器自动化用 Playwright（`channel="chromium"`） | 服务器必须 `playwright install chromium`，扫码登录/发布/私信同步全靠它 |
| 状态全在文件里：`db/` 4 个 SQLite、`cookiesFile/`、`videoFile/`、`uploadFile/`、`logs/` | 这些目录**必须挂卷**，否则 `docker compose down` 就全丢 |
| 进程内有内存队列 + 2 个发布 worker + 12h 巡检 + 24h 数据回流 + 3 条私信同步线程 + SSE | **只能单进程单副本**。多 worker/多副本会重复发布、重复回消息、SSE 断流 |
| 安全闸门：监听非回环地址时，`YUNCE_ALLOW_REMOTE=1` 和 `YUNCE_ACCESS_TOKEN` 缺一个就直接拒绝启动 | 远程部署必须配令牌，这是设计如此不是 bug |
| 上传上限 `MAX_CONTENT_LENGTH = 160MB` | Nginx `client_max_body_size` 要 ≥ 160m |

**部署拓扑（推荐）**

```
浏览器 ──HTTPS──> Nginx(443) ──HTTP──> 127.0.0.1:5409 ──> YunCe 容器/进程
                                                        ├── db/  cookiesFile/  videoFile/  (卷)
                                                        └── Chromium（扫码登录、发布、私信同步）
```

---

## 1. 最短路径（Docker，约 10 分钟）

你需要的只有一台 Linux 服务器（Ubuntu 22.04/24.04 最省事）、一个域名（可选）。

```bash
# ── 服务器上，先把代码传上来（在你自己电脑执行）
rsync -avz \
  --exclude 'venv' --exclude 'node_modules' \
  --exclude 'social-auto-upload-main/db' \
  --exclude 'social-auto-upload-main/cookiesFile' \
  --exclude 'social-auto-upload-main/videoFile' \
  --exclude 'social-auto-upload-main/logs' \
  ./deploy ./social-auto-upload-main root@[SERVER_IP]:/root/flowOps/

# ── 服务器上
ssh root@[SERVER_IP]
curl -fsSL https://get.docker.com | sh          # 已装可跳过
cd /root/flowOps/deploy
cp .env.deploy.example .env.deploy
sed -i "s/CHANGE_ME_openssl_rand_hex_24/$(openssl rand -hex 24)/" .env.deploy
grep YUNCE_ACCESS_TOKEN .env.deploy             # 记下这串，后面浏览器要用
docker compose up -d --build                    # 首次 5~10 分钟（下 chromium）

# ── 验证
docker compose ps                               # State 应为 healthy
bash smoke.sh                                   # 全绿即通过
```

然后走 **第 6 节 Nginx + HTTPS**，再 **第 7 节首次登录**。

---

## 2. 服务器要求

| 项 | 建议 |
| --- | --- |
| 系统 | Ubuntu 22.04 / 24.04 LTS（Debian 12 也行） |
| 配置 | 2 核 4G 起步；同时跑多个平台发布 → 4 核 8G |
| 磁盘 | 40G+，视频素材很吃空间（`videoFile/` 会一直长） |
| 网络 | 必须能访问抖音/小红书/视频号/快手/微博/百家号；出海发布还要能到 tiktok/youtube |
| 端口 | 对外只开 22 / 80 / 443。**5409 不要暴露公网** |
| 时区 | 容器/进程都设 `TZ=Asia/Shanghai`，定时发布才不会偏 8 小时 |

安全组/防火墙示例（云厂商安全组 + ufw）：

```bash
ufw allow 22,80,443/tcp
ufw enable
```

---

## 3. 路线 A：Docker Compose（推荐）

### 3.1 上传代码

只需要 `deploy/` 和 `social-auto-upload-main/` 两个目录（见第 1 节 rsync 命令）。

> 不要传 `venv/`（Windows 虚拟环境，服务器上用不了）、`social-auto-upload-main/sau_frontend/node_modules/`（2 万多个文件）。
> 想连历史数据一起搬，见第 8.3 节。

### 3.2 配置环境变量

```bash
cd /root/flowOps/deploy
cp .env.deploy.example .env.deploy
vi .env.deploy
```

必改/必填：

| 变量 | 值 | 说明 |
| --- | --- | --- |
| `YUNCE_HOST` | `0.0.0.0` | 容器里必须监听全网卡，否则端口映射进不来 |
| `YUNCE_ALLOW_REMOTE` | `1` | 少了它后端拒绝启动 |
| `YUNCE_ACCESS_TOKEN` | `openssl rand -hex 24` 的输出 | 少了它后端拒绝启动 |
| `YUNCE_CORS_ORIGINS` | 留空 | 前后端同源时不需要跨域 |
| `WB_API_KEY` / `WB_BASE_URL` / `WB_MODEL` | 你的 AI 接口 | AI 文案、内容包用；不填就只是这些功能不可用 |

`chmod 600 .env.deploy`。

### 3.3 构建并启动

```bash
docker compose up -d --build
docker compose logs -f yunce        # 首次启动会跑库初始化 + 建默认配置
```

镜像里做了四件事，知道就行：前端 `npm run build` → 产物摆到 `/app/index.html` + `/app/assets/`；Python 依赖按 `requirements.txt` 装；`playwright install --with-deps chromium`；启动前执行 `db/createTable.py` 建 `user_info`/`file_records`（其余库是模块导入时自建）。

### 3.4 验证

```bash
docker compose ps                          # healthy
curl -s http://127.0.0.1:5409/health       # {"code":200,...,"status":"ok"}
bash smoke.sh                              # 首页/静态资源/鉴权/业务接口全绿
```

### 3.5 日常运维

```bash
docker compose logs -f yunce                 # 实时日志
docker compose restart yunce                 # 重启（数据在 ./data，随便重启）
docker compose down                          # 停（不删卷）
docker compose up -d --build                 # 更新（见第 9 节）
docker exec -it yunce sh                     # 进容器排查
tail -f data/logs/app.log                    # 业务日志
```

---

## 4. 路线 B：裸机 systemd（不用 Docker）

适合服务器已经在跑别的东西，或者不想装 Docker。脚本会把它装到 `/opt/yunce`：

```bash
ssh root@[SERVER_IP]
mkdir -p /root/flowOps
# 同样用 rsync 把 deploy/ 和 social-auto-upload-main/ 传上来
cd /root/flowOps
bash deploy/install-baremetal.sh
```

脚本依次做：装系统依赖（含 `fonts-noto-cjk`、chromium 运行库）→ 建 `yunce` 用户 → 代码同步到 `/opt/yunce/app` → venv + `requirements.txt` → `playwright install --with-deps chromium` → 生成 `/opt/yunce/app/.env`（内含随机令牌）→ 构建前端并摆好静态产物 → 建 `database.db` 表 → 装 `yunce.service` 并启动 → 轮询 `/health`。

要点：

- 令牌在脚本输出里打印一次，也在 `/opt/yunce/app/.env` 里
- 有 `npm` 会自动构建前端；没有则按脚本提示在本机 build 后拷 `dist/index.html`、`dist/assets/`、`dist/vite.svg` 过去
- 服务管理：`systemctl status|restart|stop yunce`，日志 `journalctl -u yunce -f` 和 `/opt/yunce/app/logs/app.log`
- 手动初始化库（重装/换机器时）：`cd /opt/yunce/app/db && /opt/yunce/venv/bin/python createTable.py`

---

## 5. 前端 API 地址怎么定（最容易踩的坑）

`src/config.js` 的逻辑：

```js
const rawBase = import.meta.env.VITE_API_BASE_URL || 'http://localhost:5409'
// 绝对 URL  → 原样使用
// 相对路径  → hostname 是 localhost/127.0.0.1 就直连 5409，否则拼上当前 origin
```

| 场景 | `VITE_API_BASE_URL` | 结果 |
| --- | --- | --- |
| 同域反代（推荐，本指南默认） | `/` | 请求打到 `https://你的域名/xxx`，Nginx 转给 5409，无跨域问题 |
| 前后端分域 | `https://api.example.com` | 直连该域，必须配 `YUNCE_CORS_ORIGINS` |
| 开发机 | 不设 | 走 5173 的 vite proxy / 直连 127.0.0.1:5409 |

Docker 方式改这个只要：`docker compose build --build-arg VITE_API_BASE_URL=https://api.example.com` 或改 `deploy/docker-compose.yml` 里的 `VITE_API_BASE_URL`。

> 仓库里原先那份 `social-auto-upload-main/Dockerfile`（上游遗留，用 `sed` 改前端地址，会产出 `VITE_API_BASE_URL=http://` 这种坏值）**已删除**，统一用 `deploy/Dockerfile`。

---

## 6. Nginx + HTTPS

```bash
# 1) 反代配置
cp /root/flowOps/deploy/nginx-yunce.conf /etc/nginx/conf.d/yunce.conf
vi /etc/nginx/conf.d/yunce.conf          # 把 ops.example.com 全换成你的域名
nginx -t && systemctl reload nginx

# 2) 证书
apt-get install -y certbot python3-certbot-nginx
certbot --nginx -d ops.example.com       # 自动改写 conf 里的 443 证书路径
```

三个必须项（配置里已写好，别删）：

| 配置 | 为什么 |
| --- | --- |
| `client_max_body_size 200m` + `proxy_request_buffering off` | 视频上传，后端上限 160MB，缓冲区先落盘会白等 |
| `proxy_buffering off` | 扫码登录、私信同步是 **SSE** 长连接，缓冲会把二维码/进度卡死 |
| `proxy_read_timeout 1800s` | 发布任务同步等待可能好几分钟，默认 60s 会 504 |

没域名也能上：把 `server_name` 改成 `_`，只留 80 端口那段，访问 `http://[SERVER_IP]/`（令牌照样生效）。

---

## 7. 首次登录（没有登录页，令牌靠 localStorage）

后端校验方式：请求头 `X-YunCe-Token: <token>`，或 `?token=<token>`（源码 `runtime_config.py`）。前端从 `localStorage` 的 `yunce_access_token` 取值（源码 `sau_frontend/src/config.js`），**项目里没有登录界面**，所以要手动写一次：

1. 浏览器打开 `https://你的域名/`
2. F12 → Console，粘贴：

```js
localStorage.setItem('yunce_access_token', '把 /root/flowOps/deploy/.env.deploy 里的 YUNCE_ACCESS_TOKEN 粘这里')
location.reload()
```

3. 页面正常出数据即可。换浏览器/清缓存后要重新写一次。

> 别用 Nginx 统一追加 `X-YunCe-Token` 头来做"自动登录"——那等于对所有人取消鉴权。
> 想用 HTTP Basic Auth 外加一层：Nginx 加 `auth_basic` 即可，和令牌不冲突。

接着在「账号管理」里逐个平台**扫码登录**：二维码由服务器上的 Chromium 打开平台页面截取，经 SSE 推给前端。所以：

- 服务器必须能出网访问那些平台
- 二维码过期就点重新获取（后端会重启浏览器）
- Cookie 落在 `data/cookiesFile/`（Docker）或 `/opt/yunce/app/cookiesFile/`（裸机），**这就是账号本身，按密码级别保管**

---

## 8. 数据、备份、迁移

### 8.1 目录对照

| 内容 | Docker | 裸机 |
| --- | --- | --- |
| SQLite 库（账号/发布队列/私信/线索/内容包） | `deploy/data/db/` | `/opt/yunce/app/db/` |
| 平台 Cookie JSON | `deploy/data/cookiesFile/` | `/opt/yunce/app/cookiesFile/` |
| 上传的视频 | `deploy/data/videoFile/` | `/opt/yunce/app/videoFile/` |
| 上传的图文 | `deploy/data/uploadFile/` | `/opt/yunce/app/uploadFile/` |
| 业务日志 | `deploy/data/logs/app.log` | `/opt/yunce/app/logs/app.log` |
| 环境变量（含令牌） | `deploy/.env.deploy` | `/opt/yunce/app/.env` |

### 8.2 定时备份

```bash
# Docker（在 deploy/ 目录）
20 3 * * * cd /root/flowOps/deploy && bash backup.sh >> /var/log/yunce-backup.log 2>&1

# 裸机
20 3 * * * DATA_DIR=/opt/yunce/app ENV_FILE=/opt/yunce/app/.env OUT_DIR=/opt/yunce/backups \
          bash /root/flowOps/deploy/backup.sh >> /var/log/yunce-backup.log 2>&1
```

`backup.sh` 会先让容器把 SQLite WAL 落盘再打包（WAL 模式下直接拷 `.db` 可能拿到半截事务），默认保留 14 天。

恢复：

```bash
docker compose stop yunce
cp -r data/db data/db.bak.$(date +%s)          # 先留一份
tar -xzf backups/yunce-YYYYmmdd-HHMMSS.tar.gz -C ./
docker compose start yunce
```

### 8.3 从本机迁到服务器

```bash
# 在你的 Windows 机器上（PowerShell）
scp -r social-auto-upload-main\db        root@[SERVER_IP]:/root/flowOps/deploy/data/
scp -r social-auto-upload-main\cookiesFile root@[SERVER_IP]:/root/flowOps/deploy/data/
scp -r social-auto-upload-main\videoFile   root@[SERVER_IP]:/root/flowOps/deploy/data/
```

> 注意：`social-auto-upload-main/database.db`（项目根目录那个）是历史误跑 `createTable.py` 生成的空库，真正业务库在 `db/` 下，别拷错也别在意它。
> 迁完 `docker compose restart yunce`；登录状态直接可用，不用重新扫码。

---

## 9. 更新发布

```bash
# 1) 同步新代码（本地 → 服务器，排除数据目录）
rsync -avz --exclude 'venv' --exclude 'node_modules' \
  --exclude 'social-auto-upload-main/db' --exclude 'social-auto-upload-main/cookiesFile' \
  --exclude 'social-auto-upload-main/logs' \
  ./deploy ./social-auto-upload-main root@[SERVER_IP]:/root/flowOps/

# 2) 备份 + 重建
ssh root@[SERVER_IP]
cd /root/flowOps/deploy && bash backup.sh && docker compose up -d --build
bash smoke.sh
```

- 数据库结构升级是**自动**的：各模块导入时跑幂等迁移（`db_utils.apply_migrations`）
- 所以**更新前必须备份**（脚本第 1 步已经做了）
- 数据目录不在镜像里，重建镜像不会丢数据

裸机更新：重跑 `bash deploy/install-baremetal.sh`（会重新同步代码/依赖/前端并 `systemctl restart`，`.env` 与数据目录不动），或手工 `cd /opt/yunce/app && git pull` 后 `systemctl restart yunce`。

---

## 10. 上线检查清单

- [ ] `curl https://你的域名/health` 返回 `status: ok`
- [ ] `bash smoke.sh` 全绿（含"无令牌必须 401"）
- [ ] 浏览器写过 `yunce_access_token`，刷新后各页面有数据
- [ ] 至少一个平台扫码登录成功，`data/cookiesFile/` 里出现对应 JSON
- [ ] 发一条草稿/定时任务，`data/logs/app.log` 里能看到发布链路日志
- [ ] 私信工作台能拉到会话（说明后台同步线程活着）
- [ ] `bash backup.sh` 出了一份 tar.gz，且能解开
- [ ] 服务器防火墙只放 22/80/443，5409 从公网打不开
- [ ] `TZ` 是 Asia/Shanghai，日历上的定时时间对得上

---

## 11. 排障对照表

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 容器起来就退出，日志 `远程访问已开启，但未设置 YUNCE_ACCESS_TOKEN` | `.env.deploy` 没被读到 / 变量名拼错 | 确认文件在 `deploy/` 下、`env_file` 路径对，`docker compose config` 看实际生效值 |
| 同上但提示 `未设置 YUNCE_ALLOW_REMOTE=1` | 只设了 token | 补 `YUNCE_ALLOW_REMOTE=1` |
| 首页 404 或白屏 | 前端产物没进镜像（`/app/index.html` 缺失） | `docker exec yunce ls /app/index.html /app/assets`；重建镜像 |
| 页面能开，接口全 401 | 浏览器没写令牌 | 第 7 节的 `localStorage` 一行 |
| 接口报跨域 / 请求打到 `localhost:5409` | 打包时 `VITE_API_BASE_URL` 不对 | 用 `VITE_API_BASE_URL=/` 重新 build |
| 扫码头像位置一直转圈、二维码不出来 | Nginx/Cloudflare 缓冲了 SSE | `proxy_buffering off;`、`proxy_cache off;`；CF 关掉对 SSE 的缓冲或直接灰云 |
| 上传视频 413 | Nginx 限制 | `client_max_body_size 200m` |
| 上传超时 504 | 请求体缓冲 + 超时短 | `proxy_request_buffering off;`、`proxy_read_timeout 1800s` |
| 发布报 `Playwright 浏览器未安装` | 没装 chromium / 路径不对 | `playwright install chromium`，确认 `PLAYWRIGHT_BROWSERS_PATH` 一致 |
| 发布中途 `Target page/browser closed` | `/dev/shm` 太小 | compose 里 `shm_size: "1gb"`（裸机给容器/机器足够内存） |
| 截图/二维码里中文是方块 | 缺中文字体 | `apt-get install fonts-noto-cjk`（镜像已内置） |
| 定时发布早/晚 8 小时 | 时区 | `TZ=Asia/Shanghai` 且容器内 `date` 校验 |
| `database is locked` | 开了多进程/多副本/多 worker | 单进程单副本；SQLite 已开 WAL + busy_timeout |
| 同一任务发了两遍 | 起了 2 个副本或 2 个 gunicorn worker | 见上 |
| 服务器上登录抖音/小红书老弹验证 | 机房 IP 风控 | 换住宅/专线代理，或先在本地登录后把 `cookiesFile/*.json` 迁到服务器 |
| 日志中文乱码 | 编码环境 | `PYTHONIOENCODING=utf-8`（镜像已设） |

---

## 12. 这套部署的已知边界

1. **单进程**：不能水平扩容、不能开多 worker。要上多实例得先把内存队列/线程/SSE 换成外部队列，那是改造不是部署。
2. **没有多用户体系**：一个令牌 = 全权限。多人用就靠 Nginx Basic Auth 分账号，或者你们内部约定。
3. **机房 IP 风控**：平台侧对云服务器 IP 更敏感，扫码登录/发布可能比家里网络更容易触发验证，必要时走代理或迁 Cookie。
4. **AI 文案默认指向第三方中转**（`WB_BASE_URL` 默认值在 `.env.example` 里），生产建议改成你自己的合规端点，别把 key 提交进仓库（`.env` 已在 `.gitignore`）。
5. **`uploader/xhs_uploader` 那套需要本地签名服务 `127.0.0.1:11901`**，Web 端的小红书流程走的是浏览器版 `xiaohongshu_uploader`，**不需要**这个服务，别被 `conf.py` 里的 `XHS_SERVER` 误导。

---

## 附：文件清单

```
deploy/
├── README.md                  ← 本文件
├── Dockerfile                 生产镜像（前端构建 + Playwright Chromium + 单进程启动）
├── docker-compose.yml         单容器编排（含数据卷、healthcheck、shm_size）
├── .env.deploy.example        环境变量模板（复制成 .env.deploy）
├── nginx-yunce.conf           Nginx 反代（SSE / 大文件 / HTTPS）
├── yunce.service              裸机 systemd 单元
├── install-baremetal.sh       裸机一键安装
├── backup.sh                  数据备份（db + Cookie + env）
└── smoke.sh                   上线冒烟（健康/首页/静态/鉴权/业务接口）
```
