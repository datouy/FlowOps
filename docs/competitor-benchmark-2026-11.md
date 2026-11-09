# 云策 YunCe · 竞品对标与优化清单

> 版本：v1.0　编制：2026-11-09
> 基线：`SocialOpsHub` 当前工作树（后端根目录 23,442 行 Python / 全项目 47,225 行 / 前端 9,484 行 / 94 个后端路由 / CI 通过）
> 证据口径：① 本地一手源码；② `re/docs/蚁小二4.0-逆向分析报告.md`（商用竞品静态逆向）；③ `.codex-research/` 下 Postiz / MultiPost / GrowChief / AutoSocial / Mautic / OpenOutreach 的实仓源码与 README。文中标注"未实证"的部分不做功能断言。

---

## 0. TL;DR

**一句话：云策的功能覆盖度已经不输国内同类，真正的差距不在"功能数量"，而在三处结构性欠账——执行器形态、平台改版韧性、开放接口。**

| # | 结论 | 依据 |
|---|---|---|
| 1 | **分发是红海，私信-线索是护城河。** 竞品（蚁小二/MultiPost/Postiz）都在拼"渠道数量 + 官方 API"，这块云策追不上也不必追；但"发布 → 私信 → 意向识别 → 线索 → 跟进"的完整闭环，本轮调研的竞品里没有一个做到云策这个深度 | 蚁小二逆向只涉登录与发布，无私信/线索能力；Postiz/MultiPost 无 IM 承接 |
| 2 | **执行器形态是最贵的债。** 云策现在是"后端进程里起 Chromium"，每任务一个浏览器进程 + profile 排他锁；这决定了单机账号天花板、封号关联风险、以及能不能 SaaS 化 | `utils/account_browser.py:294` 每次 `launch_persistent_context`；对比蚁小二"单实例多 WebContentsView + 每账号独立 persist 分区" |
| 3 | **改版韧性是运营成本的大头。** 竞品用"云端下发 RPA 脚本热更新"把改版修复从"发版"降到"推配置"，云策只能 `SelectorDrift` 报错后改代码 | `utils/errors.py:118` 仅分类报错；蚁小二 `vm.runInThisContext` 热更新 RPA（逆向报告 §4.6） |

**最高杠杆的 5 个动作（按 ROI 排序）**
1. 抽 **Executor 执行层**（发布/私信/登录统一收敛成"领任务 → 执行 → 回传 stage 事件"），顺带解决"重启丢任务 + 单机密度"（§4-A1）
2. 指纹层补 **fail-closed 自检 + canvas/audio/font 噪声 + WebRTC 策略**（§4-A2，工作量 3-5 人日，直接降低关联封号）
3. 选择器外置为 **远端可下发的适配包 + 改版报告回传**（§4-B1，把 MTTR 从"天"压到"小时"）
4. 发布中心改造成 **一次创作多平台 composer**（§4-C1，用户可感知的最大体验差）
5. 开放 **REST + Webhook 事件 + CLI/MCP**（§4-E2，让 AI 工作流把云策当"发布能力"，这是 2026 的分发入口）

---

## 1. 对标地图

| 类别 | 代表产品 | 与云策的关系 | 证据强度 |
|---|---|---|---|
| **A 国内商用分发** | 蚁小二 4.0、易媒助手、融媒宝、简媒、新媒体管家、微小宝 | 直接竞争（同类客户、同价位段） | 蚁小二：**一手逆向**；其余：仅公开定位，未实证 |
| **B 浏览器扩展分发** | MultiPost（30+ 平台，Chrome/Edge 商店） | 形态竞争者——用"用户自己的浏览器"绕过风控 | 实仓源码 + CLAUDE.md |
| **C 海外开源调度** | **Postiz**（28+ 渠道、Temporal、Public API/SDK/n8n/MCP）、Mixpost | 产品形态与工程架构的教科书 | 实仓源码 + CLAUDE.md |
| **D 社媒获客自动化** | GrowChief（LinkedIn/X 外呼）、OpenOutreach、PhantomBuster/Expandi（商用） | 私信-线索板块的同构对手 | GrowChief/OpenOutreach 实仓 |
| **E 营销自动化** | Mautic（线索评分、campaign、工作流） | 线索培育与归因的方法论来源 | 实仓源码 |
| **F 本地创作者工作流** | AutoSocial Studio | 与云策"本地优先"定位最像的海外同侪 | 实仓 README + ROADMAP |
| **G 上游底座** | dreammis/social-auto-upload | 已 fork，能力天花板与社区节奏 | 本地源码 |

