# 云策 YunCe · 三大核心功能的稳定性改造（第一轮）

> 日期：2026-11-09
> 范围：**多平台分发** + **私信统一回复** + **运行稳定性**
> 原则：不改业务规则，只补"坏了会没人知道"的地方

---

## 0. 这轮做了什么（一句话版）

三条主线原本都能跑，问题不在功能，而在**故障可见性与自愈**：

| 核心功能 | 原来的隐患 | 这轮的处置 |
|---|---|---|
| **多平台分发** | 素材被删、凭据文件坏掉、代理地址少写 scheme、文案违规——全都要**起一次浏览器、烧一次账号动作**才知道 | 新增**发布前预检**：六类输入问题在浏览器启动前一次性拦下，跳过而不计失败 |
| **私信统一回复** | 三个平台的同步线程一旦退出，界面照旧显示"自动同步运行中"；发送是一把全局锁，十个账号排队等一个会话 | 同步线程纳入**心跳 + 看门狗**（死即重启）；发送锁降到**账号粒度**（同账号串行、跨账号并行） |
| **运行稳定性** | `/health` 恒返回 `ok`——库锁死、磁盘满、账号全掉、线程全死都不影响它；12 处裸 daemon 线程无人看管 | 新增 `/health/ready` 就绪探针、`/api/v2/runtime/status`、**后台守护**、首页**运行状态卡** |

新增 4 个模块、1 个前端组件、23 个测试；全量测试 **582 passed**，前端 `vite build` 通过。

---

## 1. 多平台分发：发布前预检

### 1.1 问题

发布链路的骨架其实很成熟（原子领取任务、item 级状态、账号健康门禁、风控降速门禁、幂等键、账号锁）。真正缺的是**入口过滤**：

一条子任务跑起来要经历"起 Chromium → 加载持久化 profile → 校验登录态 → 进上传页 → 上传素材"，
而下面这些情况**在启动浏览器之前就已经注定失败**：

- 素材文件被素材清理任务删了，或者上传中断留下 0 字节文件；
- 账号凭据文件被手工清掉、被改坏，或者**解密不了**（主密钥丢了/换了机器）；
- 代理地址填成 `proxy.example.com:8080`（少 scheme）——最危险的一种"小错"：看起来有隔离，实际要么请求全失败，要么静默回落直连，把真实 IP 和账号永久绑在一起；
- 指纹 sidecar 丢了，每次发布都像换了一台新设备；
- 文案含绝对化用语/手机号，平台侧审核必然不过，但我们记成"发布失败"。

结果是：**失败率这个指标被自己的脏数据污染**，真正的平台拒绝反而看不出规律；账号动作被白白消耗，而账号动作正是风控最敏感的资源。

### 1.2 做法

新增 `publish_preflight.py`，在 `_run_job` 的 item 循环里、`_account_lock` 之前插入一道门禁：

```
账号健康门禁 → 风控降速门禁 → 【发布前预检】 → 账号串行锁 → 浏览器动作
```

六类检查（每条都区分 `blocking` / `warning`）：

| 检查 | blocking 条件 | warning 条件 |
|---|---|---|
| `assets` | 文件不存在 / 0 字节 / 没有素材 | 扩展名不在白名单、视频任务带多个素材 |
| `account` | 凭据不存在 / 解密失败 / 读不出内容 / 没有 Cookie | 检查过程异常 |
| `proxy` | 地址缺 `http://`·`https://`·`socks5://` | 没绑独立出口（走本机 IP）、地址内嵌凭据 |
| `fingerprint` | —（不阻塞，只影响风控） | 没有指纹记录、指纹自相矛盾 |
| `compliance` | 复用编辑期同一套规则（标题超长、绝对化用语、手机号、联系方式、素材缺失） | 话题过多、外链、同名标题 |
| `disk` | 剩余空间 < 200MB | — |

**关键设计**：

