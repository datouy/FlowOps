# 云策 YunCe — 产品规划与改造方案 V2

> 文档状态：**待评审**（评审通过后按本文档排期实施）
> 版本：v2.0　编制日期：2026-10-30
> 目标读者：业务负责人 / 产品负责人 / 研发
> 定位基线：**企业级优先，个人级兜底**

---

## 0. 摘要（TL;DR）

云策当前已经是一个**功能可用、能跑通真实链路**的运营工作台，不是 Demo。但它现在还是「**一个多账号自动化脚本 + Web 界面**」，距离「**企业级内容运营中台**」还差三块地基：

| 地基 | 当前状态 | 企业级要求 | 差距 |
|---|---|---|---|
| **① 身份与权限** | 单一全局 Token，无用户体系 | 多用户 / 角色 / 数据隔离 / 审计 | **P0 阻塞级** |
| **② 稳定与可观测** | 内存队列、进程重启丢任务、无指标 | 持久化调度 / 指标 / 告警 / 健康探针 | **P0 阻塞级** |
| **③ 流量与转化分析** | 只有「最新快照」聚合，无时序、无归因、无 ROI | 趋势 / 漏斗 / 归因 / 成本核算 / 报表 | **P1 核心缺口** |

本方案提出 **4 个批次、28 项改造**，按 P0→P3 排期。其中 P0 为「不做就不能对外给团队用」，P1 为「用户明确提出的统计流量核心诉求」，P2 为「内容—线索—成交闭环」，P3 为「体验与平台扩展」。

---

## 1. 现状盘点（已实测验证）

### 1.1 实测结论

| 项 | 结果 |
|---|---|
| 单元测试 | `pytest tests -q` → **134 passed in 4.77s** |
| 前端构建 | `vite build` → **成功**，产物 1.86 MB（gzip 515 KB） |
| 只读接口 | `/health`、`/getAccounts`、`/getFiles`、`/jobs`、`/publish-records`、`/account-status`、`/stats/summary`、`/stats/status`、`/ai/status`、`/todo/summary`、`/report/weekly`、`/dm/analytics`、`/dm/conversations`、`/dm/rules`、`/dm/settings`、`/dm/export`、`/dm/priority-leads`、`/dm/storage/health`、`/schedule/week`、`/schedule/suggest`、`/material-status`、`/stats/unmatched`、`/materials/duplicates`、`/materials/unused`、`/notify/status`、`/review/pending` → **全部 200 且结构正确** |
| 写操作 | `/ai/generate` → **200**，返回完整小红书文案（标题/正文/话题/发布建议）；`/dm/simulate` → **200**，正确识别 `intent=high`、抽出 `travel_date=国庆`、`traveler_count` 字段 |
| 校验规则 | 绝对化用语、手机号、联系方式、标题字数、话题数量、素材存在性、重复标题 → **均已覆盖** |
| 数据回流 | 抖音/小红书/视频号/快手 4 平台抓取正常，本轮 `fetched=2 saved=2 matched=0`，账号隔离逻辑生效（`filter_current_account_*`） |

**结论：核心链路是通的，工程质量（测试覆盖、连接托管、WAL 落盘、请求日志、Token 守卫）明显高于同类开源项目。** 下面的问题不是「能不能跑」，而是「能不能上企业级、能不能规模化运营」。

### 1.2 功能全景

```
账号管理 ── 扫码/密码/短信登录 · Cookie 导入导出 · 12h 巡检 · 过期告警
   │
内容生产 ── AI 文案(6平台风格) · 多平台内容包 · 自动封面(3:4/9:16) · 合规检查
   │
发布调度 ── 后台队列 · 失败重试1次 · 发布记录 · 平台侧定时 · 发布日历 · 排期建议
   │        └─ 审核中心（提交→审核→发布）· 验证码二次验证 · 钉钉/飞书/企微告警
   │
私域承接 ── 三平台私信只读同步 · 统一收件箱 · 意向识别 · 规则引擎 · AI自动回复
   │        └─ 线索沉淀(9字段) · 阶段流转 · 优先跟进 · CSV导出 · Webhook接入
   │
数据复盘 ── 4平台回流 · 平台/账号/话题聚合 · 运营周报 · 未匹配内容手工关联
```

### 1.3 代码资产

- 后端 Python：**10,978 行**（18 个顶层模块）
- 前端 Vue3：**13 个视图 / 6,815 行**
- 数据库：**4 个独立 SQLite 库**（`database.db` 账号素材 / `publish_jobs.db` 任务发布 / `ops_center.db` 消息线索 / `ai_pack_jobs.db` 内容包），共 16 张业务表
- 部署：Docker Compose + 裸机 systemd + Nginx + 备份脚本 + 冒烟脚本，**生产已跑在 `106.55.225.14:81/flowops/`**

---

## 2. 缺口分析（按严重度）

### 2.1 P0 — 企业级阻塞项

#### P0-1 无用户体系与权限模型 🔴 **最高优先**

现状：`runtime_config.py` 只有一个全局 `YUNCE_ACCESS_TOKEN`，所有请求共用；谁能拿到 Token 就能操作**全部账号的 Cookie、全部私信、全部发布**。