> 说明：易媒助手、融媒宝等国内商用产品本轮未做实证拆解，只作为"市场存在"列出；不要拿它们的宣传页当功能基线。

---

## 2. 能力矩阵

图例：✅ 有且成熟 · 🟡 有但弱 · ❌ 无 · — 不适用

| 能力维度 | 云策 YunCe | 蚁小二 4.0 | MultiPost | Postiz | GrowChief | 上游 SAU |
|---|---|---|---|---|---|---|
| 分发渠道数 | **6 已接**（+5 发布器未接：B站/YouTube/TikTok/虎扑/支付宝） | **40+ 平台配置** | **30+**（扩展注入） | **28+** | 2（LinkedIn/X） | 6-10 |
| 发布通道 | ✅ **hybrid**：录制请求模板 API 直发 + 页面发布回落 | ✅ API 腿 + RPA 腿 | 🟡 扩展 DOM 注入 | ✅ 以官方 API 为主 | 🟡 纯页面自动化 | 🟡 纯页面 |
| 账号环境隔离 | ✅ 一账号一 profile + 指纹 + 代理（`account_environment.py`） | ✅ 一账号一 `persist:auth-<id>` 分区 | — 复用用户浏览器 | 🟡 OAuth 多账号 | ✅ 代理绑定 | ❌ |
| 出口 IP 绑定 | ✅ 代理池 + 粘性检查 + 共用告警（`proxy_pool.py:156`） | ✅ 每环境 setProxy + 粘性 `sid`，**无 DIRECT 回落** | — 用户本机 IP | — | ✅ | ❌ |
| 指纹深度 | 🟡 JS 层 12 项（UA/locale/tz/screen/WebGL/hc/dm） | ✅ CDP + canvas/audio/font 噪声 + 子帧/Worker 递归 + UA 内核对齐 | — 真实浏览器 | — | ✅ Patchright | ❌ |
| 指纹 **fail-closed** | ❌ 注入失败照常执行 | ✅ 空白页校验必须返回 true，否则拒绝开环境 | — | — | 🟡 | ❌ |
| 改版韧性 | 🟡 多选择器兜底 + `SelectorDrift` 分类 + 失败截图 | ✅ **RPA 云端热更新**（改版不发版） | 🟡 扩展发版 | ✅ 官方 API 稳定 | 🟡 发版 | 🟡 发版 |
| 调度/队列 | 🟡 内存 `Queue` + DB 状态 + 本地定时循环 + 重启恢复 | ✅ 云端调度 | ❌ | ✅ **Temporal** workflow | ✅ working hours | ❌ |
| 多机/多租户 | ❌ 单进程、单库、单 Token | ✅ 云端控制面 + 多设备执行器 | — | ✅ 多 workspace + 团队 | ✅ | ❌ |
| 团队协作 | ❌ 单一 `YUNCE_ACCESS_TOKEN` | 🟡 账号归属 | ❌ | ✅ 成员/评论/内容互换 | 🟡 | ❌ |
| 审计日志 | ❌ 无（仅技术日志 `app_logging.py`） | 未实证 | — | ✅ | — | ❌ |
| 分析能力 | ✅ **4 平台回流 + daily_metrics 时序 + 漏斗 + 完成率 + 月趋势 + 周报** | 🟡 基础数据 | ❌ | ✅ analytics | 🟡 | ❌ |
| 私信/IM 承接 | ✅ **三平台统一收件箱 + 意向识别 + 线索 + 自动回复引擎 + Webhook 入站** | ❌（本次逆向范围外） | ❌ | ❌ | 🟡 外呼消息 | ❌ |
| 开放接口 | 🟡 仅入站 `/dm/inbound` | ✅ 开放平台 | ✅ REST + 扩展 API | ✅ **Public API + Node SDK + n8n + Make + MCP** | ✅ API + n8n | ❌ |
| 部署形态 | 🟡 本地 Web（start.bat）/ 服务器 Docker+systemd+Nginx | ✅ Electron 设备执行器 + 云控制台 | ✅ 浏览器扩展 | ✅ SaaS / 自托管 | ✅ SaaS/API | 🟡 本地脚本 |
| 移动端 | ❌ | 🟡 | — | 🟡 响应式 | — | ❌ |

