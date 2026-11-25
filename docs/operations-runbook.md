# 云策 FlowOps 生产运维手册

> 适用版本：2026-11 实名认证与指标批次
> 原则：先停止不可控的自动动作，再定位原因；宁可让任务进入人工处理状态，也不要未经确认重复发布。

## 1. 监控接入

Web 进程提供 Prometheus 文本指标：

```text
GET /api/metrics
X-YunCe-Token: <team.manage 权限令牌>
```

推荐抓取周期 30 秒，超时 10 秒。示例：

```yaml
scrape_configs:
  - job_name: yunce
    metrics_path: /api/metrics
    scheme: https
    static_configs:
      - targets: ["ops.example.com"]
    authorization:
      type: Bearer
      credentials_file: /etc/prometheus/yunce-token
```

告警规则见 `deploy/prometheus-alerts.yml`。当前告警覆盖进程不可抓取、任务积压、执行器离线、uncertain 比例、无有效账号、无管理员、管理员未启用 MFA、磁盘不足和 AI 连续失败。

## 2. 事故分级

| 级别 | 示例 | 首次响应 | 目标恢复 |
| --- | --- | --- | --- |
| P0 | 未授权访问、重复公开发布、凭据泄露、无管理员 | 立即停止执行器或关闭入口 | 30 分钟内隔离 |
| P1 | 队列持续积压、executor 离线、磁盘不足、发布成功率骤降 | 停止新增发布 | 2 小时内恢复或降级 |
| P2 | AI 或通知依赖异常、单平台适配失效、非关键指标缺失 | 保留人工兜底 | 当班处理 |

任何 P0/P1 事故都要记录时间线、操作人、任务 ID、日志目录、数据库状态、远端 ID/截图、影响范围和回滚结果。

## 3. 标准处置

### A. 管理员不可用

1. 使用离线保管的全局引导令牌登录。
2. 进入「设置 → 团队」，启用或创建管理员。
3. 验证个人账号可登录、`/api/metrics` 显示 `yunce_auth_active_admins >= 1`。
4. 完成操作后撤销临时会话，将引导令牌继续离线保管。

禁止通过删除 `users` 表或关闭全部鉴权恢复访问。

### A2. 管理员丢失认证器

1. 使用另一名管理员账号在「设置 → 团队」重置该成员的双因素认证。
2. 如果只剩一名管理员，使用离线保管的全局引导令牌登录后重置。
3. 重置会撤销目标成员全部会话；本人重新登录后必须在「设置 → 安全」重新绑定。
4. 检查丢失设备前的登录和审计记录；若有可疑活动，同时重置密码并轮换平台凭据。

### B. 人员离岗或凭据泄露

1. 立即在团队页禁用该成员或重置密码；现有会话会在下一次请求失败关闭。禁用、角色变更和账号范围变更会自动执行发布任务联锁。
2. 核对接口返回的 `jobRevocation.cancelled`、`cancelledItems` 和 `deferred`。`deferred` 表示仍有已进入平台调用或结果未知的子任务，必须人工核对。
3. 在团队页使用「停待发」可先查询并取消该成员仍归属的未开始任务；例行停止前可调用 `POST /api/v2/users/{id}/cancel-queued-jobs` 并传 `dryRun=true` 预览。
4. 检查审计日志中的登录、发布、账号、素材和导出事件，并确认 `authorization_revoked` 或 `authorization_paused` 任务事件。
5. 对 `running`、`processing`、`uncertain` 子任务回查远端平台；已发布则保留远端 ID 和证据，未发布才允许重新创建任务。禁止把暂停任务直接手工改回 pending。
6. 如 Cookie 或 AI Key 可能泄露，停止 executor，轮换对应平台登录态或密钥。

升级到 `publish_jobs` schema v7 时，历史任务无法可靠补出创建人。升级前排空或导出开放队列，升级后人工处理 `created_by` 为空且仍处于 pending/running/processing/paused 的旧任务。

### 执行器离线

1. 检查 Web 的 `/health/ready` 是否报告 `publish-executor`。
2. 查看 `logs/executor/` 或 `docker compose logs yunce-executor`。
3. 确认 Web 与 executor 挂载同一份 `data/db`，并且只有一个生产 executor。
4. 重启 executor 后等待租约回收；不要手工把 `running` 任务直接改成 `pending`。
5. 重启前记录 `jobs.executor_id/lease_token/heartbeat_at`，重启后核对任务不会被重复认领。

### 任务积压

1. 看 `yunce_jobs_stuck_pending`、`yunce_jobs_total{status="pending"}` 和 executor 心跳。
2. 停止新增发布，避免积压继续扩大。
3. 检查 SQLite 锁、磁盘空间、Chromium 崩溃和平台风控。
4. 按平台、账号、素材大小和任务阶段拆分延迟，不用单次平均耗时判断容量。
5. 恢复后先处理一条 canary，再逐步恢复批次。