```164:186:E:\project\Dtou\flowOps\social-auto-upload-main\runtime_config.py
def access_allowed(method: str, path: str, headers, args) -> bool:
    """判断一次请求是否可以通过可选的访问令牌校验。"""
    if not ACCESS_TOKEN:
        return True
    if str(method or "").upper() == "OPTIONS" or is_public_path(path):
        return True
    return token_ok(extract_token(headers, args))
```

企业场景致命问题：
- 运营总监、投放、客服**共用一个密码**，无法追责、无法踢人、无法分权
- 没有任何**操作审计**，谁删了账号、谁发了私信、谁改了自动回复规则，查不到
- 数据**无法隔离**，代理商 A 能看到代理商 B 的客户线索

> **改造**：新增 `users / roles / user_roles / sessions / audit_logs` 五表 + JWT 会话 + RBAC 权限点 + 账号/线索级数据范围（`scope=all|assigned`）+ 全量审计埋点。个人使用时降级为「单管理员 + 自动登录」，零配置可用。

#### P0-2 任务队列在内存，重启丢运行中任务 🟠

```118:121:E:\project\Dtou\flowOps\social-auto-upload-main\publish_jobs.py
def _worker(worker_id):
    while True:
        job_id = _q.get()
        try:
            _run_job(job_id)
```

`_q = queue.Queue()` 是**进程内**的。`recover_pending_jobs()` 只捞 `status='pending'`，**`status='running'` 的任务在重启后永久卡死**——前端永远看到「发布中」，既不失败也不重试。

同理 `ai_pack_jobs` 的后台线程、`douyin_dm_sync` / `xiaohongshu_dm_sync` / `channels_dm_sync` 的同步轮询，**全部是裸 daemon thread**，无心跳、无看门狗、无崩溃自愈。

> **改造**：`jobs` 表增加 `heartbeat_at / worker_id / lease_until`，改为 **DB 租约 + 定时扫描** 的调度模型（无需引入 Redis/Celery，保持单机可跑）；进程启动时把 `running` 且心跳超时的任务重置为 `pending`；所有后台循环统一收敛到一个 `Scheduler` 组件，带 watchdog 与指数退避。

#### P0-3 无本地调度引擎，「定时发布」实际是平台侧定时 🟠

`scheduled_items` 表记录了每条排期（账号 × 素材 × 时间），**但代码里没有任何一处按 `scheduled_at` 触发**。定时逻辑全部下沉给了各平台的「定时发布」功能（`post_video_*` 的 `enableTimer` 参数）。

后果：
- 平台定时功能被风控/改版即失效，且**我们不知道哪些真的发出去了**（`scheduled_items.status` 只在 `success` 时批量更新）
- 发布日历是「计划表」，不是「真实执行记录」
- 无法实现「发布前 30 分钟自动生成封面/检查素材」这类**前置编排**

> **改造**：引入本地调度器，以 `scheduled_items` 为唯一事实来源，提前 `trigger_ahead_minutes` 唤醒 → 复核素材/合规/账号有效性 → 执行 → 回写 `actual_published_at / real_status`。平台侧定时降级为「平台不支持时的兜底」。

#### P0-4 无审计日志、无操作留痕 🟠

目前只有 `app_logging.py` 的**技术日志**（request_id + 耗时），**没有业务审计**。删账号、删素材、导出线索 CSV、批量发私信、改自动回复规则——全部无痕。

> **改造**：`audit_logs` 表 + `@audit("account.delete")` 装饰器，记录 `actor / action / target / before / after / ip / ua / ts`，前端提供「操作审计」页，支持筛选与导出。合规场景下这是硬要求。

#### P0-5 健康检查过于表面，无依赖探针 🟡

```90:94:E:\project\Dtou\flowOps\social-auto-upload-main\sau_backend.py
@app.route('/health', methods=['GET'])
def health():
    return jsonify({'code': 200, 'data': {'status': 'ok'}, 'msg': None}), 200
```

无论 SQLite 是否损坏、磁盘是否写满、AI 是否可用、Cookie 是否有几个有效，都返回 `ok`。Docker healthcheck 形同虚设。

> **改造**：`/health`（存活）与 `/health/ready`（就绪）分离，就绪项检查：4 个 DB 可写 + 磁盘剩余空间 > 阈值 + AI 配置 + 有效账号数 + 后台线程存活 + 上次回流时间。

#### P0-6 缺少限流、CSRF 与安全响应头 🟡

无请求频率限制（`/ai/generate` 一次调用 = 一次真金白银的模型调用）；无 CSRF 防护（Token 走 `localStorage` + Header 尚可，但若将来启用 Cookie 会话即刻失守）；无 `X-Frame-Options` / `CSP` 等安全头；`/upload` 未做类型与尺寸白名单校验。

> **改造**：按用户/IP 的令牌桶限流（AI 接口单独配额）、安全响应头中间件、上传文件类型白名单 + 尺寸 + 病毒扫描预留。

#### P0-7 生产数据库与开发库同构，无迁移工具链 🟡

虽有 `schema_migrations` 表和 `apply_migrations()`，但各模块 `init_db()` 用的是 **幂等 DDL + `_ensure_column` 手工 ALTER**，无版本化迁移脚本、无回滚、无 dry-run。生产库一旦结构漂移无法定版。

> **改造**：迁移脚本化（`migrations/00X_xxx.sql` + `alembic` 或自研 runner）、`migrate --status / --up / --dry-run`、启动时校验版本并拒绝「代码新于库旧」。

---

### 2.2 P1 — 流量统计与数据分析（用户核心诉求）