- **预检不通过 → `skipped`，不是 `failed`**：不消耗 `attempts`、不写账号健康状态、不发任何账号动作。重试同样的脏输入没有意义。
- **预检自己异常 → 放行**：预检绝不能成为新的故障点。
- **凭据判断走 `credential_exists` / `read_storage_state`**：账号凭据现在的正式存储是加密的 `.enc`，直接 `is_file()` 会把加密账号全判成"凭据丢失"（这个坑代码里原本就有注释警告）。
- **解密失败单独归因**：`account_cookie_undecryptable` ≠ "没登录"。前者是主密钥丢了，重新扫码会把问题掩盖成"偶发掉线"。
- **去重**：预检与合规检查有重叠（素材、标题），按文案去重，UI 上不刷两遍。
- **开关**：`FLOWOPS_PUBLISH_PREFLIGHT`（默认开）。单测是隔离文件系统的，`tests/conftest.py` 里默认关掉。

### 1.3 真实环境里立刻抓到的问题

用真实账号跑预检，发现两件之前只能表现为"账号又挂了"的事：

```
94ddbaa0...  -> asset_missing                       + proxy_absent(未绑独立出口)
f985cb5e...  -> asset_missing, account_cookie_undecryptable  + proxy_absent
```

第二个账号（视频号，状态本来就是"失效"）的凭据**解不开**——不是没登录，是密文文件和当前主密钥不匹配。
以前这种情况只会显示"账号失效，请重新扫码"，扫码之后大概率还是连不上；现在会直接说"凭据解密失败，主密钥可能已丢失或更换"。
另外两个账号都**没有绑定独立出口**，发布走的是本机 IP——多账号这样做等于自己给自己打上"账号矩阵"标签。

---

## 2. 私信统一回复：线程守护 + 锁粒度

### 2.1 同步线程的静默死亡

三个平台的同步循环（`dm_platform_common.BaseDmSync._background_loop`）都有完整的 `try/except` + 退避，
但**没有人管线程本身**：一旦线程退出（逃出 try 的异常、Event 被永久清除、死锁），
`_worker_started` 仍然是 `True`，`start_background()` 幂等直接返回——**永远不会自动恢复**。
而界面显示的是 `status_payload()` 里的 `last_*` 字段，看起来"还在同步"。

处置：

1. 循环每跑一圈打一次心跳：`runtime_guard.beat(guard_name)`；业务失败用 `beat(ok=False, error=...)` 单独记（**同步失败 ≠ 线程故障**，两件事不能混）。
2. `BaseDmSync` 新增 `restart_background()`：把 `_worker_started` 打回 `False` 再启动。游标和消息都在库里，重启不会重拉历史（去重靠 `external_message_id`）。
3. 三个平台模块各加一个 `restart_background()` 包装，交给看门狗做重启器。

### 2.2 一把全局锁 → 账号粒度

`platform_reply.py` 原来是一把 `_send_lock = threading.Lock()`：**所有账号的私信回复排成一队**。
每条回复都要"起浏览器 → 开会话 → 输入 → 点击 → 等 2.5 秒确认 → 关浏览器"，十几秒起步；
三个平台、十个账号同时有客户来信时，最后一个客户要等好几分钟。

改成 `_send_lock_for(account)`：**同账号串行、跨账号并行**。
平台侧真正在意的是"同一个账号不要并发操作"——不同账号本来就是不同的人、不同的设备、不同的出口 IP，串起来没有风控收益，只有延迟。

### 2.3 顺手修掉的一个隐患

`comment_sync.start_comment_loop()` 原本**没有幂等保护**：被调用两次就起两个同步线程，
两个线程同时拉同一批评论、抢同一个游标。看门狗重启线程之后这个隐患会从"潜在"变成"必然"，所以补上了幂等 + `restart_comment_loop()`。

---

## 3. 运行稳定性：看得见的健康度

### 3.1 后台守护（`runtime_guard.py`）

12 处裸 daemon 线程现在统一登记：