**矩阵读出来的三件事**
1. 云策在"私信-线索"和"分析深度"两项**领先所有对标物**——这是差异化，应继续加厚而不是分散。
2. 在"渠道数、指纹深度、改版韧性、开放接口、多租户"五项上落后，且这五项**互相耦合**：没有执行器分层，多租户和改版热更新都做不了。
3. MultiPost 证明了第三条路：**不碰 Cookie、不伪造指纹，用用户自己的浏览器当执行器**——这对"封号恐惧"的客户是最强卖点，值得做成云策的一个部署模式。

---

## 3. 我方真实水位（实测，不是自评）

### 3.1 已经做对的（改造时不要动）

| 能力 | 证据 |
|---|---|
| 账号即环境：一账号一 profile + 指纹 + 代理 sidecar | `utils/account_environment.py:218`（`"一频道一出口：不继承登录会话的代理"`） |
| 指纹稳定复用，登录/改版不重生成 | `utils/account_fingerprint.py:227` `fingerprint_for_storage` |
| hybrid 双通道：首次发布录制"创建作品"请求 → 之后 API 直发，异常回落 RPA | `publish_transport.py:1-35`、`HybridPublisher` @622 |
| 发布幂等键（平台+账号+素材+标题指纹） | `publish_jobs.py:554` |
| 风控 ≠ 失效：风控只降速，不把账号踢出队列 | `publish_jobs.py:717` `_throttle_account`、`account_store.py:649` `mark_throttled` |
| 错误分层（登录失效/风控/选择器漂移/文件缺失/网络），且"不重试"语义正确 | `utils/errors.py:38,85,118` |
| 令牌桶 + 夜间降速 + 平台并发信号量 | `utils/rate_limit.py:97-230` |
| 任务事件流 + 时间轴接口 | `job_events.py:53-66`、`/api/v2/jobs/<id>/timeline` |
| 凭据 AES-256-GCM 落盘 + 全局脱敏 | `utils/credential_crypto.py`、`/api/v2/credentials/status` |
| 时序 + 漏斗 + 完成率 | `stats_reflow.py:853 monthly_trend`、`:988 conversion_funnel`、`:1112 completion_rate` |
| 反检测内核（Patchright） | `requirements.txt`、`interactive_login.py:26` |
| 工程底座：单测（56 个测试文件 / 2026-10 实测 134 passed）+ CI（pytest/ruff/vite build）+ Docker/systemd/Nginx + 备份 + 冒烟 | `.github/workflows/ci.yml`、`tests/`、`deploy/` |

### 3.2 仍然是空的（grep 0 命中，不是"计划中"）

| 项 | 证据 |
|---|---|
| 用户体系 / RBAC | `audit_logs`、`roles` 全仓 0 命中；`runtime_config.py` 单一全局 Token |
| 操作审计 | 0 命中 |
| 就绪探针 | `/health/ready` 0 命中，`/health` 恒返回 ok |
| 队列租约/心跳 | `lease_until`、`heartbeat` 0 命中；`_q = _queue.Queue()` @`publish_jobs.py:1694` |
| 版本化迁移 | 无 `migrations/`，仍是幂等 DDL + `_ensure_column` |
| 分页 | 94 个路由只有 5 处 `page_size/per_page`，23 处硬编码 `LIMIT n` |
| 开放 API/SDK/MCP | 0；`sau_cli.py` 62KB 只是内部 CLI |
| 移动端 | 侧边栏固定宽度，无响应式断点 |

### 3.3 代码体量（债的刻度）

```
dm_ops.py           132 KB      sau_backend.py      126 KB
publish_jobs.py     111 KB      interactive_login.py 74 KB
douyin_dm_sync.py    60 KB      stats_reflow.py      52 KB
前端：Messages.vue 88 KB · AccountManagement.vue 73 KB · PublishCenter.vue 56 KB
```