这是用户明确提出的「**统计流量**」，也是当前**最不足以支撑运营决策**的部分。

#### P1-1 只有「最新快照」，没有时序 🟠 **最关键**

```776:786:E:\project\Dtou\flowOps\social-auto-upload-main\stats_reflow.py
        rows = [dict(r) for r in c.execute('''
            SELECT s.* FROM content_stats s
            INNER JOIN (
                SELECT content_key, MAX(id) AS max_id
                FROM content_stats
                WHERE content_key IS NOT NULL AND content_key != ''
                GROUP BY content_key
            ) latest ON latest.max_id = s.id
            ORDER BY s.id DESC
        ''').fetchall()]
```

`content_stats` 表**每次回流都 INSERT 一条新快照**（数据是好的），但**所有查询层一律只取 `MAX(id)` 的最新一条**。结果是：

- ❌ 无法回答「这条视频发布后第 1/3/7 天涨了多少粉」
- ❌ 无法回答「上个月 vs 这个月，整体流量涨了还是跌了」
- ❌ 无法画任何趋势曲线
- ❌ 无法计算「发布后 48 小时增速」这类爆款判定指标
- ⚠️ `weekly_report` 的「近 7 天播放」其实是**存量最新快照的求和**，不是「近 7 天产生的播放量」，**数字口径是错的**

> **改造**：
> 1. 新增 **`daily_metrics` 宽表**：`date × platform × account × content_key → plays/likes/comments/collects/shares/followers/increments`，每日回流入库后做一次增量聚合（快照差分）
> 2. 新增 **时序查询 API**：`/stats/trend?metric=plays&granularity=day|week|month&from=&to=&group_by=platform|account|topic`
> 3. 新增 **单内容生命周期 API**：`/stats/content/{key}/timeline` 返回该内容从发布到今天的完整曲线与增长斜率
> 4. 修正 `weekly_report` 口径，区分 **「期间新增播放量」**（增量）与 **「期间快照总量」**（存量），并在 UI 上明确标注

#### P1-2 无流量归因：不知道流量从哪来 🟠

`content_stats` 有 `topic_totals`（从标题 `#话题` 解析），但这**不是归因**。无法回答：
- 「桂林线路」和「阳朔线路」哪条线带来的线索更多？
- 哪个 KOL/哪个账号的流量转化最好？
- 付费投放 vs 自然流量的占比？

> **改造**：
> 1. **`campaigns`（营销活动/内容计划）表** + `contents.campaign_id`，让每条内容归属到某个活动/主题/产品线
> 2. **`utm`/来源标记**：发布时给标题带隐藏话题码或使用平台专属归因参数，回流时解析归因
> 3. **归因漏斗视图**：`曝光 → 播放 → 互动 → 私信 → 线索 → 成交`，逐层转化率 + 逐平台对比
> 4. **内容维度 ROI**：`content_id → 产生线索数 → 标记收入`（可手工录入或对接 CRM）

#### P1-3 无成本核算，无法算 ROI 🟠

系统会持续消耗 AI Token、Playwright 资源、账号风控额度，但**没有任何成本记录**。运营无法回答「这个月 AI 花了多少」「一条线索的获取成本是多少」。

> **改造**：
> 1. **`ai_usage_log` 表**：每次 LLM 调用记录 `model / prompt_tokens / completion_tokens / cost / scene(文案生成|AI回复|内容包) / user_id`
> 2. **`cost_daily` 聚合**：按天/按场景/按用户统计
> 3. **ROI 面板**：`线索数 / 投放成本 / AI成本 / 单线索成本 / 单千次播放成本`
> 4. AI 预算与配额：日/月上限、超额告警、按用户配额（与 `dm_settings.ai_max_per_day` 现有机制合并）

#### P1-4 分析维度太窄，运营不够用 🟡

现有维度：**平台 / 账号 / 话题**。缺：
- **时间维度**（趋势、日/周/月、同比环比）
- **内容类型维度**（视频 vs 图文 vs 短文案 vs 长文案）
- **发布形式维度**（立即发布 vs 定时发布；有无封面；带商品链接）
- **漏斗维度**（互动→私信转化率）
- **对比维度**（平台横向对比、账号横向对比、话题 Top/Bottom）

> **改造**：分析维度建模为可配置维度（`analytics_dimensions`），提供「下钻 → 上卷」的分析器。

#### P1-5 缺少报表输出能力 🟡

周报只有纯文本 + CSV，**无 PDF / Excel（XLSX）/ 定时推送**。运营要发给老板，只能截图。

> **改造**：周报一键导出 **PDF（可读版式，含图表）** 与 **XLSX（多 Sheet：总览/平台/账号/内容/线索）**；支持**定时周报推送**到企微/飞书/邮件；报表模板可配置（哪些指标、哪个周期、发给谁）。

#### P1-6 数据回流覆盖不全 🟡

`FETCHERS` 只覆盖 **小红书/视频号/抖音/快手 4 个**，`微博/百家号` 无回流。但 `uploader/` 目录里**已经有** `bilibili_uploader / youtube_uploader / hupu_uploader / tk_uploader / alipay_uploader` 五个未接线的发布器——发布能力有、数据分析却接不上，属于能力浪费。

> **改造**：抽象 `Fetcher` 接口统一注册，新增平台只需实现一个类；优先补 **B站 + 微博**（粉丝价值高）。

