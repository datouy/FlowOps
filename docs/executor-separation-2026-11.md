# 发布执行器隔离与并发加固（2026-11）

> 本文记录当前工作区代码已经实现的发布执行链路、部署方式和验证边界。
> 核验日期：2026-11-20。真实平台发布、真实二维码登录和生产进程强杀演练仍未完成。

## 1. 解决的问题

本轮针对五个已确认缺口：

| 编号 | 缺口 | 当前处理 |
| --- | --- | --- |
| P0-1 | 发布 worker 与 Web 同进程 | 新增 `publish_executor.py`；支持 `web` / `executor` / `all` 三种进程角色 |
| P0-2 | `/postVideoBatch` 绕过任务模型 | 改为全批校验后逐条 `enqueue_job()`，不再调用 `dispatch_publish()` |
| P1-1 | 同账号锁只在当前进程有效 | 发布、私信、评论共用 SQLite `account_leases` 跨进程租约 |
| P1-2 | heartbeat 调用了不存在的函数 | claim 返回唯一 `leaseToken`，heartbeat 使用 `heartbeat_token()` 校验 token |
| P1-3 | 单个共享 Chromium 崩溃影响全部账号 | 账号稳定映射到多个 Chromium 分片，每片独立实例和锁 |

目标是先补齐任务事实、执行隔离和互斥语义，再根据真实指标调 worker 数、分片数和平台并发。当前没有生产性能数据，因此不能把本次改造描述为已经验证的吞吐提升。

## 2. 进程模式

`FLOWOPS_PROCESS_ROLE` 控制进程职责：

| 值 | Web/API | 发布 worker | 排期/追踪/租约回收 | 账号巡检 | 用途 |
| --- | --- | --- | --- | --- | --- |
| `all` | 是 | 是 | 是 | 是 | 默认值，兼容旧单容器部署 |
| `web` | 是 | 否 | 否 | 否 | 只接收请求、写任务、提供界面和 SSE |
| `executor` | 否 | 是 | 是 | 是 | 运行 `publish_executor.py` |

独立执行器入口：

```powershell
cd E:\project\Dtou\flowOps
$env:FLOWOPS_PROCESS_ROLE = 'executor'
.\venv\Scripts\python.exe .\social-auto-upload-main\publish_executor.py
```

Windows 也可直接运行根目录 `run_executor.bat`。Web 终端需要显式设置 `FLOWOPS_PROCESS_ROLE=web`，否则默认仍是 `all`，两个进程都会尝试拥有发布运行时。

## 3. 任务流与唤醒

```text
POST /postVideo 或 /postVideoBatch
  -> jobs（SQLite，任务事实）
  -> Web 进程返回 job_id / job_ids
  -> executor dispatcher 每 2 秒查 pending jobs
  -> 进程内 queue.Queue（只负责本地唤醒）
  -> claim_job_token（原子 UPDATE，生成 leaseToken）
  -> publish_items（单素材 × 单账号）
  -> account_serial + publish_guard
  -> Playwright 发布
  -> 远端终态校验与结果回写
```

SQLite 是任务事实，内存队列不是持久化来源。Web 进程写库后，即使 executor 的 HTTP 进程没有共享 Python 内存，也能由数据库轮询发现任务。

任务租约由 `FLOWOPS_EXECUTOR_LEASE` 控制，默认 120 秒。执行期间由 heartbeat 续期；过期任务回收时，未提交子任务回 `pending`，已经提交但结果未知的子任务进入 `uncertain`，不会自动重发。

启动恢复只处理没有有效租约的 `running` 任务。另一个执行器仍持有有效租约时，新进程不会把它的任务重置。

任务进入终态、重新排队、取消或转为远端 processing/scheduled 后，会清空 `executor_id`、`lease_token` 和租约时间；旧 token 不能再通过 heartbeat 续命。普通任务详情接口也不会返回 `lease_token`。

## 4. 同账号跨进程互斥

`utils/account_serial.py` 使用两层锁：