| 任务 | 心跳 | 重启器 |
|---|---|---|
| `publish-scheduler` | 每 20s | `publish_jobs.restart_publish_runtime` |
| `publish-tracker` | 每 60s | 同上 |
| `account-patrol` | 12h 一轮 | `publish_jobs.restart_patrol` |
| `stats-reflow` | 24h 一轮 | `stats_reflow.restart_stats_loop` |
| `comment-sync` | 900s 一轮 | `comment_sync.restart_comment_loop` |
| `douyin-dm-sync` | ≤10s（空闲也打） | `douyin_dm_sync.restart_background` |
| `xhs-dm-sync` | 同上 | `xiaohongshu_dm_sync.restart_background` |
| `channels-dm-sync` | 同上 | `channels_dm_sync.restart_background` |

watchdog 每 30s 巡检：**线程死** → 重启（指数退避 30s→600s，最多 5 次）；**心跳停滞** → 判卡死并重启。
状态全在内存，进程重启即重置——这没问题，因为进程重启本身就是一次全量重置。

### 3.2 就绪探针（`health_probe.py`）

`/health` 保持"进程活着"语义不动（探针依赖它做重启判断），新增 `/health/ready`：

| 检查 | 判定 |
|---|---|
| 4 个 SQLite | `BEGIN IMMEDIATE` 拿写锁再回滚（不脏数据）；拿不到 = blocking |
| 磁盘 | < 500MB blocking，< 2GB warning |
| 后台任务 | 线程死/心跳停 = blocking；连续业务失败 = warning |
| 队列 | 有任务入队超 10 分钟仍未被领取 = warning（**worker 池死了的症状**） |
| 账号 | 全部失效 = warning（0 个账号也是 warning） |
| 数据回流 | 超过 3 天没成功回流 = warning（抓取链路断了） |
| AI | 未配置 = warning（只影响文案） |

任一 blocking 失败 → **HTTP 503**，`data.blockers` 里给具体项。

> 为什么单独加 `queue` 这一项：worker 线程池阻塞在队列上是**正常**的，不能用心跳判断它死了；
> 但它死了的症状非常明确——任务入队后一直 `pending`。看积压比看线程状态更接近用户感受。

### 3.3 前端：首页运行状态卡

新增 `SystemHealthCard.vue`，插在首页三张统计卡下面：

- 顶部：`服务正常` / `有阻塞项` 徽章 + 上次检查时间 + 刷新；
- 四个数字：检查项 / 阻塞 / 提醒 / 后台任务在线数；
- 只列**有问题**的检查项（全绿时列十几条只会让人懒得看）；
- 后台任务逐条：心跳多久前、重启过几次、是否已停/卡死；
- **60 秒自动刷新**——稳定性问题的特点就是"悄悄坏掉"，不能指望用户主动点。

配套接口 `/api/v2/system/health` **永远返回 200**、就绪与否放在 `data.ready` 里：
否则前端响应拦截器会把 503 当成接口报错，弹一个红条——而用户只是想知道"现在有没有问题"。

### 3.4 运维开关

`FLOWOPS_DISABLE_BACKGROUND=1`：只起 Web、不跑任何后台任务。
线上出问题时第一动作往往是"先把自动动作停下来，别再往平台上打"，但又必须保留界面看清状态。

---

## 4. 改动清单

| 文件 | 类型 | 说明 |
|---|---|---|
| `runtime_guard.py` | **新增** | 后台任务注册 / 心跳 / 看门狗 / 状态聚合 |
| `health_probe.py` | **新增** | 就绪探针（库·磁盘·任务·队列·账号·回流·AI） |
| `publish_preflight.py` | **新增** | 发布前预检（素材·凭据·代理·指纹·合规·磁盘） |
| `utils/account_fingerprint.py` | 修改 | 新增 `fingerprint_consistency()`：指纹内部自洽检查（静态，不起浏览器） |
| `publish_jobs.py` | 修改 | 接入预检；`KIND_PRECHECK`；调度/追踪/巡检加心跳；新增 `restart_*` |
| `dm_platform_common.py` | 修改 | 循环心跳；`guard_name`；`restart_background()` |
| `douyin/xiaohongshu/channels_dm_sync.py` | 修改 | 各加 `restart_background()` 包装 |
| `comment_sync.py` | 修改 | 补幂等保护；心跳；`restart_comment_loop()` |
| `stats_reflow.py` | 修改 | 心跳；`restart_stats_loop()` |
| `platform_reply.py` | 修改 | 全局发送锁 → 账号粒度 `_send_lock_for()` |
| `sau_backend.py` | 修改 | 后台任务登记 + 看门狗启动；`/health/ready`、`/api/v2/runtime/status`、`/api/v2/system/health`；`FLOWOPS_DISABLE_BACKGROUND` |
| `sau_frontend/src/components/SystemHealthCard.vue` | **新增** | 首页运行状态卡（60s 自动刷新） |
| `sau_frontend/src/views/Dashboard.vue` | 修改 | 首页插入运行状态卡 |
| `tests/test_runtime_stability.py` | **新增** | 23 个用例 |
| `tests/conftest.py` | 修改 | 测试环境默认关闭发布预检 |