---

### 2.3 P2 — 内容与线索闭环

| 编号 | 缺口 | 现状 | 改造方向 |
|---|---|---|---|
| P2-1 | **线索无跟进节奏** | `dm_contacts` 有 `stage`，但**没有 `next_follow_at`、没有跟进提醒、没有跟进历史** | 加 `follow_up_at / follow_up_history`，到期进今日待办，支持「超期未跟进」预警 |
| P2-2 | **线索无归属与协作** | 线索不归属任何员工，多人协作会撞单 | `owner_id / assigned_at`，认领、协作备注、撞单检测 |
| P2-3 | **审核流过轻** | 只有「通过/驳回」，**无审核人、无意见、无多级、无版本对比** | `reviews` 表记录审核人/意见/时间；支持多级审批；稿件留版本快照 |
| P2-4 | **无内容库/选题库** | AI 生成完即用，没有沉淀 | 新增「选题库 + 内容资产库」：把优秀内容、爆款模板、复用脚本沉淀下来 |
| P2-5 | **无账号分组与角色化视角** | 账号是平铺列表 | 按品牌/地区/线路分组，账号打标签，支持「只看我的号」 |
| P2-6 | **线索与内容未打通** | 无法知道「这条私信是看了哪条视频来的」 | `dm_contacts.source_content` 存的是文案，升级为**内容 ID 关联 + 素材 ID 关联** |
| P2-7 | **私信只读同步是「半自动」** | 自动回复生成草稿，需人工发送；工作台仅支持文本 | 打通真正的自动发送（受控开关 + 限频 + 黑名单）；扩展图片/表情/商品卡片 |
| P2-8 | **无客户侧 CRM** | 线索止于「导出 CSV」 | 轻量 CRM：客户档案、订单、跟进漏斗、与成单金额打通（与 P1-2 ROI 联动） |

---

### 2.4 P3 — 工程与体验

| 编号 | 缺口 | 影响 | 改造方向 |
|---|---|---|---|
| P3-1 | **前端巨型组件** | `Messages.vue` 1477 行、`PublishCenter.vue` 1671 行、`AccountManagement.vue` 1360 行，改一处容易崩一片 | 按领域拆分 composables + 子组件；引入 `TypeScript` |
| P3-2 | **无分页** | 所有列表都是 `LIMIT 50/100` 硬上限，数据涨到几千条后无法用 | 统一分页协议 `{page,page_size,total}` + 排序 + 筛选 |
| P3-3 | **平台字典重复 3 处** | `NAMES = {1:小红书…}` 在 `ops_center.py`、`publish_jobs.py`、`stats_reflow.py` 各写一遍，改名必漏 | 统一收敛到 `myUtils/platform_registry.py`（该文件已有 `PLATFORM_META`，未被全面使用） |
| P3-4 | **硬编码路径** | `Path(BASE_DIR)/"videoFile"` 散落各处 | 统一 `paths.py` 常量层 |
| P3-5 | **无 OpenAPI 文档** | 60+ 接口无契约，联调靠读源码 | 接入 `flask-smorest` 或手写 OpenAPI 3.1，产出 `/api/docs` |
| P3-6 | **无 E2E 测试** | 只有 134 个后端单测，前端 0 测试 | Playwright 覆盖「登录→建任务→审核→发布→看数据→回复私信」主链路 |
| P3-7 | **无 i18n** | 有 `README.en.md`，但 UI 全中文硬编码 | Vue-i18n，中英双语（若面向海外客户） |
| P3-8 | **无移动端/响应式** | 侧边栏固定 220px，手机端不可用 | 响应式布局 + 移动端精简视图（审批、待办、私信） |
| P3-9 | **发布日历只读** | 不能在日历上拖拽改期 | 日历支持拖拽改期、批量改期、冲突检测 |
| P3-10 | **无全局搜索** | 找一条历史内容要一个个页面翻 | 全局搜索：内容 / 素材 / 线索 / 消息 / 任务 |
| P3-11 | **发布无幂等与去重护栏** | 同一素材可能重复发出 | 发布前 MD5+标题双去重、`dry-run` 预演 |
| P3-12 | **无消息队列/事件总线** | 只有静态 webhook 配置，无事件订阅 | 内部事件总线 + 出站 Webhook（发布成功/失败/线索产生/账号过期） |

---

## 3. 目标架构（改造后）