对比 2026-10-30 的规划文档（后端 10,978 行），**两周内后端根目录翻到 23,442 行**——增长集中在 `dm_*` 与 `publish_*`，功能在赢，结构在输。

---

## 4. 差距与优化清单

格式：**竞品怎么做 → 我方现状 → 落地动作 → 验收指标**。P0=不做就不能规模化，P1=用户可感知，P2=工程健康。

### A 组：执行器形态与账号安全（P0，最贵的债）

#### A1 把"浏览器自动化"抽成 Executor 执行层 `P0`

- **竞品**：蚁小二把账号抽象成"环境"，由**云端下发任务、本地 device worker 执行**（`socket.on('auth'|'rpa_publish')`）；Postiz 把后台作业全塞进 **Temporal orchestrator**，API 层只管收单。两者的共同点是：**控制面与执行面分离**。
- **我方现状**：`publish_jobs.py:1694` 进程内 `Queue` + 11 处裸 daemon 线程（发布 worker 池、调度循环、追踪循环、统计回流、评论同步、AI 内容包各一套）；浏览器在同一个进程里 `launch_persistent_context`（`account_browser.py:294`），每任务一个 Chromium 进程 + profile 排他锁。后果：① 重启丢 `running` 任务（靠启动时重置兜底，`publish_jobs.py:1713`）；② 单机并发 ≈ 账号数 × 一个 Chromium（按 300-500 MB/进程估算，8 GB 内存实际很难超过 5-6 个账号同时在线）；③ 无法多机、无法 SaaS。
- **落地**：
  1. 定义 Executor 协议（本地回环 HTTP/WS）：`POST /executor/claim`（带能力标签：平台/动作/账号池）、`POST /executor/{id}/events`（回传 stage）、`POST /executor/{id}/release`。
  2. `jobs` / `scheduled_items` 加 `executor_id / lease_until / heartbeat_at`，`_worker` 改成"租约抢占 + 心跳续期"，超时任务自动回 `pending`。**不需要 Redis/Celery**，SQLite 单机足够。
  3. 浏览器改为**实例池 + 冷热分层**：热账号（近 24h 有任务）常驻 context，冷账号按需拉起，空闲 10 分钟回收；并发上限做成配置项（`FLOWOPS_MAX_BROWSERS`），超出排队。
  4. 二期：执行器可注册到远程（异地机器/客户机房），控制面只发任务不收 Cookie——这一步直接打开"私有化多租户"。
- **验收**：`kill -9` 后端后重启，0 个任务卡在 `running`；单机 16 GB 稳定承载 ≥12 个账号并发；`/api/v2/executors` 能看到执行器心跳与负载。

#### A2 指纹补 fail-closed + 噪声 + WebRTC `P0`（工作量最小、收益最大）

- **竞品**：蚁小二在空白页注入后**自检**，`Runtime.evaluate` 必须返回 `true`，否则 `throw 指纹设置未能生效，已阻止环境启动`；并递归注入 iframe/Worker/ServiceWorker，`webRequest` 在指纹就绪前取消全部请求；噪声只翻最低位、seed 确定性、含 `Function.prototype.toString` 处理；`setWebRTCIPHandlingPolicy('disable_non_proxied_udp')`。
- **我方现状**：`utils/account_fingerprint.py:254 fingerprint_init_script` 只有 `Object.defineProperty` 包装（platform/hc/dm/languages/UA-CH/screen/WebGL vendor），**无 canvas、无 audio、无字体、无 WebRTC、无自检**；且 `Object.defineProperty` 的 `toString` 未处理，一探即知。
- **落地**：
  1. `fingerprint_init_script` 增补：canvas `getImageData/toDataURL/toBlob` 低位噪声（seed = `device_id`，只动不透明像素、导出走副本）、`OfflineAudioContext` 1e-8 级扰动、`measureText` 系数抖动、`WebGLRenderingContext.getParameter` 的 `UNMASKED_*` 已有则保留、`navigator.webdriver` 删除（不是置 false）。
  2. `launch_persistent_context` 增加 `--disable-features=WebRtcHideLocalIpsWithMdns` 之外，显式设置 `webrtc` 策略为 `disable_non_proxied_udp`（Playwright 用 `context.new_cdp_session` + `Emulation` 或启动参数兜底）。
  3. 新增 `utils/fingerprint_probe.py`：打开 `about:blank` 后跑 12 项断言（UA===声明、screen、canvas hash≠默认、audio hash≠默认、fonts 数量、WebGL vendor、webdriver undefined、plugins 长度、timezone、languages、RTCPeerConnection 可探测性），**任一失败 → 该账号本次任务不执行**，写 `job_events.record_error(kind='fingerprint_unavailable')`。这就是 fail-closed。
  4. **UA 与内核版本对齐**：`user_agent` 里的 `Chrome/x.y.z` 必须等于 `playwright.chromium.__version__` 的同主版本，否则平台侧一比对就露。目前 `account_fingerprint.py:120` 从 UA 反推 `chrome_major`，方向反了。