1. 当前进程的 `threading.Lock`，降低同进程 SQLite 竞争。
2. 共享 SQLite 的 `account_leases`，在 Web、executor、DM、评论、账号巡检和手动健康检查之间保证同账号串行。

默认数据库是 `db/account_leases.db`，可用 `FLOWOPS_ACCOUNT_LEASE_DB` 覆盖。租约默认 180 秒，由后台心跳续期；包括发布的操作链路必须使用统一的 `account_key(platform, account_name, account_id, cookie)` 命名空间。

`check_cookies_concurrently()` 接受可选的 `(platform_type, file_path, lease_key)` 第三项。应用内的巡检、单账号检查、批量检查和后台账号任务都会传入与发布一致的账号键；实际检查在独立线程事件循环中执行，等待跨进程租约时不会阻塞 Web 事件循环。

相关配置：

```text
FLOWOPS_ACCOUNT_LEASE_DB=db/account_leases.db
FLOWOPS_ACCOUNT_LEASE_SECONDS=180
FLOWOPS_ACCOUNT_MIN_INTERVAL=3
FLOWOPS_ACCOUNT_JITTER=2.5
```

SQLite 文件必须位于同一台主机的持久化本地卷。不要把运行中的 SQLite 放到 NFS、对象存储或多台容器各自独立的目录中。

## 5. Chromium 分片

共享浏览器不再只有一个全局实例。账号按 Cookie 路径 SHA-256 稳定映射到固定分片：

```text
FLOWOPS_SHARED_BROWSER=1
FLOWOPS_SHARED_BROWSER_SHARDS=2
```

分片数范围 1-16，实例按需惰性启动。一个分片崩溃只影响该分片上的 context，下一次访问会重建连接。账号级 `storage_state`、代理、指纹参数和 init script 仍按 context 隔离。

代价是分片越多，Chromium 基础进程占用越高。应从 2 个分片起步，用真实并发、RSS、崩溃率和发布延迟决定是否增加。`FLOWOPS_SHARED_BROWSER=0` 仍可回退到一账号一持久化 profile 的隔离模式。

## 6. Docker split 部署

覆盖文件：`deploy/docker-compose.split.yml`。

```bash
cd deploy
docker compose -f docker-compose.yml -f docker-compose.split.yml config
docker compose -f docker-compose.yml -f docker-compose.split.yml up -d --build
docker compose logs -f yunce yunce-executor
```

两个服务共享：

- `data/db`
- `data/cookies`
- `data/cookiesFile`
- `data/videoFile`
- `data/uploadFile`
- `data/logs`

`data/logs` 下由 `FLOWOPS_LOG_DIR` 再分出 `web/` 与 `executor/`。两个进程可以读取同一业务日志目录，但不能同时使用 loguru 轮转同一个文件；split 覆盖文件已经为两侧设置独立目录。

执行器容器不监听 HTTP 端口，因此 split 文件关闭了从 Dockerfile 继承的 5409 healthcheck。Web 的 `/health/ready` 会读取 `publish_executors.heartbeat_at`；`FLOWOPS_PROCESS_ROLE=web` 时，30 秒内没有 executor 心跳会阻塞就绪检查。

回退到兼容模式时，不加载覆盖文件：

```bash
docker compose -f docker-compose.yml up -d --build
```

回退前先停止 `yunce-executor`，避免它与重新启动的 `all` 模式 Web 同时拥有发布会话。

## 7. 执行器协议与状态

现有管理接口：

| 接口 | 语义 |
| --- | --- |
| `GET /api/v2/executors` | 当前进程角色、执行器心跳、running 任务和浏览器池 |
| `POST /api/v2/executors/claim` | 原子抢占任务并返回唯一 `leaseToken` |
| `POST /api/v2/executors/heartbeat` | 用 `jobId + leaseToken` 续期；无效 token 返回 409 |
| `POST /api/v2/executors/release-stale` | 回收已过期租约 |
| `GET /api/v2/serial/status` | SQLite 账号租约、限速和浏览器池状态 |