```
                    ┌──────────────────────────────────────────┐
 浏览器 (Vue3)      │  网关 / 中间件                            │
  ├ 登录页 + 会话   │  · JWT 鉴权 + RBAC 权限点                 │
  ├ 运营工作台     │  · 限流 (令牌桶) + 安全响应头              │
  ├ 数据分析中心   │  · 审计埋点 (@audit)                      │
  ├ 审核中心       │  · 请求日志 (request_id)                  │
  └ 操作审计       └────────────────┬─────────────────────────┘
                                     │
              ┌──────────────────────┼───────────────────────┐
              ▼                      ▼                       ▼
     ┌─────────────────┐   ┌──────────────────┐   ┌──────────────────┐
     │ 领域服务层       │   │  调度与执行层      │   │  数据分析层        │
     │ · accounts      │   │  · Scheduler      │   │  · daily_metrics  │
     │ · contents      │   │    (DB 租约+扫描)  │   │  · campaigns      │
     │ · campaigns     │   │  · JobWorker      │   │  · attribution    │
     │ · leads         │   │    (租约+心跳)    │   │  · ai_usage_log   │
     │ · reviews       │   │  · Watchdog       │   │  · cost_daily     │
     │ · rules         │   │  · 定时发布执行    │   │  · funnel         │
     │ · users/RBAC    │   │  · 私信同步轮询    │   │  · report builder │
     └────────┬────────┘   └────────┬─────────┘   └────────┬─────────┘
              │                     │                      │
     ┌────────▼────────┐   ┌────────▼─────────┐   ┌────────▼─────────┐
     │ 平台适配器层      │   │  数据层            │   │  数据层            │
     │ PlatformRegistry│   │  SQLite(WAL)      │   │  SQLite / 可选 PG  │
     │ 6 平台发布        │   │  · 4 库 → 统一视图 │   │  聚合表 + 索引     │
     │ 4 平台回流        │   │  · 版本化迁移      │   │  定时聚合任务      │
     │ 3 平台私信        │   │  · 备份/恢复       │   │  物化视图          │
     └─────────────────┘   └──────────────────┘   └──────────────────┘
```

**技术选型原则：保持单机可跑、不强依赖外部中间件。**
- 不引入 Redis/Celery（当前部署是单机 Docker，用户规模未到瓶颈），用 **SQLite + DB 租约** 实现可靠调度
- 不引入 Postgres（数据量在千万行以内 SQLite 完全够用），但**表结构与查询层按可迁移设计**
- 保留 MySQL/PG 迁移路径：所有 SQL 收敛到 `repositories/` 层，不在路由里写裸 SQL

---

## 4. 数据模型新增（DDL 草案）

```sql
-- ===== P0 身份与审计 =====
CREATE TABLE users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL UNIQUE,
  display_name TEXT NOT NULL,
  password_hash TEXT NOT NULL,           -- PBKDF2-SHA256
  role TEXT NOT NULL DEFAULT 'operator', -- admin/manager/operator/viewer
  status TEXT NOT NULL DEFAULT 'active', -- active/disabled
  data_scope TEXT NOT NULL DEFAULT 'all',-- all/assigned
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  last_login_at DATETIME
);

CREATE TABLE audit_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  actor_id INTEGER, actor_name TEXT,
  action TEXT NOT NULL,                  -- account.delete / lead.export / publish.approve
  target_type TEXT, target_id TEXT,
  summary TEXT, before_json TEXT, after_json TEXT,
  ip TEXT, user_agent TEXT,
  request_id TEXT,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_audit_created ON audit_logs(created_at DESC);
CREATE INDEX idx_audit_actor ON audit_logs(actor_id, created_at DESC);

-- ===== P1 流量与成本 =====
CREATE TABLE daily_metrics (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  stat_date TEXT NOT NULL,               -- YYYY-MM-DD
  platform INTEGER NOT NULL,
  account TEXT NOT NULL DEFAULT '',
  content_key TEXT NOT NULL DEFAULT '',  -- 空=账号级汇总
  campaign_id INTEGER,
  plays INTEGER DEFAULT 0, likes INTEGER DEFAULT 0, comments INTEGER DEFAULT 0,
  collects INTEGER DEFAULT 0, shares INTEGER DEFAULT 0, followers INTEGER DEFAULT 0,
  plays_inc INTEGER DEFAULT 0, likes_inc INTEGER DEFAULT 0,   -- 当日增量
  comments_inc INTEGER DEFAULT 0, collects_inc INTEGER DEFAULT 0,
  shares_inc INTEGER DEFAULT 0, followers_inc INTEGER DEFAULT 0,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(stat_date, platform, account, content_key)
);
CREATE INDEX idx_dm_trend ON daily_metrics(stat_date, platform);
CREATE INDEX idx_dm_content ON daily_metrics(content_key, stat_date);

CREATE TABLE campaigns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL, product_line TEXT DEFAULT '', objective TEXT DEFAULT '',
  start_date TEXT, end_date TEXT, budget REAL DEFAULT 0,
  target_leads INTEGER DEFAULT 0, owner_id INTEGER,
  status TEXT DEFAULT 'active', created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE ai_usage_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scene TEXT NOT NULL,                   -- content_copy / dm_reply / content_pack / ai_draft
  user_id INTEGER, platform TEXT DEFAULT '',
  model TEXT DEFAULT '', prompt_tokens INTEGER DEFAULT 0,
  completion_tokens INTEGER DEFAULT 0, cost REAL DEFAULT 0,
  duration_ms INTEGER DEFAULT 0, success INTEGER DEFAULT 1,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_ai_usage_day ON ai_usage_log(created_at, scene);

-- ===== P2 线索闭环 =====
ALTER TABLE dm_contacts ADD COLUMN owner_id INTEGER;
ALTER TABLE dm_contacts ADD COLUMN follow_up_at DATETIME;
ALTER TABLE dm_contacts ADD COLUMN source_content_id INTEGER;   -- 关联发布内容
ALTER TABLE dm_contacts ADD COLUMN revenue REAL DEFAULT 0;      -- 成单金额(可手工/CRM录入)

CREATE TABLE follow_up_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  contact_id INTEGER NOT NULL, operator_id INTEGER,
  action TEXT NOT NULL, note TEXT DEFAULT '',
  next_follow_at DATETIME, created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- ===== P0 可靠调度 =====
ALTER TABLE jobs ADD COLUMN heartbeat_at DATETIME;
ALTER TABLE jobs ADD COLUMN worker_id TEXT DEFAULT '';
ALTER TABLE jobs ADD COLUMN lease_until DATETIME;
ALTER TABLE jobs ADD COLUMN scheduled_at DATETIME;      -- 本地调度触发时间
ALTER TABLE scheduled_items ADD COLUMN real_status TEXT DEFAULT 'planned';
ALTER TABLE scheduled_items ADD COLUMN actual_published_at DATETIME;
ALTER TABLE scheduled_items ADD COLUMN error TEXT DEFAULT '';
```