- **验收**：`/api/v2/accounts/<id>/fingerprint-probe` 返回 12 项明细；browserleaks / creepjs 上"一致性"项 ≥ 80%；同账号 30 天内指纹哈希不变。

#### A3 同账号动作全局串行 + 人类节律 `P0`

- **竞品**：GrowChief 明确"哪怕 10 个 workflow 同时触发同一账号，也 10 分钟才动一次，绝不并发"；并有 working hours 窗口。
- **我方现状**：有令牌桶（`rate_limit.py`）和夜间降速，但**没有"同一账号在任意时刻只有一个浏览器动作"的全局锁**（profile 排他锁只挡住了浏览器，挡不住 HTTP 直发腿与私信并发）。
- **落地**：`utils/account_lock.py` 提供 `with account_serial(account_id, timeout=...)`，覆盖三条腿：发布、私信发送、评论/点赞等互动；配合已有的 `set_throttle`。节律上给"动作间隔"加抖动（当前 `utils/errors.py:226` 的 retry jitter 思路可复用）。
- **验收**：压测同一账号 10 个任务并发提交，实际执行严格串行，最小间隔 ≥ 配置值。

### B 组：平台改版韧性（P0，运维成本大头）

#### B1 选择器/流程外置为"远端适配包" `P0`

- **竞品**：蚁小二把 RPA 脚本放服务端，`/api/open/verson/checkForUpdate?type=RPA` 热更新后落 `<userData>/online-srcripts-2/rpa`，`vm.runInThisContext` 执行——**平台改版当天就能推修复，不用发版**。
- **我方现状**：选择器硬编码在各 `uploader/*/main.py`；`utils/errors.py:118` 有 `SelectorDrift` 分类（做得对），但**没人消费它**——失败只是报错，改版靠人肉改代码发版。`ks_uploader/main.py:47` 已经有"失败保存截图+HTML 便于对照修选择器"的好习惯，可以升级成机制。
- **落地**：
  1. 抽 `adapters/<platform>.yaml`：每条关键步骤（上传入口、标题输入、正文输入、话题、封面、定时开关、发布按钮、验证码弹窗、成功标志）给**有序候选列表 + 断言**，代码只读配置。
  2. 版本化 + 远端拉取：`ADAPTER_BASE_URL` → 校验签名/版本 → 落本地缓存 → 与代码内置版本比对；失败自动回退内置。
  3. **改版报告回传**：`SelectorDrift` 时自动打包（平台、步骤名、页面 URL、失败选择器、DOM 快照、截图、UA/版本），一键上传或导出 zip。这是"改版当天就能修"的前提。
  4. 灰度：适配包带 `min_app_version`，避免老客户端吃新配置。
- **验收**：任选一平台改版模拟（篡改本地 DOM 结构），运维能在不改代码的情况下用适配包恢复；MTTR 从"天"降到"≤2 小时"。

#### B2 会话保鲜与返回码语义 `P1`

