# FlowOps 双版本对比与合并报告

- 仓库：`E:/project/Dtou/flowOps`，分支 `main`
- 上游基线：`f3c373f` → 最新 `9d1c3bf`（GitHub `datouy/FlowOps`）
- 本地版本：`f3c373f` + 未提交工作区改动（35 个文件，+1181 / −1579）
- 合并完成后 HEAD：`9d1c3bf`，工作区 = 上游最新 + 本地重构增量（无提交，未 push）
- 备份：`E:/project/Dtou/_flowops_backup/pre-pull-20261022-113205`（56 个文件 + patch），`git stash@{0}`

---

## 一、两个版本改了什么

| 维度 | 本地版本（WIP） | 上游版本（41eaff7 + 9d1c3bf） |
| --- | --- | --- |
| 改动文件 | 35 个 | 10 个（两提交） |
| 行数 | +1181 / −1579 | +585 / −165 |
| 架构方向 | 抽公共层：新增 `BaseDmSync`（753 行）到 `dm_platform_common.py`，三个平台模块瘦身成适配器（douyin 622 行 / channels 263 行 / xhs 323 行） | 在三个平台模块里**各自**内联多账号并行 + 持续轮询，抖音增加常驻浏览器复用 |
| 私信同步 | 单账号串行、按 `interval_seconds` 定时；间隔下限 5/30 秒 | 账号级 `ThreadPoolExecutor` 并行；间隔可为 0 = 上一轮结束立即下一轮；登录失效退避 |
| 抖音 | 每次扫描新开 Playwright + context，扫描完关闭 | 独立 asyncio 事件循环线程承载常驻浏览器，按账号缓存 context/page，Cookie mtime 变化才重建；`quick` 低延迟模式（limit≤20、1 次滚动、短 settle） |
| 数据层 | 新增 `db_utils`（WAL + busy_timeout + 版本化迁移）、`runtime_config`、`app_logging`；`sqlite3.connect` 全部换成 `db_utils.connect` | 无变化（仍直接 `sqlite3.connect`） |
| AI/线索 | `dm_ops` 拆出 `_initialize_schema` / `_migrate_message_counters`，补索引 | 新增 `_score_priority_lead` / `priority_leads` / `dm_priority_leads`，待办页展示「今日优先跟进」 |
| 前端 | 拆分 `api/dm.js`、`utils/dm-format.js`、`composables/useLivePolling.js`、`config.js`（统一 API_BASE + token） | 同步模式 UI（0=持续）、消息页 1 秒刷新、`?contact=<id>` 深链直达会话 |
| 测试 | 新增 6 个 pytest 模块 + `.github/workflows/ci.yml`（pytest + ruff + npm build） | 无测试改动 |

**结论**：不是简单的「谁覆盖谁」。本地改的是**结构**（去重、可测试、可观测），上游改的是**功能**（并行、常驻浏览器、优先线索）。两侧都改到同一批文件，属于语义冲突。

## 二、冲突与合并决策

两侧同时修改 7 个文件：`README.md`、`channels_dm_sync.py`、`dm_ops.py`、`douyin_dm_sync.py`、`ops_center.py`、`Messages.vue`、`xiaohongshu_dm_sync.py`。

| 文件 | 处理方式 | 说明 |
| --- | --- | --- |
| `dm_ops.py` / `ops_center.py` / `README.md` | git 三方自动合并 | 本地迁移封装 + 上游优先线索，互不重叠 |
| `dm_platform_common.py` | 保留本地 `BaseDmSync`，**扩展**上游能力 | 并行、持续轮询、quick、退避统一到公共层 |
| `douyin_dm_sync.py` | 本地瘦身版 + 上游常驻浏览器整套逻辑 | 保留 `DouyinDmSync` 适配器结构，把上游 `_ensure_douyin_browser_loop / _get_douyin_session / _scan_douyin_in_browser / quick` 原样搬入 |
| `channels_dm_sync.py` / `xiaohongshu_dm_sync.py` | 用本地瘦身版 | 并行与轮询由基类承担，平台文件只保留协议解析与 `scan_account` |
| `Messages.vue` | 本地重构版 + 上游三处 UI 功能 | 同步模式文案、`??` 取 0、1 秒轮询、`useRoute` 深链 |

### 合并后 `BaseDmSync` 的能力

- 每账号并行扫描：`max_workers`（抖音 12，视频号/小红书 8），结果顺序与账号顺序一致
- 持续轮询：`interval_seconds = 0` → 上一轮结束 0.5s 后立刻下一轮；`> 0` → 按间隔
- 失败退避：默认 30s（抖音 300s），避免登录失效时高频空转
- `quick` 触发：`trigger in quick_triggers`（默认 `auto`），子类签名带 `quick` 才透传，老子类自动兼容
- 统一状态：`sync_mode`（continuous/interval）、`parallel_workers`、`next_run_at`、`timings`
- 三个 HTTP 路由、设置校验、状态聚合仍集中在基类