---

## 5. 新增 API 契约（草案）

### 5.1 身份与权限（P0）
```
POST   /api/auth/login            {username, password} → {token, refresh, user}
POST   /api/auth/logout
POST   /api/auth/refresh
GET    /api/auth/me               → 当前用户 + 权限点 + 数据范围
GET    /api/users                 列表（admin）
POST   /api/users                 创建
PUT    /api/users/{id}            更新（角色/状态/数据范围）
DELETE /api/users/{id}
GET    /api/roles                 角色与权限点
```
**权限点设计**：`account.view/manage`, `material.view/manage/delete`, `publish.create/approve/cancel`,
`lead.view/manage/export`, `rule.manage`, `analytics.view/export`, `audit.view`, `user.manage`, `settings.manage`。

### 5.2 流量分析（P1）— 用户核心诉求
```
GET /api/stats/trend
    ?metric=plays|likes|comments|collects|shares|followers
    &granularity=day|week|month
    &from=2026-09-01&to=2026-10-30
    &group_by=platform|account|topic|content_type|campaign
    &platform=&account=&campaign_id=&topic=
    → {series:[{bucket, platform, value, value_inc}], totals:{...}, compare:{prev_period, delta_pct}}

GET /api/stats/content/{content_key}/timeline
    → {content:{...}, timeline:[{date, plays, likes, followers, plays_inc}],
       slope, velocity, peak_date, best_hour}

GET /api/stats/funnel
    ?from=&to=&group_by=platform|account|campaign
    → {stages:[{stage:impression|play|interact|dm|lead|deal, value, conv_rate}],
       by_platform:[...], by_campaign:[...]}

GET /api/stats/roi
    ?from=&to=
    → {ai_cost, ad_cost, total_cost, leads, deals, revenue,
       cost_per_lead, revenue_per_lead, roi, roas}

GET /api/stats/attribution
    ?dim=topic|campaign|content_type|account
    → [{key, contents, plays, leads, lead_rate, revenue, cpa}]

GET /api/stats/content-type           内容类型维度分布
GET /api/ai/usage                    AI 用量与成本明细
GET /api/ai/usage/summary            按天/场景/用户聚合
```

### 5.3 报表（P1）
```
GET  /api/report/weekly?days=7        增强：区分 存量/增量 + 同比环比 + 漏斗 + ROI
GET  /api/report/export?type=pdf|xlsx&days=7&template=default
POST /api/report/schedule             定时推送配置
```

### 5.4 调度与运维（P0）
```
GET  /api/jobs?page=&page_size=&status=&platform=&from=&to=
GET  /api/jobs/{id}/events            任务执行事件流（替代裸 progress 字段）
POST /api/jobs/{id}/cancel            取消排队中任务
POST /api/scheduler/run-once          手动触发一轮调度扫描
GET  /api/health/ready                深度就绪探针
GET  /api/metrics                     Prometheus 格式指标
GET  /api/audit                       操作审计查询
GET  /api/audit/export
```

### 5.5 线索闭环（P2）
```
GET  /api/leads?page=&page_size=&stage=&intent=&owner_id=&follow_up=due
PUT  /api/leads/{id}                  更新阶段/负责人/跟进时间/成交额
POST /api/leads/{id}/claim            认领
POST /api/leads/{id}/follow-up        记录跟进
GET  /api/leads/{id}/timeline         跟进 + 会话 + 来源内容
```

---

## 6. 前端信息架构（改造后）

```
侧边栏（重组）
├─ 工作台
│   ├─ 首页总览          今日待办 / 核心指标卡 / 异常告警
│   └─ 今日待办          线索、审核、发布、账号、素材，一屏收敛
├─ 内容
│   ├─ 选题与活动        campaigns + 选题库
│   ├─ AI 文案
│   ├─ 内容包
│   ├─ 合规检查
│   └─ 素材库
├─ 发布
│   ├─ 发布中心
│   ├─ 发布日历          支持拖拽改期
│   ├─ 审核中心          多级 + 意见 + 版本
│   └─ 发布记录          分页 + 筛选 + 重试
├─ 私域
│   ├─ 私信中心
│   └─ 线索 CRM          漏斗看板 + 跟进任务 + 成交
├─ 分析                 ← 本次重点新增
│   ├─ 流量总览          趋势线 / 平台对比 / 漏斗 / ROI 四象限
│   ├─ 内容表现          Top/Bottom + 单内容生命周期曲线
│   ├─ 渠道归因          话题 / 活动 / 内容类型
│   ├─ 成本与 ROI        AI 成本 + 单线索成本 + 回本周期
│   └─ 运营周报          预览 / PDF / XLSX / 定时推送
├─ 账号
│   ├─ 账号管理
│   └─ 分组与标签
└─ 系统
    ├─ 自动回复规则
    ├─ 通知配置
    ├─ 团队与权限        ← 新增
    ├─ 操作审计          ← 新增
    └─ 系统设置 / 备份恢复
```