- **竞品**：蚁小二把 `session` cookie 改成 `Number.MAX_SAFE_INTEGER` 永不过期；`checkAccountAlive` 用 `1/0/-1/-2/-3/-4` 六个返回码区分"存活/掉登录/未知响应/无 worker/无 cookie/请求异常"，并据此把异常账号摘出队列——"用坏 Cookie 硬刷"才是最招风控的动作。
- **我方现状**：`/api/v2/accounts/<id>/check` + 12h 巡检已有；但 session cookie 保鲜未做，掉线判定粒度也没到"未知响应 vs 掉登录"。
- **落地**：① `hydrate_context` 时把 `session=True` 的 cookie 补 `expires`；② 账号检查结果统一成 6 态枚举（复用 `utils/errors.py` 语义），前端账号列表直接显示"需重扫/风控降速/代理异常/正常"；③ 连续 N 次"未知响应"自动摘出发布池并告警，不再重试。

### C 组：分发体验（P1，用户每天都能感知）

#### C1 发布中心改造成"一次创作、多平台 composer" `P1`（体验差距最大）

- **竞品**：Postiz / MultiPost 的核心体验都是**一次编辑 → 勾选多个渠道 → 每渠道独立变体（标题长度/标签数/尺寸/首图）→ 预览 → 一次提交**。
- **我方现状**：`PublishCenter.vue`（56 KB）是"单选一个平台 → 填一份表单 → 提交"，发 6 个平台要点 6 次；截图 `.demo-artifacts/07-publish-center.png` 可见 tab 名还是"发布1"、按钮文案"+添加Tao"（错字）。
- **落地**：① 内容作为一等对象（`contents` 表）→ 渠道变体（`content_variants`：platform/caption/hashtags/cover/aspect/定时）；② 顶部"渠道多选 + 每渠道一键微调（AI 按平台风格改写）"；③ 侧栏实时合规校验（已有 `compliance.py`，接进来）；④ 提交后一个 job 下 N 个 item（后端 `jobs`→`items` 结构已支持，`/publish/jobs/<id>/items` 已存在，**前端没跟上**）。
- **验收**：1 条素材 → 6 平台 → 1 次提交 → 1 个任务详情看 6 条 item 的独立状态。

#### C2 首页改成"运营驾驶舱" `P1`

- **竞品**：Postiz 首页是日历 + 待发内容 + 分析卡；蚁小二首页是账号矩阵健康总览。
- **我方现状**：`01-home.png` 只有 3 个计数卡 + 快捷入口 + 最近素材表。**"昨天涨了多少播放""几条线索没跟进""哪个账号掉线了""今天发布失败 3 条"这四件事一屏看不到**——这恰恰是运营每天早上要的。
- **落地**：首页四象限：① 今日待办（已有 `/todo/summary`，直接前端化）；② 昨日 vs 前日对比（`daily_metrics` 已有）；③ 账号健康（掉线/风控降速/代理异常）；④ 发布成功率（按平台，取自 `job_events`）。日历提到首屏。

#### C3 发布日历可拖拽 + 冲突检测 `P2`
`PublishCalendar.vue` 目前只读；`schedule_suggest.py` 已能算撞车，把它接到拖拽校验上即可。

### D 组：数据与转化（P1，业务价值最高）

#### D1 从"内容指标"升级到"线索 ROI" `P1`

- **竞品**：Mautic 的线索评分 + campaign 归因是成熟范式；Postiz 只有内容 analytics。
- **我方现状**：`stats_reflow.py` 已有 `conversion_funnel`（曝光→播放→互动→私信→线索）和 `completion_rate`，但**线索止于 CSV 导出**，没有金额、没有成本、没有归因到"哪条内容带来成交"。
- **落地**：
  1. `campaigns` 表（活动/线路/产品线）+ `contents.campaign_id`；发布时可选打 campaign。
  2. `ai_usage_log`（model/tokens/cost/scene/user）→ `cost_daily`；Playwright 与代理成本手工录入口径。
  3. 线索加 `deal_amount / closed_at / owner_id`，形成 `内容 → 线索 → 成交 → ROI` 一条链。
  4. 报表：PDF（含图）+ XLSX 多 Sheet + 定时推送到企微/飞书（`notify_ops.py` 已有通道，加个模板即可）。
- **验收**：能回答"9 月桂林线投放 3000 元 + AI 成本 200 元，带来 47 条线索、9 单、收入 5.4 万，单线索成本 68 元"。