这些接口受团队管理权限保护。当前受支持的拆分执行形态是同一主机、共享 SQLite 的 `publish_executor.py`；HTTP claim/heartbeat 已可用于租约控制，但还没有把远端 worker 的逐 item 结果回传封装成完整远程执行 SDK。

## 8. 性能采集

当前生产基线仍为“未测”。上线前先采集同等业务窗口的数据：

只读基线工具（不创建、领取或发布任务）：

```bash
cd social-auto-upload-main
python production_audit.py --hours 24 --format json --output ../deploy/out/acceptance/before.json
# 完成同口径验收后
python production_audit.py --hours 24 --format json \
  --compare ../deploy/out/acceptance/before.json \
  --output ../deploy/out/acceptance/after.json
```

工具按 `finished_at` 统计完成窗口吞吐和延迟，按当前状态统计 backlog，并区分账号租约的 live、expired 和 released。SQLite 连接使用只读 URI；输出文件才可能写入磁盘。

```sql
-- 当前积压：pending 与已经到点的 scheduled 任务
SELECT status, COUNT(*) AS jobs
FROM jobs
WHERE status IN ('pending','scheduled')
GROUP BY status;

-- 近 24 小时任务延迟：从创建到完成，单位秒
SELECT COUNT(*) AS finished,
       ROUND(AVG((julianday(finished_at)-julianday(created_at))*86400),1) AS avg_seconds,
       ROUND(MAX((julianday(finished_at)-julianday(created_at))*86400),1) AS max_seconds
FROM jobs
WHERE finished_at IS NOT NULL
  AND created_at >= datetime('now','-1 day');

-- 子任务吞吐与失败率
SELECT status, COUNT(*) AS items
FROM publish_items
WHERE created_at >= datetime('now','-1 day')
GROUP BY status;

-- 正在执行的账号级任务数
SELECT COUNT(*) AS running_items
FROM publish_items
WHERE status='running';
```

至少记录：单素材单账号发布 P50/P95、每平台并发数、队列积压峰值、成功/失败/uncertain 数量、Chromium 分片 RSS 与崩溃次数。没有 before/after 的同口径样本，不应宣称性能提升百分比。

## 9. 已验证与未验证

已完成的自动化验证：

- 账号检查、发布调度、共享浏览器、运行时稳定性和执行器分离合并测试：66 passed。
- 新增覆盖执行器分离、恢复租约、batch 任务化、heartbeat 409、Web 执行器健康检查、跨进程账号锁、同账号并发探针串行和浏览器分片测试。
- Python 编译检查和针对改动文件的 Ruff 检查通过。
- 生产审计工具专项测试：7 passed；覆盖只读 SQLite、完成窗口、终态吞吐、P95、租约分类、executor gate 和 baseline delta。
- 全量后端测试最近两次均为 708 passed、0 failed；最终一轮 157.18 秒。此前 681 passed / 20 failed 是旧快照，已由当前全量重跑替代。

未完成的生产验收：

- 抖音、小红书、视频号真实发布和远端终态回查。
- 真实二维码登录，以及登录态跨重启保持。
- Web/executor 分别重启、执行器强杀、Chromium 分片崩溃的真实演练。
- Docker split 编排实际启动；当前开发主机没有 Docker，只能做静态文件检查。
- `deploy/verify_split.sh` 已通过 Git Bash `bash -n`，但尚未在 Linux 验收机执行 Compose 和容器；当前主机没有 Docker。
- 生产吞吐、P95 延迟、内存和队列积压的前后对比。

## 10. 当前剩余风险

1. SQLite 仍是单主机任务事实源，没有跨主机数据库或分布式队列。
2. 多 executor 同时运行未作为生产形态验收；当前建议一个 Web 和一个 executor。
3. 调度和追踪尚无独立 leader election；横向扩容前需要补齐。
4. Chromium 分片降低单点影响范围，但仍是同机进程；机器级故障会同时影响全部分片。
5. 团队授权仍是 `admin` / `operator` / `viewer` 加账号 scope；剪辑、审核、运营、管理员四职责的独立权限点尚未形成完整闭环。