### 6.1 「流量总览」页面线框（重点）

```
┌────────────────────────────────────────────────────────────────────┐
│ 流量总览   [日期: 近30天 ▾][平台: 全部 ▾][账号: 全部 ▾][活动: ▾]  │
├────────────────────────────────────────────────────────────────────┤
│ ┌──────────┐┌──────────┐┌──────────┐┌──────────┐┌──────────────┐ │
│ │总播放 12.4万││互动 8,932││私信 421 ││线索 138 ││单线索成本 ¥2.1│ │
│ │▲ 23% 环比 ││▲ 8%      ││▲ 31%    ││▲ 12%    ││▼ 0.4        │ │
│ └──────────┘└──────────┘└──────────┘└──────────┘└──────────────┘ │
├────────────────────────────────────────────────────────────────────┤
│  流量趋势（播放 / 互动 / 线索，可切换）                              │
│  ▁▂▃▅▇▆▅▃▂▁▂▄▆▇▇▆▅▃▂▁▂▃▅▇▇█▇▅▃▂▁  ← 日粒度，可切周/月          │
├───────────────────────────────┬────────────────────────────────────┤
│ 转化漏斗                       │ 平台对比（横向条形）                 │
│ 曝光 100万 ████████            │ 小红书 ████████ 播放 8.2万 线索 62  │
│ 播放  12.4万 ███              │ 抖音   ██████   播放 3.1万 线索 45  │
│ 互动  8,932  █               │ 视频号 ████     播放 1.1万 线索 31  │
│ 私信  421     ▌               │                                     │
│ 线索  138     ▏               │ 内容类型分布 / 归因 Top             │
│ 成交  31      ▏               │ 线路口播 ████████ 线索 48  ROI 3.2  │
└───────────────────────────────┴────────────────────────────────────┘
```

---

## 7. 实施排期（建议）

| 批次 | 内容 | 交付物 | 预估 |
|---|---|---|---|
| **P0-a**<br>地基 | 用户体系 + RBAC + 会话 + 审计日志 | 登录页、团队与权限页、操作审计页、权限点矩阵 | 5–7 天 |
| **P0-b**<br>可靠性 | DB 租约调度器 + 任务心跳 + 就绪探针 + 限流 + 安全头 + 迁移脚本化 | `scheduler.py`、`/health/ready`、`/api/metrics`、迁移 runner | 4–5 天 |
| **P1-a**<br>流量地基 | `daily_metrics` 宽表 + 增量聚合 + 历史回填 + 时序 API | `/api/stats/trend`、内容时间线 API | 4–5 天 |
| **P1-b**<br>流量前台** | 流量总览页（趋势/漏斗/平台对比）+ 修正周报口径 + 筛选器 | 「分析」模块前端 | 4–5 天 |
| **P1-c**<br>ROI 与报表** | `campaigns` + `ai_usage_log` + 归因 + ROI API + PDF/XLSX 导出 + 定时推送 | ROI 面板、报表中心 | 5–6 天 |
| **P2**<br>闭环 | 线索跟进节奏/归属/CRM + 审核流增强 + 选题库 + 账号分组 | CRM 看板、审核中心升级 | 6–8 天 |
| **P3**<br>工程体验** | 前端拆分 + TS + 分页 + OpenAPI + E2E + 响应式 + 日历拖拽 + 全局搜索 | 工程基线提升 | 8–10 天 |

**总计：36–46 人日**（1 名前端 + 1 名后端并行，约 3–4 周）。

### 里程碑与验收

| 里程碑 | 验收标准（可执行） |
|---|---|
| M1 安全可交付 | 2 个角色（管理员/运营）登录互不可越权；删除账号/导出线索在审计页可见；无 Token 时全站 401（仅 `/health` 公开） |
| M2 稳定可交付 | 发布过程中 kill -9 重启，任务不丢不卡；`/health/ready` 全绿；任务列表可分页；迁移脚本可 dry-run 与回滚 |
| M3 流量可交付 | 趋势图可切换日/周/月/平台/账号/话题；单内容可看发布后逐日曲线；周报数字口径与趋势图**完全一致**（自动化断言校验） |
| M4 ROI 可交付 | 可看到当月 AI 成本总额与分场景拆分；线索可关联到发布内容；漏斗五层转化率可下钻；周报可导出 PDF 与 XLSX |
| M5 闭环可交付 | 线索可认领、可设下次跟进时间、到期进今日待办；审核意见与版本可见；账号可分组 |

---

## 8. 企业级 vs 个人级：双模式设计

同一套代码，通过「**工作区模式**」开关适配两种场景，避免为个人用户增加使用负担。

| 维度 | 个人模式（默认开箱） | 企业模式 |
|---|---|---|
| 身份 | 自动创建本地管理员，免登录 | 用户名密码 + RBAC |
| 数据范围 | 全部数据 | `all` / `assigned` 按人隔离 |
| 审计 | 保留 30 天 | 永久 + 导出 |
| 调度 | 单进程线程 + 每日备份 | DB 租约 + 多副本可选 + Prometheus + 告警 |
| 报表 | 周报（CSV/PDF） | 周报 + 定时推送 + ROI + 成本看板 |
| 配置 | `.env` 一次配置 | 管理后台可视化配置 |
| 部署 | 单机 `start.bat` | Docker Compose / systemd + Nginx + 备份 + 监控 |