#### D2 回流平台补齐 + Fetcher 注册化 `P2`
`stats_reflow.py:630` `FETCHERS` 只有 4 个平台，而 `uploader/` 里 B站/微博/百家号 发布器已存在——**发得出去、看不到数据**是能力浪费。抽 `Fetcher` 接口 + 注册表，先补 B站 + 微博。

### E 组：企业级地基与开放能力（P1）

#### E1 RBAC + 审计 + 就绪探针 + 版本化迁移 `P1`
- 竞品里 Postiz 有 workspace/成员/角色；云策仍是单一 `YUNCE_ACCESS_TOKEN`（`runtime_config.py:164`），删账号/导出线索 CSV/改自动回复规则**全无痕迹**。
- 落地顺序：① `users/roles/sessions/audit_logs` 四表 + `@audit("account.delete")` 装饰器（先埋点，后做 UI）；② `/health/ready`（4 库可写 + 磁盘 + AI + 有效账号数 + 后台线程 + 上次回流时间）；③ `migrations/00X_*.sql` + `migrate --status/--dry-run`。
- 注意：**个人版必须保持"零配置可用"**（单管理员 + 自动登录），别把私有化客户挡在门外。

#### E2 开放接口：REST + 事件 Webhook + CLI/MCP `P1`（战略级）

- **竞品**：Postiz 把 `Public API + Node SDK + n8n 节点 + Make 集成 + MCP Server + AI Agents CLI` 全做了；GrowChief 同样以"API + n8n 节点"为主卖点。**2026 年的内容分发入口是 AI 工作流，不是人的鼠标。**
- **我方现状**：只有入站 `/dm/inbound`；`sau_cli.py` 是内部 CLI，未对外。
- **落地**：
  1. 出站事件总线（`job.success/job.failed/account.expired/lead.created/dm.received`）+ 签名 webhook（现在 `notify_ops.py` 是静态配置，升级为可订阅）。
  2. `POST /open/v1/contents`（建内容+渠道变体）、`POST /open/v1/jobs`（提交发布）、`GET /open/v1/jobs/{id}`（含 stage 时间轴）、`GET /open/v1/leads`。API Key + 作用域 + 限流。
  3. 一个 `yunce` CLI（把 `sau_cli.py` 收敛）+ 一个 MCP server（让 Claude/Codex 直接"发一条到小红书并等结果"）。
- **验收**：外部脚本 3 行调用完成"创建→发布→拿到平台链接"；n8n/MCP 能列出渠道并提交。

### F 组：工程健康（P2）

| 项 | 现状 | 动作 |
|---|---|---|
| 巨型文件 | `dm_ops.py` 132 KB / `sau_backend.py` 126 KB / `publish_jobs.py` 111 KB | 按领域拆包：`dm/{store,sync,reply,media}.py`、`publish/{queue,executor,tracker}.py`；`sau_backend.py` 只留 app 装配 + 路由注册（蓝图拆分） |
| 巨型组件 | `Messages.vue` 88 KB、`PublishCenter.vue` 56 KB、`AccountManagement.vue` 73 KB | 拆 composables + 子组件；引入 TS（至少给 API 层加 `.d.ts`） |
| 分页 | 94 路由仅 5 处分页，23 处 `LIMIT n` | 统一 `{page,page_size,total}` + 排序 + 筛选，先做发布记录/私信/线索三处 |
| 安全 | token 可走 URL（`config.js:9-12`）、存 localStorage、无 CSP/X-Frame-Options、`/upload` 无类型尺寸白名单 | ① 去掉 URL 传 token（改用一次性 code 换 token）；② 安全响应头中间件；③ 上传白名单 + 大小限制；④ AI 接口独立配额防刷 |
| 可观测 | 技术日志有，无指标/告警 | `/metrics`（发布成功率、队列深度、执行器负载、AI 花费）+ 阈值告警到 `notify_ops` |
| 测试 | 后端 56 个测试文件（无前端测试、无 E2E） | Playwright E2E 覆盖"登录→建任务→审核→发布→看数据→回私信"主链路；前端至少给 stores/api 单测 |
| 文档 | 60+ 接口无契约 | OpenAPI 3.1（手写或 `flask-smorest`）→ `/api/docs`，前端类型由它生成 |

---

## 5. 明确不建议做的（反脆弱）