### uncertain 任务

1. `uncertain` 表示可能已经提交到远端，禁止自动重试。
2. 用平台作品 ID、发布时间、标题和截图核对远端结果。
3. 已发布则回写成功及远端 ID；未发布才允许创建替代任务。
4. 同一幂等键和同一账号的替代任务要保留原任务关联，避免账实不符。

### 磁盘不足

1. 低于 2 GiB 时停止新的视频上传和发布。
2. 先备份数据库和关键配置，再清理可重建日志、过期派生数据和已确认无引用的临时文件。
3. 不得直接删除 `db/`、`cookiesFile/`、`videoFile/` 或未核对引用的素材。
4. 恢复空间后运行 `/health/ready`、备份和一次私有发布 canary。

### 账号失效

1. 检查账号巡检结果、代理出口、验证码和平台风控提示。
2. 暂停该账号自动动作，避免持续失败触发更强风控。
3. 由管理员重新扫码或更新受支持的登录态。
4. 重新验证账号身份、私信/评论归属和发布权限后再恢复。

### AI 与第三方依赖异常

1. 查看 `yunce_ai_failures_today`、AI 用量记录和上游状态。
2. 达到配额或成本阈值时降低每日上限，继续使用本地模板或人工草稿。
3. AI 不得被当作价格、库存、承诺或联系方式的事实来源。

### Web 不可用

1. 检查进程/容器和反向代理，不要先删数据卷。
2. 查看 `/health`、`/health/ready` 和 Web 日志。
3. 如果发布仍在进行，优先确认 executor 状态；Web 恢复前避免手工重复提交任务。
4. 回滚应用版本时保留新增数据库列和会话表，不要用旧库覆盖生产库。

## 4. 备份与恢复

备份使用 `deploy/backup.sh`。恢复必须在临时目录或 staging 先验证：

备份完成后可独立检查压缩包：

```bash
python3 deploy/restore_verify.py backups/yunce-YYYYmmdd-HHMMSS.tar.gz --json
```

检查项包括压缩包路径安全、4 个 SQLite 的 `quick_check`、必要表和关键行数、实名管理员与孤儿会话、环境变量必要键和凭据文件数量。默认备份脚本在系统有 `python3` 时会自动执行该校验；设置 `VERIFY_BACKUP=0` 才能跳过，不建议在生产跳过。

1. 停止 Web 和 executor，避免恢复时继续写库。
2. 保留现场数据副本和版本信息。
3. 解压备份到隔离目录，检查 4 个 SQLite 文件、Cookie 主密钥和环境变量是否配套。
4. 运行 `PRAGMA quick_check`，核对用户、任务、审计和账号数量。
5. 在隔离环境启动 Web-only，验证登录和只读页面。
6. 再验证 executor 心跳和一条私有 canary。
7. 记录 RPO、RTO、恢复前后数量差异和失败原因。

仅有备份文件不等于恢复能力；每次生产发布前至少保留一份已知可恢复的演练记录。

## 5. 发布与回滚

1. 发布前备份数据库、`cookiesFile/` 和 `.env`。
2. 后端全量测试、前端 lint/测试/构建、权限黑盒和 `/health/ready` 必须通过。
3. 先发布 Web 或先停 executor 的方案必须写清，不能在两个 `all` 模式进程同时运行。
4. 迁移使用增量 DDL；旧代码应忽略新增列。
5. 出现 P0/P1 时停止 executor、回滚应用版本、恢复入口；不要删除新增审计字段或会话表。

## 6. 当前未取得的生产证据

- 抖音、小红书、视频号真实私密/草稿发布及远端终态回查。
- 真实扫码登录跨 Web/executor 重启保持。
- Docker split 实际启动和容器级故障恢复。
- Web/executor 强杀、Chromium 分片崩溃。
- 生产旧库脱敏副本迁移和完整备份恢复演练。
- 30—80 用户、20—50 账号、每日 200—1000 子任务的容量基线。

上述项目在取得 E3/E4 证据前，只能标记为“待验收”，不能对外声明生产通过。

## 7. 已知安全增强项

- 当前 SSE、下载链接和部分媒体预览沿用查询参数令牌方式。必须确保 Nginx/网关访问日志不记录完整 query，或后续改为短时媒体票据/HttpOnly 会话；共享屏幕、浏览器历史和 Referer 也可能泄露 URL 令牌。
- 登录限流同时包含单 Web 进程内窗口和 SQLite 持久失败锁定；多 Web 实例部署前仍应把窗口计数迁移到共享存储。
- 已支持标准 TOTP 双因素认证，但尚未接入企业 OIDC/SAML SSO。生产管理员应启用双因素并限制管理入口网络范围。