**原则：企业能力不增加个人用户的日常操作步骤。** 所有新增能力在个人模式下自动降级、默认开启有用项、隐藏无意义项。

---

## 9. 风险与边界

| 风险 | 影响 | 缓解 |
|---|---|---|
| 平台风控/页面改版 | 自动化发布与私信同步随时失效 | 平台适配器独立可替换 + 失败自动告警 + 人工兜底通道（已在 README 声明边界） |
| 账号矩阵规模膨胀 | 单机 Playwright 内存/CPU 压力 | 账号分组 + 分批调度 + 限流 + 并发上限配置 |
| SQLite 写锁争用 | 高频同步时 `database is locked` | WAL + 短事务（已做）+ 聚合写批量化 + 读写分离（分析库只读副本） |
| 迁移影响生产数据 | 事故 | 迁移前强制备份 + dry-run + 版本校验拒绝降级启动 |
| AI 成本失控 | 费用超支 | 用量记录 + 日/月配额 + 超额告警 + 自动降级到本地模板 |
| 合规（广告法/平台规则） | 封号/限流 | 合规检查已有 → 扩展为词库可维护 + 发布前强制校验 + 敏感词版本化 |

---

## 10. 需要你决策的 4 个问题

> 下面 4 个问题会直接影响排期与实现方式，请给出结论后我再动手改代码。

### ❶ 用户体系走「轻量」还是「完整」？
- **A. 轻量**（推荐）：仅加「角色开关 + 审计日志」，不引入登录页，多人共用 Token。最快落地，满足追责。
- **B. 完整**：用户名密码 + RBAC + 数据隔离。功能最全，工作量 +5~7 天。

### ❷ 流量分析的目标受众是谁？决定看板粒度
- **A. 老板/决策层**（推荐）：月/周粒度、看趋势、对比、ROI，不需要分钟级明细。
- **B. 运营执行层**：日粒度、单内容曲线、逐条内容对比，需要更多交互。
- **C. 两者都要**：分层看板（概览 + 钻取），工作量 +2~3 天。

### ❸ 是否要做「本地定时调度」改造？
- **A. 做**（推荐）：平台侧定时受风控影响且我们无法确认真实执行状态；改造成本约 2~3 天。
- **B. 不做**：继续依赖平台定时，开发日历只做「计划表」，接受不确定性。

### ❹ 成本核算做到什么程度？
- **A. 仅 AI 成本**（推荐）：记录每次模型调用与费用，算单线索成本，不含投放/人力成本。
- **B. 完整 ROI**：AI 成本 + 投放预算 + 成交金额 + 回本周期。需要业务侧录入成交数据。
- **C. 不做**：只做流量统计，不算成本。

---

## 11. 附录 A — 本次实测明细

```
[通过] pytest tests -q                        → 134 passed in 4.77s
[通过] npm run build (vite 6.4.4)              → ✓ 1717 modules transformed, built in 8.59s
[通过] 27 个只读 API                          → 全部 HTTP 200，JSON 结构符合契约
[通过] POST /ai/generate (小红书·桂林漓江四日游) → 200，标题/正文/7话题/发布建议 完整
[通过] POST /dm/simulate (抖音·询价+国庆+预订)   → 200，intent=high，抽出 travel_date=国庆
[通过] /dm/export                              → 200，CSV 16 列表头（BOM 编码，Excel 可直开）
[通过] /stats/unmatched + POST /stats/link    → 200，孤儿数据可手工关联发布记录
[通过] /dm/storage/health                      → 200，WAL 大小/消息条数/备份列表 正常
[注意] /dm/inbound GET                         → 405（仅 POST，符合设计）
[注意] AI 生成需 12s / 次                      → 无流式返回，前端等待体验一般（建议加 SSE）
[注意] elementPlus chunk 1.09MB (gzip 341KB)   → 首屏偏重，需按需引入（见 P3-1）
```

## 11. 附录 B — 现状优点（改造时须保留）

1. **连接托管**（`ManagedConnection`）—— 彻底解决了旧版「只提交不关闭」导致的 WAL 锁死与「重启后聊天记录丢失」
2. **版本化迁移机制**已具备雏形（`schema_migrations` + `apply_migrations`），只需脚本化补齐
3. **账号数据隔离**（`filter_current_account_*`）—— 删账号后数据不串台，这个设计很对
4. **排期一次性落库**（`_expand_schedule` 不重算）—— 解决了历史「每次重启顺延一天」的严重 bug
5. **`myUtils/platform_registry.py`** —— 平台适配器注册表已是正确的收敛方向，应全面推开并复用
6. **发布策略显式传参**（`resolve_publish_strategy`）—— 避免了「定时被静默降级为立即发布」
7. **请求级日志与 request_id** —— 已具备可观测性雏形，补上指标导出即可
8. **134 个单测 + CI（pytest + ruff + 前端构建）** —— 工程基线良好，改造时应保持并扩充
9. **AI 兜底策略**（接口不可用自动降级本地模板）—— 稳定性意识很好
10. **安全默认值**（非回环监听强制要求 Token，否则拒绝启动）—— 思路正确，需扩展到用户级