1. **不要追渠道数量**。每加一个平台 = 选择器 + 风控 + 回流 + 测试的长期负债；竞品 40+ 平台是靠专职团队养的。云策 6 平台做透 > 12 平台半残。要扩，先扩"同一平台多账号密度"。
2. **不要把"全自动真实发送私信"当卖点**。当前"草稿 + 人工确认"是风控与合规的正确姿态，保留；只给"白名单客户 + 强授权 + 限频"的显式开关。
3. **不要自建代理池**。住宅代理是采购生意，自建 IP 段只会更快被标记。做好"绑定 + 共用告警 + 成本核算"即可（`proxy_pool.py` 已够）。
4. **不要上 Kafka/Redis/Celery**。单机 SQLite + 租约调度 + 执行器分层，能撑到几十个账号；过早分布式化只会让私有化部署变难。
5. **不要照抄蚁小二的"指纹 JS 包装"而不做自检**。它自己在报告里也承认"这些是可观测的 JS 包装，不是原生级隐身"——包装 + fail-closed 才有意义，只包装等于给检测方送特征。

---

## 6. 90 天落地路线

| Sprint | 主题 | 交付 | 验收指标 |
|---|---|---|---|
| **S1（1-3 周）** | 安全与稳定 | 指纹 fail-closed + 噪声 + WebRTC + UA 对齐；同账号串行锁；`/health/ready`；token 不走 URL | 探测通过率 ≥80%；并发同账号严格串行；重启 0 卡死任务 |
| **S2（4-7 周）** | 执行器分层 | Executor 协议 + jobs 租约心跳 + 浏览器池化 + `/api/v2/executors` | 16 GB 单机 ≥12 账号并发；执行器掉线任务自动回收 |
| **S3（8-10 周）** | 改版韧性 | 适配包 YAML 外置 + 远端拉取 + SelectorDrift 自动回传 | 模拟改版不改代码恢复；MTTR ≤2h |
| **S4（11-13 周）** | 体验与开放 | 多平台 composer；首页驾驶舱；`/open/v1/*` + 事件 webhook + CLI/MCP | 6 平台 1 次提交；外部脚本 3 行打通；n8n 节点可用 |

**贯穿项（每周 20% 工时）**：RBAC 埋点 + 审计表 → 分页统一 → 单体拆分（先 `dm_ops.py`）。

---

## 7. 证据索引

| 结论 | 证据 |
|---|---|
| 指纹仅 JS 层、无自检 | `utils/account_fingerprint.py:254-320`（`Object.defineProperty` 包装，无 canvas/audio/自检） |
| 每次任务起一个 Chromium | `utils/account_browser.py:294` `launch_persistent_context` |
| 内存队列 | `publish_jobs.py:1694 _q = _queue.Queue()`；`_worker` @1798；`_schedule_loop` @2503 |
| 风控只降速不判死 | `publish_jobs.py:717`、`account_store.py:649`、`utils/rate_limit.py:284-315` |
| 错误分层 | `utils/errors.py:38,85,118` |
| hybrid 双通道 | `publish_transport.py:1-35`、`:622 HybridPublisher` |
| 代理共用告警 | `utils/proxy_pool.py:156` |
| 一频道一出口 | `utils/account_environment.py:218-260` |
| 时序/漏斗/完成率 | `stats_reflow.py:853,988,1112` |
| Fetcher 只覆盖 4 平台 | `stats_reflow.py:630` |
| 无 RBAC/审计/租约/迁移/就绪探针 | 全仓 grep 0 命中 |
| 分页缺失 | 94 路由 / 5 处分页 / 23 处 `LIMIT n` |
| 蚁小二指纹 fail-closed 与热更新 | `re/docs/蚁小二4.0-逆向分析报告.md` §4.6、§5.2、§5.5 |
| Postiz Temporal + Public API/SDK/MCP | `.codex-research/.../postiz/CLAUDE.md`、`postiz/README.md:39-86` |
| MultiPost 30+ 平台扩展注入 | `.codex-research/.../multipost/CLAUDE.md`、`multipost/README.md:23` |
| GrowChief 同账号串行 + working hours | `.codex-research/.../growchief/README.md:55-70` |