**没有改动的**：发布状态机、幂等键、风控降速、限流、凭据加密、私信同步的抓取逻辑。
这轮只在"入口"和"旁路"上加固，不动主流程。

---

## 5. 验收方式

### 5.1 自动化

```bash
cd social-auto-upload-main
python -m pytest -q                      # 582 passed
python -m pytest tests/test_runtime_stability.py -q   # 23 passed
cd sau_frontend && npm run build         # vite build ok
```

### 5.2 手工（5 分钟）

```bash
# 1) 只起 Web，不跑后台任务
set FLOWOPS_DISABLE_BACKGROUND=1   # Windows PowerShell: $env:FLOWOPS_DISABLE_BACKGROUND='1'
python sau_backend.py

# 2) 三个新端点
curl http://127.0.0.1:5409/health              # {"status":"ok"}
curl http://127.0.0.1:5409/health/ready        # ready + checks，未就绪时 503
curl http://127.0.0.1:5409/api/v2/system/health # 永远 200，ready 在 data 里
curl http://127.0.0.1:5409/api/v2/runtime/status
```

### 5.3 故障演练（验证"坏了真的能看见"）

| 演练 | 期望 |
|---|---|
| 删掉某个素材文件后再发 | 该条 item 直接 `skipped`，`error_kind=precheck_failed`，**不消耗 attempts**，任务事件里写明"素材文件不存在" |
| 把某账号的 `.enc` 换成随机字节 | 预检报 `account_cookie_undecryptable`，提示主密钥问题，而不是"请重新扫码" |
| 把代理改成 `1.2.3.4:8080` | 预检 blocking（`proxy_scheme`） |
| 手动 `kill` 掉 DM 同步线程（或 `SIGSTOP` 让心跳停） | 30s 内看门狗重启，`/api/v2/runtime/status` 里 `restarts` +1，首页卡片显示"重启 1 次" |
| 灌满磁盘 / 锁死 publish_jobs.db | `/health/ready` 返回 503，首页出现红色阻塞项 |
| 提交一个 30 分钟内没人领取的任务 | 就绪探针 `queue` 项转 warning："入队超过 10 分钟仍未被领取，发布 worker 可能已停" |

---

## 6. 下一轮建议（按 ROI）

1. **指纹 fail-closed**：`fingerprint_init_script` 目前只有 `Object.defineProperty` 包装——没有 canvas/audio/字体噪声、没有 WebRTC 策略、没有自检。补"打开 about:blank 后自检 12 项，任一失败拒绝执行该账号任务"，是从"看起来像真人"到"失败也不上网"的关键一步。这轮先把**静态自洽检查**做了（`fingerprint_consistency`），动态探针可以复用同一套断言。
2. **Executor 执行层**：现在每次发布/发信都要 `launch_persistent_context` 起一个 Chromium，单机并发 = 账号数 × 一个浏览器进程。抽成"执行器领任务 + 浏览器池 + 冷热分层"，才能把账号密度从 5-6 提到 12+。
3. **队列租约**：`_q = queue.Queue()` 仍是进程内的，多进程部署会重复消费。给 `jobs` 加 `lease_until / heartbeat_at`，把 `_worker` 改成租约抢占，是 SaaS 化的前置条件。
4. **改版韧性**：`SelectorDrift` 现在只报错没人消费。把选择器外置成可远端下发的适配包，失败自动回传截图+DOM，MTTR 才能从"天"降到"小时"。