## 三、验证结果

| 项目 | 命令 | 结果 |
| --- | --- | --- |
| 单元/集成测试 | `python -m pytest` | **87 passed**（原 73 + 新增 14） |
| 静态检查 | CI 白名单 `ruff check ...` | **All checks passed** |
| 后端接口冒烟 | `python scripts/smoke_backend.py` | **70/70 通过**（沙箱 BASE_DIR、禁用真实平台） |
| 服务启动冒烟 | `python scripts/smoke_server.py` | **8/8 通过**（真实端口 + 3 条同步线程启动） |
| 前端构建 | `npm run build` | **成功**，1717 modules，11.8s |
| 模块导入 | 57 个业务模块 import | 全部成功（仅 `conf.example` 非模块） |

新增测试：
- `tests/test_dm_sync_parallel.py`：并行耗时/线程、结果顺序、quick 透传、持续/间隔调度、单账号失败不阻塞、旧签名兼容、状态字段
- `tests/test_douyin_dm_sync.py`：Cookie 缺失兜底、quick 透传、常驻事件循环单例、跨线程协程桥、会话校验、行过滤

> 未做真机验证：抖音/小红书/视频号的真实私信同步需要账号 Cookie 与 Playwright Chromium，本次合并**没有**对真实平台发起任何请求（冒烟测试全程沙箱化）。

## 四、待优化清单（按优先级）

### A. 正确性 / 一致性（建议优先）

1. **规则分类枚举对不上**：`dm_ops.save_rule` 只接受 `segment ∈ {"", "silver", "family", "team"}`，而系统实际枚举是 `SEGMENTS = {general, new, interested, existing, unknown}`。前端选「意向客户」保存后会被静默丢成通用规则。
2. **`draft_only` 写死为 1**：`BaseDmSync.settings_view` 强制 `draft_only=1`，配置项存在但用户无法关闭。
3. **`/dm/inbound` 未配密钥时完全放行**：`_check_inbound_secret` 在 `inbound_secret` 为空时返回 True；配合 `YUNCE_HOST=0.0.0.0` 就是公网可写接口。
4. **`POST /dm/rules`、`POST /message-templates` 不返回新建 id**：前端只能整表重拉，脚本化调用拿不到 id。

### B. 稳定性 / 资源

5. **抖音常驻会话无上限**：`_douyin_browser_runtime["sessions"]` 仅在 Cookie mtime 变化、page 关闭、登录异常时清理，缺少空闲超时 / LRU 上限，长时间运行会累积 context。
6. **Playwright 无关闭钩子**：事件循环线程与浏览器进程没有 `atexit`/`teardown` 收尾，热重载或退出时会留下残留进程。
7. **并行写 SQLite 无重试**：12 线程并发 `ingest_many`，WAL + 30s busy_timeout 只是兜底；建议加写队列/重试装饰器，避免偶发 `database is locked`。
8. **`_pending_conversation_unread` 查询缺复合索引**：按 `platform/account/direction/status` 过滤并按 contact 分组，现有索引（status、intent、contact_id、external）覆盖不全，账号多时每轮扫描都全表扫。

### C. 性能 / 体验

9. **消息页 1 秒全量轮询**：`useLivePolling(interval: 1000)` 每秒拉会话列表 + 详情，建议改增量（`since_id`/`updated_after`）或 SSE。
10. **`todo_summary` 每次请求算 `priority_leads(6)`**，无缓存；待办页刷新时可加 5–10 秒内存缓存。
11. **`quick` 能力靠签名反射判定**：子类若用 `**kwargs` 会误判；建议改成显式类属性 `supports_quick = True`。
12. **`max_conversations` 前端未暴露编辑**：设置页只显示同步间隔，后端支持 1–200，UI 缺失。
13. **并行测试依赖 `sleep`**：`test_dm_sync_parallel` 用 0.15s 睡眠断言并行，CI 慢机可能抖动，建议改 `threading.Barrier`。

### D. 工程化

14. **ruff 只覆盖白名单**：全仓仍有 33 处存量问题（`uploader/tencent_uploader`、`ai_content.py`、`examples/`、`db/createTable.py`），建议逐步扩到全仓。
15. **`conf.py` 的 `DEBUG_MODE=True` 硬编码**、`db/createTable.py` 用相对路径 `./database.db` 依赖 `os.chdir`，建议改为显式路径 + 环境变量。
16. **`interval_seconds` 语义扩张后缺文档**：0=持续、>0=间隔，需要写进 README / `.env.example`，否则运维会误配成高频轮询。
17. **配置文件版本化**：`requirements.txt` 与 `requirements.lock` 并存，建议明确以 lock 为准并写进 CI。
