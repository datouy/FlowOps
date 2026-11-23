# FlowOps 问题总清单（2026-11-23）

> 范围：本次对话中确认过的架构事实、当前工作区代码审查发现、本轮实际测试结果、未验证风险和对外沟通限制。
>
> 代码审查基线：Git HEAD 18fc05a。本轮四角色权限修复仍在工作树中，尚未提交、尚未部署。
>
> 本清单不把静态代码完成、聚焦测试通过或本地调度成功当成真实平台发布成功。

> **后续批次更新（同日）**：已在当前工作树增加实名用户与会话、TOTP 双因素、持久登录锁定、团队管理、Prometheus 指标、告警规则、运维 runbook 和备份恢复校验器。身份不再只依赖用户令牌或共享全局令牌；但真实 Web/反向代理登录、真实手机认证器端到端验收、企业 SSO、生产等价用户数据、Prometheus 告警实触发和真实备份恢复演练仍未完成。本文件下方关于“身份仍是运行时令牌”的描述应结合 `docs/medium-enterprise-optimization-plan.md` 的实施记录阅读。

## 0. 证据状态

| 项目 | 当前证据 | 结论 |
| --- | --- | --- |
| 本轮后端全量测试 | 2026-11-23 实跑 `pytest tests -q`：721 passed，164.91 秒 | 当前工作树的后端全量结果；包含新增四角色与直接路由回归 |
| 本轮静态检查 | Ruff `All checks passed`；`py_compile` 通过 | 证明所选 Python 文件可编译且通过当前静态规则，不证明真实平台发布 |
| 本轮前端验证 | `npm run lint` 通过；Vitest 2 个文件、5 个测试通过；`npm run build` 成功 | 证明当前前端可 lint、可测试、可构建，不等于浏览器端人工验收 |
| 对话早期全量快照 | 681 passed，20 failed | 历史快照，仅用于解释旧测试债务，不再代表当前工作树 |
| 仓库已有生产验收文档 | 2026-11-20 记录 708 passed，0 failed | 历史记录；不得替代本轮 721 passed 或未来发布前重跑 |
| 真实平台发布 | 未验证 | 不能宣称抖音、小红书、视频号生产发布通过 |
| 真实二维码登录 | 未验证 | 未证明登录态跨 Web/executor 重启保持 |
| 强杀与 Chromium 分片崩溃 | 未验证 | 未证明真实租约回收、无重复发布和分片隔离 |
| Docker split | 未验证 | 当前机器无 Docker；compose 和容器未实际运行 |
| 性能 | 全部未测 | 不能宣称吞吐、P50/P95/max、队列积压、成功率、uncertain 比例或内存有量化提升 |

## 1. 权限、角色与越权风险

### 1.1 PERM-01 至 PERM-13 优先级复核

分档标准同时考虑：已认证低权限用户能否直接利用、是否阻塞剪辑/审核/运营同时使用、修复成本与回归风险。

| 编号 | 上线前档位 | 可利用性 | 对三角色并用的阻塞性 | 修复成本与回归风险 | 本轮状态 |
| --- | --- | --- | --- | --- | --- |
| PERM-01 | 上生产前必须堵 | 高；operator 直接继承多项高权能力 | 直接阻塞 | 中；涉及令牌、路由、前端和测试 | 已修复 |
| PERM-02 | 上生产前必须堵 | 高；错拼角色可得到可登录身份 | 间接阻塞 | 低；集中令牌解析和启动校验 | 已修复 |
| PERM-03 | 上生产前必须堵 | 高；漏填 scope 扩大账号范围 | 直接阻塞 | 低到中；影响所有账号范围判断 | 已修复 |
| PERM-04 | 上生产前必须堵 | 高；/v2 可进入不同授权结果 | 直接阻塞 | 中；涉及全部别名和路由登记 | 已修复 |
| PERM-05 | 上生产前必须堵 | 高；一个 publish 同时授予编辑、审核和发布 | 直接阻塞 | 中；需同步旧客户端和测试 | 已修复 |
| PERM-06 | 上生产前必须堵 | 高；客户端可绕过审核进入发布队列 | 直接阻塞且后果不可逆 | 低到中；审核策略集中在服务端 | 已修复 |
| PERM-07 | 上生产前必须堵 | 高；GET 接口可读审计、账号和媒体 | 直接阻塞 | 中；需补齐业务领域授权 | 已修复 |
| PERM-08 | 上生产前必须堵 | 中到高；审核接口暴露执行器内部字段 | 企业验收阻塞 | 低；改为固定字段和有限状态 | 已修复 |
| PERM-09 | 可灰度期修 | 无后端新增越权；主要是错误入口和体验 | 不阻塞后端安全边界 | 中；需读取 session 并贯穿页面 | 已提前修复 |
| PERM-10 | 上生产前必须堵 | 中；审批结果无法归责，可能被内部滥用 | 企业验收直接阻塞 | 低；同一审核事务补字段 | 已修复 |
| PERM-11 | 上生产前必须堵 | 中；创作和素材操作无法追责 | 企业验收直接阻塞 | 中；增量 schema 和写入点 | 已修复 |
| PERM-12 | 可灰度期修 | 无直接越权；误报会遮蔽配置错误 | 间接影响运维确认 | 低；读取真实令牌状态 | 已提前修复 |
| PERM-13 | 可延后 | 撤权后新请求已失败关闭；旧队列继续执行 | 不阻塞最初三角色上线 | 高；要授权快照和队列撤销策略 | 口径已明确，自动取消延后 |

**PERM-04 与 PERM-06 的紧急度比较：**如果只能先修一个，PERM-06 更紧急。它让未经审核的内容直接进入可发布状态，业务后果不可逆，而且可能直接造成违规内容上线。PERM-04 破坏角色边界，影响面的接口更广，同样是上生产前必须同期堵住的 P0；不能把 PERM-06 优先解释成 PERM-04 可以晚一版。

PERM-13 的当前产品口径固定为：

- 撤销或禁用令牌后，后续新请求立即认证失败，不再获得原角色权限。
- 已经入队的任务继续执行；管理员若要求停止，必须显式取消相应 job/item。
- “撤销身份时自动取消该身份尚未执行的任务”需要任务创建者、授权快照和批量撤销策略，列为后续增强。

### 1.2 最小可上线权限矩阵

`viewer` 仅为旧令牌迁移兼容身份，不具有业务权限；新部署只分配下面四个生产角色。

| 权限 | cutter | reviewer | operator | admin |
| --- | --- | --- | --- | --- |
| `content.edit` | 允许 | 拒绝 | 拒绝 | 允许 |
| `material.write` | 允许 | 拒绝 | 拒绝 | 允许 |
| `review.decide` | 拒绝 | 允许 | 拒绝 | 允许 |
| `publish.execute` | 拒绝 | 拒绝 | 允许 | 允许 |
| `message.operate` | 拒绝 | 拒绝 | 允许 | 允许 |
| `account.manage` | 拒绝 | 拒绝 | 拒绝 | 允许 |
| `team.manage` | 拒绝 | 拒绝 | 拒绝 | 允许 |
| `audit.read` | 拒绝 | 拒绝 | 拒绝 | 允许 |

必须拆开的权限：

- `content.edit`：允许保存和修改草稿，但不等于可提交发布。
- `material.write`：允许上传、改标签和清理素材，不自动获得审核或发布权。
- `review.decide`：只允许读取安全审核 DTO 并通过或驳回待审核任务。
- `publish.execute`：只允许运营创建发布任务、控制排期和查看发布结果。
- `message.operate`：只允许私信和评论操作，不与发布权捆绑。
- `account.manage`、`team.manage`、`audit.read`：账号、登录、凭据、系统运维、团队和审计归管理员。

### 1.3 三条失败关闭策略

| 配置风险 | 现行处理 |
| --- | --- |
| 未知角色 | 启动时严格校验并报错；运行期若令牌文件被改成未知角色，该条目不再生成身份，认证失败关闭，不降级为 viewer。 |
| 非管理员空 scope | 启动时报配置错误；运行期认证跳过该条目；账号列表、账号动作和任务过滤都拒绝跨范围访问。管理员由服务端生成 `scope_mode=all`。 |
| `/v2` 兼容别名 | 授权前统一规范化为 `/api/v2`；同一路由只使用一份“方法 + 路径”策略；受保护但未登记策略的请求在启用令牌时返回 403。 |

### 1.4 审核链路的服务端强制

服务端按以下条件共同决定是否强制审核：

1. 内容已持久化 `review_required=1` 时永久保持强制，后续低权限请求不能把它降回直发。
2. `FLOWOPS_REVIEW_MODE=all_required` 时，cutter、operator 和 admin 都强制审核。
3. 默认 `FLOWOPS_REVIEW_MODE=cutter_required` 时，cutter 强制审核；operator 和 admin 保持旧直发流程。
4. reviewer、viewer 或未登记角色若进入提交路径，一律失败关闭为强制审核。
5. 内容进入审核、排期或执行状态后冻结，禁止继续修改标题、正文、素材和变体。

请求体中的 `reviewRequired` 当前被**忽略**，只保留字段兼容，既不作为授权依据，也不作为审核策略建议。前端已停止发送该字段。返回体中的 `reviewRequired` 是服务端计算后的实际结果。

验证方式：

- 自动回归以 cutter 身份先保存内容，再提交 `POST /api/v2/posts/{id}/submit` 且显式传 `reviewRequired=false`，断言响应为 `true`，并直接查询 `jobs.status=pending_review`、`jobs.review_required=1`。
- 另一条回归把团队策略设为 `all_required`，以 operator 调用 `/postVideo`，断言入队参数仍为 `review_required=true`。
- 发布前人工黑盒应使用真实 cutter 令牌重复同一请求，并确认发布 worker 未认领该任务。

兼容性：operator 原有 `/postVideo`、批量发布和排期流程在默认策略下仍可直发；cutter 的保存草稿和素材上传不受影响。operator 不再拥有内容编辑权，这是四角色权限边界的有意变化。

### 1.5 审计字段最小落库

| 数据 | 最小 schema 变更 | 写入层 |
| --- | --- | --- |
| 审核决定 | `jobs.reviewed_by TEXT NOT NULL DEFAULT ''`；沿用 `reviewed_at`、`review_decision`、`review_note` | `publish_jobs.approve_job()` / `reject_job()` 在状态条件更新的同一事务中写入，路由提供当前操作人 |
| 内容创建 | 沿用并强制写入 `contents.created_by` | `p0p1_ops.py` 的内容创建和开放 API 内容创建业务函数 |
| 素材上传 | `file_records.uploaded_by TEXT NOT NULL DEFAULT ''` | `material_ops.record_upload()`；`uploadSave` 传入当前操作人 |
| 审计事件 | 沿用 `audit_logs` | `rbac_audit.record_audit()`；审核通过/驳回另写 `review.approve` / `review.reject` |

写入点放在业务函数或业务事务，不放在 Flask 路由拼接 SQL，也不使用数据库触发器。理由：业务函数能拿到统一身份对象，并能在同一事务中保证审核状态和审核人一致；触发器拿不到可信的 HTTP 身份。审计日志是附加证据，失败不回滚已完成的业务事务；`reviewed_by`、`created_by`、`uploaded_by` 是业务事实字段，与对应业务写入一起处理。

历史记录的空操作人统一显示为“历史未记录”，不得根据当前登录人、最早管理员或猜测数据回填。历史值空白的数量可单独统计，但不能伪造操作者。对企业验收的直接影响是：无法证明谁创建内容、谁上传素材、谁批准发布，事故追责和职责分离验收不成立。

### 1.6 改动文件清单

| 文件 | 改动性质 | 主要内容 |
| --- | --- | --- |
| `runtime_config.py` | 修改角色/权限映射表、修改默认值、增加启动校验 | 新增 cutter/reviewer；八个权限点；未知角色和空 scope 失败关闭；审核模式校验；真实 RBAC 状态 |
| `access_scope.py` | 修改集中权限映射表、增加路径规范化 | `/v2` 归一化；方法+路由策略；未登记保护路由 403；非管理员空 scope 拒绝 |
| `sau_backend.py` | 加强现有 `before_request` 中间件、修改路由注册 | 全路由集中授权；晚注册 `/v2` 别名同步；账号脱敏 DTO；审核安全 DTO；审核人和审计写入 |
| `p0p1_ops.py` | 修改业务函数、修改路由行为 | 服务端审核策略；忽略客户端 `reviewRequired`；写入 created_by；RBAC/审核字段返回；内容状态冻结 |
| `publish_jobs.py` | 增量 schema、修改业务函数 | 审核字段与索引迁移；approve/reject 条件更新；`reviewed_by` 持久化 |
| `material_ops.py` | 增量 schema、修改业务函数 | `uploaded_by`；统一上传写入；素材操作审计 |
| `rbac_audit.py` | 修改角色元数据和审计写入 | 四角色元数据；可信当前操作人；结构化审计函数 |
| `db/createTable.py` | 修改新库默认 schema | 新安装的 `file_records` 直接包含 `uploaded_by` |
| `sau_frontend/src/stores/user.js`、`App.vue`、`CommandPalette.vue` | 修改前端状态与可见性 | 读取 `/api/v2/session`；权限判断；身份显示；导航和命令入口收敛 |
| `ContentCenter.vue`、`ContentLibrary.vue`、`PostComposer.vue`、`Review.vue`、`MaterialManagement.vue`、`PublishCenter.vue`、`WorkspaceSettings.vue` | 修改页面级能力展示 | 按权限显示新建、审核、素材、设置和发布入口；不再发送 `reviewRequired`；展示操作人 |
| `tests/test_permission_matrix.py` | 新增黑盒回归 | 四角色矩阵、直接路由、审核强制、安全 DTO、别名、失败关闭、无副作用 |

本轮没有新增调度、执行器、租约或浏览器分片逻辑。

### 1.7 本轮验证与回滚

本轮实际验证结果：

- 后端全量：`721 passed in 164.91s`。
- Ruff：选中后端与测试文件全部通过。
- `py_compile`：选中后端与测试文件通过。
- 前端：ESLint 通过；Vitest 5 passed；Vite production build 成功。
- 自动路由覆盖检查：除 `/`、静态资源、`/favicon.ico`、`/health`、`/health/ready` 外，没有未登记权限策略的保护 Flask 路由。

发布前仍需用真实令牌和独立进程重复直接 HTTP 黑盒，不能把 Flask test client 结果当成生产浏览器验收。

回滚方式：

- 当前代码未提交、未部署。应先审阅 diff，按功能形成可回退提交，再发布；需要回滚时按功能使用 `git revert`，不要对含其他未提交工作的脏工作树执行硬重置。
- `jobs.reviewed_by`、`file_records.uploaded_by` 都是增量列；旧代码可以忽略新列。代码回滚时不删除列、不覆写已记录的审核人和上传人。回滚或迁移前先备份事实数据库。
- 代码回滚必须同步恢复令牌配置和 `FLOWOPS_REVIEW_MODE`；只回滚 Python 文件会让新旧权限口径混合。
- 前端回滚需恢复对应源码并重新执行 `npm run build`，不能只替换单个哈希资源。

### 1.8 修复前证据（保留用于审计）

以下条目记录缺口被发现时的代码事实和影响；当前修复状态以上述清单为准。

### PERM-01：四角色权限闭环尚未实现

**状态：已修复；企业级上线 P0。**

修复前实际生效的角色只有 admin、operator、viewer，没有剪辑和审核角色。剪辑人员若使用 operator，会继承发布、私信和审核能力；审核人员若使用 operator，也会继承发布能力。

证据：

- runtime_config.py:43-58 只定义 admin、operator、viewer。
- App.vue:169 的导航固定显示。
- ContentCenter.vue:40 和 WorkspaceSettings.vue:43 没有按角色过滤视图。

影响：不能安全地把系统同时交给剪辑、审核和运营使用。

### PERM-02：未知角色被静默降级为 viewer

**状态：已修复；企业级上线 P0。**

runtime_config.py:197 将未知角色转换为 viewer。令牌文件写入 reviewer、editor 或其他拼写时，服务不会报配置错误，而会生成一个可登录的只读身份。

影响：角色配置错误难以发现；预期只读审核身份与实际身份不一致；无法形成失败关闭的权限系统。

### PERM-03：非管理员空 scope 等于全账号范围

**状态：已修复；企业级上线 P0。**

runtime_config.py:270 将空 scope 解释为不限制。管理员使用空 scope 合理，但剪辑、审核或运营令牌漏填 scope 时会看到全部账号数据。

影响：一次令牌配置遗漏就可能扩大数据可见范围，并让 reviewer/viewer 读取全部账号元数据和关联业务数据。

### PERM-04：/v2 兼容别名绕过集中权限映射

**状态：已修复；企业级上线 P0。**

后端注册 /api/v2/* 的 /v2/* 别名，Vite 开发代理又会把 /api 前缀去掉。集中权限规则只登记 /api/v2 前缀，因此同一条路由经过别名后会落入默认 publish 权限。

修复前实际调用 `permission_for_request` 的结果：

- POST /api/v2/accounts/1/check 返回 account。
- POST /v2/accounts/1/check 返回 publish。

影响：operator 具有 publish，开发代理路径可能执行本应只允许 account.manage 的账号操作。账号范围仍然存在，但角色边界被突破。

证据：

- access_scope.py:95-130。
- sau_backend.py:1446-1467。
- sau_frontend/vite.config.js:69-72。

### PERM-05：内容编辑、素材、审核决定和发布共用一个 publish 权限

**状态：已修复；企业级上线 P0。**

access_scope.py:106 将 contents、publish、postVideo、review、materials、upload、media 等统一归为 publish。

影响：

- 剪辑要编辑内容就必须拥有发布能力。
- 审核人员要通过或驳回审核就必须拥有发布能力。
- 无法单独禁止剪辑直接发布。
- 无法让审核人员只处理内容审核。

### PERM-06：剪辑提交审核时可从客户端绕过审核开关

**状态：已修复；企业级上线 P0。**

p0p1_ops.py:497-586 的 content_submit 直接读取请求体 reviewRequired。只要未来把内容提交开放给剪辑，剪辑即可发送 reviewRequired=false 创建直接发布任务。

影响：审核链路可以被客户端参数绕过。审核强制必须由后端根据角色和内容状态决定。

### PERM-07：GET 接口没有按业务领域授权

**状态：已修复；企业级上线 P0。**

全局 before_request 主要对写方法进行领域授权；GET 通常只要求令牌和账号 scope。以下读取接口缺少独立领域权限：

- /api/v2/audit/logs：任何已认证读取身份可调用，没有 team 或 audit 权限检查。
- /api/v2/auth-sessions：只有账号范围过滤，没有 account 权限检查。
- /api/v2/accounts：只有范围过滤，没有独立的 account.read。
- /api/v2/accounts/{id}/credentials：只有范围过滤，没有管理员权限检查。
- /getFiles、/download、/getFile 和 DM 媒体下载缺少 material、message 等领域权限。

影响：审核或只读用户可绕过前端页面直接调用接口读取审计、账号、授权记录、凭据元信息或媒体。

证据：

- sau_backend.py:212-218。
- sau_backend.py:748、884、1435。
- p0p1_ops.py:284-293。

### PERM-08：审核列表返回过宽字段和任意任务状态

**状态：已修复；企业级上线 P0。**

sau_backend.py:2179-2186 的 /review/pending 接受任意 status 参数并执行 SELECT * FROM jobs。

影响：

- 审核身份可读取非待审核任务。
- 可能暴露 executor_id、租约和其他内部状态。
- 普通任务详情会移除 lease_token，审核接口没有同等字段收口。
- 审核页面与执行器协议的数据边界混在一起。

### PERM-09：前端只做登录校验，没有读取当前身份

**状态：已修复；可灰度期修，本轮已提前完成。**

App.vue:191-196 只调用 /auth/check，没有使用 /api/v2/session 返回的角色、权限点或 scope。所有导航和多数操作入口固定显示。

影响：用户会看到并尝试无权操作；错误主要通过后端 403 暴露；页面层无法提示最小权限状态。后端授权仍必须是安全事实，前端过滤只用于体验。

### PERM-10：审核决定没有记录审核人

**状态：已修复；企业级上线 P0。**

publish_jobs.py:2977-3025 记录 review_note、reviewed_at、review_decision，但没有 reviewed_by。

影响：无法证明具体由谁批准或驳回；发生内容事故时只能看到结果，不能形成完整审批链。

### PERM-11：内容和素材没有可靠的创建人、上传人归属

**状态：已修复；企业级上线 P0。**

- contents.created_by 字段存在，但 p0p1_ops.py:321、415 创建内容时没有写入。
- file_records 没有上传人字段；uploadSave 只记录文件名、大小、路径、MD5 和标签。

影响：无法限制“剪辑只能修改或删除自己的素材”，也无法从素材追责到具体操作人。最低上线阶段可以共享素材库，但上传、修改、删除仍需审计。

### PERM-12：RBAC 状态接口误报 ready

**状态：已修复；可灰度期修，本轮已提前完成。**

p0p1_ops.py:277-282 的 /api/v2/rbac/status 曾固定返回 ready=True，但 HTTP 鉴权实际来自 runtime_config 的 JSON 令牌；数据库 users/roles 表并不负责当前请求授权。

影响：运维或验收人员可能把“RBAC 状态接口返回 ready”误认为四角色权限已闭环。

### PERM-13：角色撤销不会阻止已经入队的任务

**状态：产品口径已明确；自动取消历史队列任务列为可延后增强。**

当前执行器没有请求身份；已批准或已入队的任务不会因为操作人权限随后被撤销而自动取消。

建议的最低口径：

- 撤销令牌立即阻止新请求。
- 已入队任务是否继续由管理员通过 job/item 取消接口明确决定。
- 若要求撤销即停止未执行任务，需要增加任务创建者、授权快照和撤销策略。

## 2. 功能正确性、可靠性与运维风险

### REL-01：真实三平台发布和远端终态尚未验证

**状态：最高业务风险，未验证。**

抖音、小红书、视频号尚未各完成至少一条真实私密或草稿发布。上传器返回、页面提示、submitted、审核中、转码中都不能直接当作发布成功。

通过标准：

- 远端平台确实能查到作品或 note。
- 本系统记录远端 ID、终态和证据。
- 不支持回查时最多标记 submitted，不能计入成功。
- 重启或重试不会重复发布。

### REL-02：真实二维码登录和跨重启登录态保持未验证

**状态：未验证。**

需要验证三个时点：

- 登录后刷新页面。
- 重启 Web。
- 重启 executor。

通过标准包括账号记录、storage_state、Cookie/localStorage、平台创作者页实际可操作性和账号间不串号。视频号多频道还必须逐频道验证独立 profile、指纹、代理和登录会话谱系。

### REL-03：Web/executor 强杀恢复未演练

**状态：已有代码和单测，缺少真实进程证据。**

未验证项：

- Web 被杀后 API 和就绪检查恢复。
- executor 被杀后租约过期、pending 恢复。
- 已提交但结果未知的 item 进入 uncertain。
- 人工解决 uncertain 前不会被自动重发。

### REL-04：Chromium 分片崩溃恢复未演练

**状态：代码完成，真实故障未验证。**

当前共享 Chromium 按账号稳定映射到多个分片，默认 2 片。仍需验证：

- 定向杀死一个 Chromium 分片。
- 其他分片继续工作。
- 崩溃分片下一次访问可重建 context。
- 崩溃期间任务不伪成功、不重复发布。

### REL-05：整机 OOM 仍会同时影响所有分片

**状态：已确认架构限制。**

分片降低单个 Chromium 崩溃的影响范围，但整机 OOM 或内核回收会同时影响 Web、executor 和所有浏览器分片。FLOWOPS_SHARED_BROWSER=0 可以回退一账号一进程，但会失去共享进程的容量优势。

### REL-06：默认 all 模式不会自动获得 Web/executor 分离收益

**状态：部署口径风险。**

FLOWOPS_PROCESS_ROLE 默认 all 用于兼容旧部署。只有明确启动 split 编排，才获得 Web 和 executor 的职责分离、独立日志目录和故障边界。

### REL-07：多 executor、跨主机事实库和 leader election 未完成

**状态：规模阶段缺口，非当前单机单 executor 的第一阻塞项。**

- SQLite 当前是单机事实源，不适合直接跨主机共享。
- 新增多个 scheduler/tracker 前需要 leader election 或等价协调。
- 横向扩容依赖多 executor、跨主机任务事实、协调机制和容量基线。

建议优先级：

1. 四角色权限闭环，企业上线前 P0。
2. leader election，单 Web 加单 executor 阶段之后、增加第二调度实例之前。
3. 多 executor + 跨主机数据库/队列，规模阶段。
4. 横向扩容，依赖前三项和性能基线。

### REL-08：端到端图片/视频预览与素材读取权限边界不清

**状态：业务领域授权已收口；单文件级素材 ACL 仍为后续增强。**

素材、预览、DM 媒体和普通文件下载现已按 `material.write`、`message.operate`、`account.manage` 等领域拒绝无权角色。当前最小矩阵仍允许 cutter、reviewer、operator 在获准领域读取共享素材；尚未实现“每个文件只允许上传人或指定审核人读取”的资源级 ACL。若企业要求素材按人隔离，还需 `material.read`、文件所有者与显式共享关系。

## 3. 已确认完成、不应继续当作 P0/P1 代码缺口的事项

以下事项已在当前架构或代码层收口；真实运行验收仍需单独完成。

| 事项 | 当前状态 |
| --- | --- |
| 持久化任务队列 | 已完成；postVideo 入队返回 job_id，写入 publish_jobs.db，并展开 item |
| /postVideoBatch 统一入队 | 代码层完成；全批校验后逐条入队，不再绕过 jobs/items/租约 |
| 调度、重试、租约、重启恢复 | 已完成，面向单机；成功子任务不会整批重发 |
| 跨进程账号互斥 | 已完成；publish、DM、评论、巡检、健康检查共用 SQLite 租约 |
| 执行器协议 | 代码层完成；claim token、heartbeat_token、错误 token 返回 409 |
| Web/executor 角色分离 | 代码层完成；由 FLOWOPS_PROCESS_ROLE 控制 |
| 账号 BrowserContext 隔离 | 已完成；每账号独立 storage_state、代理、指纹和初始化脚本 |
| Chromium 分片 | 已完成；账号稳定映射到多个共享 Chromium 分片 |
| split 日志隔离 | 已完成；支持 FLOWOPS_LOG_DIR |
| 账号检查事件循环修复 | 已完成；线程等待跨进程租约，浏览器检查回原事件循环 |

## 4. 测试债务与测试有效性问题

### TEST-01：历史 20 个失败不能再直接写成当前仍失败

**状态：已用本轮实跑结果更新；历史口径继续分开保留。**

对话早期提供的快照是 681 passed、20 failed。仓库当前的 production-acceptance-2026-11.md 记载 2026-11-20 后续重跑为 708 passed、0 failed。

因此：

- 本清单把 20 failed 标记为历史快照。
- 不把历史失败逐条当作当前开放缺陷。
- 2026-11-23 当前工作树已实际运行全量测试：721 passed，0 failed，用时 164.91 秒。
- 708 passed 仍是历史文档数字，不能替代本轮 721 passed；任何后续部署前应在最终提交上再跑一次。

### TEST-02：历史失败组的处置边界

仓库现有验收文档给出的历史分类：

| 历史失败组 | 旧分类 | 当前处置边界 |
| --- | --- | --- |
| DM 假接口签名 | A. 纯旧债 | 测试替身与真实接口约定不一致；需要保持替身同步，没有证据表明真实 DM 协议仍坏 |
| 媒体安全测试访问真实网络/FakeResponse 缺字段 | A. 纯旧债 | 测试隔离不足；修复应覆盖 mock URL、响应状态字段，避免环境制造假失败 |
| 账号表缺暂停字段 | B. 可能掩盖新问题 | 已加兼容和测试；上线前仍需用生产旧库脱敏副本验证 schema 兼容 |
| /deleteFile? GET 删除未受 publish 权限保护 | C. 产品缺陷 | 历史缺口已修；仍需在新的四角色矩阵下重新做黑盒越权测试 |

以上分类来自仓库文档和对话历史。没有当前失败详情时，不能重新判断具体根因。

### TEST-03：权限映射单测通过不等于权限闭环

**状态：本轮已增加直接 Flask 路由黑盒，代码层缺口覆盖完成；生产进程验收仍待执行。**

本轮不再停留在权限映射单测。`tests/test_permission_matrix.py` 直接请求 Flask 路由，并检查任务库、主库、文件哈希和审计结果。覆盖：

- cutter、reviewer、operator、admin、viewer 的精确权限集合。
- cutter 保存和上传成功，但发布、批量发布和审核 403。
- cutter 显式传 `reviewRequired=false` 仍创建 `pending_review`。
- reviewer 使用安全 DTO 审批，账号、登录、Cookie、私信和发布 403。
- operator 只能操作 scope 内账号。
- viewer 不能读取审计、凭据状态、授权会话和任务。
- `/api/v2` 与 `/v2` 状态码和 JSON 一致。
- 空 scope、未知角色启动失败且认证返回空身份。
- 全部禁止请求前后，数据库和业务文件无一致副作用。
- 所有受保护 Flask 路由都命中权限策略。

这不代表真实 Web/executor 进程、真实令牌文件热更新和多浏览器环境已经验收。

### TEST-04：权限黑盒回归已转成可执行测试

以下九条已转成可执行测试设计；当前自动实现见 `tests/test_permission_matrix.py`。

| # | 前置条件 | 请求 | 期望 HTTP | 期望业务副作用 | 执行方式 |
| --- | --- | --- | --- | --- | --- |
| 1 | cutter 令牌，scope 含测试账号；内容库、任务库可写 | `POST /api/v2/posts`；`POST /uploadSave`；随后 `POST /postVideo`、`POST /postVideoBatch`、`POST /review/1/approve` | 保存和上传 200；后三项均 403 | `contents` 新增一行，`created_by=剪辑用户`、`review_required=1`；`file_records` 新增一行且 `uploaded_by` 正确；`videoFile` 新增对应文件；禁止请求不新增 job、不改账号或审核字段 | 自动；真实令牌人工复验 |
| 2 | cutter 已保存含平台变体的草稿；目标账号在 scope 内；executor 暂停 | `POST /api/v2/posts/{id}/submit`，请求体显式含 `reviewRequired:false` | 200，响应 `reviewRequired=true` | `jobs` 新增任务且 `status=pending_review`、`review_required=1`；变体状态为 `pending_review`；没有 worker 认领或进入 publish 执行 | 自动；真实进程人工复验 |
| 3 | reviewer 令牌；一个 scope 内 `pending_review` job；executor 暂停 | `GET /review/pending`；`POST /review/{id}/approve`；再请求账号、登录、Cookie、私信、发布接口 | 审核列表和审批 200；其余业务接口 403 | 响应不含 `lease_token`、`claim_token`、`heartbeat_token`、`progress`、`result`；job 变为 `pending` 或 `scheduled`，`reviewed_by/review_decision/reviewed_at` 有值；`audit_logs` 新增 `review.approve`；账号、授权会话和私信表不变 | 自动；真实进程人工复验 |
| 4 | operator 令牌；一个 scope 内账号和一个 scope 外账号 | `/postVideo` 分别提交两个账号 | 范围内 200；范围外 403 | 只新增范围内 job；越权请求不新增 job，账号表不变；发布执行租约和远端动作不启动于越权请求 | 自动；真实进程人工复验 |
| 5 | viewer 令牌 | `GET /api/v2/audit/logs`、`/api/v2/credentials/status`、`/api/v2/auth-sessions`、`/jobs`；`GET /api/v2/session` | 四个业务读取接口 403；session 200 | session 返回空业务权限；不返回审计、凭据、授权会话或任务数据；数据库和文件哈希不变 | 自动；真实令牌人工复验 |
| 6 | operator 令牌；`/api/v2` 路由已注册 | `GET /api/v2/accounts` 与 `GET /v2/accounts`；`POST /api/v2/accounts/1/check` 与 `POST /v2/accounts/1/check` | GET 两侧同码且同 JSON；POST 两侧均 403 | 账号健康检查、账号状态、审计和任务均无新增；证明别名不获得不同权限 | 自动；真实进程人工复验 |
| 7 | 令牌文件含非管理员条目但无 scope | 启动服务；另直接调用认证解析 | 不产生 HTTP：启动失败；绕过启动时认证返回空，对应请求为 401 | 不绑定身份，不产生全账号范围；主库、任务库和文件不变 | 自动；发布前用真实配置文件演练 |
| 8 | 令牌文件含未知角色 `editor` 或拼错角色 | 启动服务；另直接调用认证解析 | 不产生 HTTP：启动失败；绕过启动时认证返回空，对应请求为 401 | 不降级为 viewer，不生成受限登录身份；数据库和文件不变 | 自动；发布前用真实配置文件演练 |
| 9 | viewer 令牌；权限拒绝发生在路由前 | 依次请求审计、授权会话、凭据、发布、删素材、素材清理、DM 回复、审核通过 | 全部 403 | 对主库、任务库、运营库全部表做前后快照；对 `videoFile`、`cookiesFile`、`dm_media` 做 SHA-256 前后快照；两组快照必须完全一致 | 自动；关键生产验收再对真实进程执行 |

自动化与人工边界：

- 九条都可以用 Flask test client 自动化，现有测试已覆盖路由、权限和数据库副作用。
- 用例 1、2、3、4、6 还必须在真实 Web 进程中用独立用户令牌、独立 SQLite 文件和停止 executor 的受控环境人工复验，证明 HTTP 栈、环境变量和实际令牌文件没有偏差。
- 用例 7、8 必须用真实令牌文件演练启动失败，证明部署编排没有吞掉退出码或继续启动健康实例。
- 用例 9 的“无副作用”证据来自前后快照及哈希对比，不能只看 403 响应。

## 5. 性能与容量

### PERF-01：全部性能基线未测

当前没有可核实的以下数据：

- 单次发布 P50/P95/max。
- 高峰吞吐和队列积压。
- 成功率、uncertain 比例。
- 各平台、账号、视频大小分组耗时。
- all 模式与 split 模式的对照结果。
- Chromium 分片 RSS、executor 容器内存峰值。

影响：不能宣称任何性能提升百分比，也不能证明 split 或共享浏览器在当前生产负载下带来收益。

### PERF-02：现有采集需要区分等待、浏览器和平台终态

建议区分：

- queue wait：真正开始执行前的排队时间。
- start to submitted_at：浏览器上传和平台提交阶段。
- submitted 到远端终态：平台审核、转码、发布延迟。
- end-to-end only success：用户从任务创建到成功完成的总时间。
- terminal P95：成功、失败、uncertain 等全部终态的尾延迟。
- throughput：时间窗内终态 item 数除以小时数。

输出必须附带样本量、平台、账号数、素材大小、执行模式和失败/unknown 数。

### PERF-03：单机最小压测仍待执行

仓库验收文档建议的首轮方案：

- 账号：3 个专用测试账号，每个平台 1 个。
- 任务：30 个独立 item，每平台 10 条。
- 执行：2 个 publish worker、最多 4 个浏览器上下文、2 个共享 Chromium 分片。
- 时长：预热 5 分钟，稳定运行至少 60 分钟，等待远端终态或明确超时。
- 重复：至少 2 次，建议 3 次。
- 采样：每 10 秒记录容器和 Chromium 进程树内存。

### PERF-04：对外性能口径

基线建立前可以表述：

> 已完成任务队列、账号互斥、执行器分离和浏览器分片等代码加固；真实平台发布、生产重启和性能尚未验收，目前没有可对外声明的吞吐或延迟提升百分比。

不能表述：

- 性能已提升 X%。
- split 模式吞吐更高。
- 共享浏览器已经扩大一倍容量。
- 账号隔离已经避免所有 OOM。

## 6. 真实 bad case 与产品背景

### BAD-01：尚未获得可核实的真实运营 bad case

对话中没有提供满足以下字段的 3 到 5 个真实案例，因此不能把代码审查发现改写成事故。

记录模板：

- 操作人及角色。
- 操作时间和账号。
- 做了什么。
- 期望结果。
- 实际结果和返回码。
- 截图、日志、job/item ID。
- 发生频率。
- 是否可复现、是否产生重复发布或数据损失。

### BAD-02：权限代码边界已具备，但尚不能宣称企业交付已完成

本轮已关闭的代码阻塞：

- 已有独立 cutter、reviewer、operator、admin 权限边界。
- 账号登录、Cookie、凭据、健康检查和系统运维已收口到管理员；运营和剪辑只获得业务所需的脱敏账号目录。
- 审核人、内容创建人和素材上传人已落库。
- 前端已读取当前 session 的角色和权限点。
- 九条直接 API 黑盒已有 Flask 自动回归，并验证禁止请求无数据库和文件副作用。

仍不能正式宣布交付的原因：

- 上述黑盒尚未在真实 Web/executor 进程、真实用户令牌和生产旧库副本上完成人工验收。
- 真实三平台发布、二维码登录跨重启、强杀恢复、Docker split 和性能基线仍未验证。
- 系统仍为单机单进程事实源，不能把当前权限闭环扩大解释为多机企业级容量已完成。

## 7. 最小上线顺序

### P0：开放给剪辑和运营之前

1. **代码完成**：固定 cutter、reviewer、operator、admin 四个有效角色；未知角色失败关闭。
2. **代码完成**：非管理员必须配置显式 scope；空 scope 仅允许管理员。
3. **代码完成**：统一规范化 /api/v2 和 /v2，所有路由读写都显式登记权限。
4. **代码完成**：拆分 content.edit、material.write、review.decide、publish.execute、message.operate、account.manage、team.manage、audit.read。
5. **代码完成**：剪辑提交时由后端强制审核，请求中的 reviewRequired=false 被忽略。
6. **代码完成**：账号登录、凭据、代理、健康检查、删除和团队审计仅管理员可用。
7. **代码完成**：审核列表返回专用安全 DTO，不返回租约等执行器内部字段。
8. **代码完成**：审核、上传和内容创建记录操作人，并写对应审计事件。
9. **自动回归完成，真实进程待做**：用真实用户令牌在停止 executor 的受控 Web 进程重复九条黑盒矩阵。
10. **发布配置待做**：使用每人独立令牌，不共享全局管理员令牌；令牌文件不得提交。

### P0/P1：企业级发布验收之前

1. 三平台各完成至少一条真实私密/草稿发布和远端终态回查。
2. 三平台完成真实二维码登录和跨重启保持验证。
3. 在可用 Docker 环境实际启动 split，验证健康、角色、日志和持久化目录。
4. 完成 Web/executor 强杀恢复和 Chromium 分片崩溃演练。
5. 用生产旧库脱敏副本验证新增 `reviewed_by`、`uploaded_by` 和审核索引的 schema 兼容。
6. **本轮已完成当前工作树全量测试 721 passed；发布前在最终提交上再跑一次并保存输出。**
7. 建立 all 与 split 的同口径性能基线。

### P2：规模阶段

1. leader election。
2. 多 executor。
3. 跨主机任务事实存储。
4. 横向扩容和容量调度。

## 8. 可以直接作为验收结论的表述

当前可用的准确结论：

> FlowOps 已在当前工作树完成四角色最小权限模型的代码闭环：cutter、reviewer、operator、admin 权限拆分，未知角色和非管理员空 scope 失败关闭，/v2 别名统一授权，审核服务端强制，账号与凭据控制面收口到管理员，审核人、内容创建人和素材上传人落库。后端全量 721 个测试、Ruff、py_compile、前端 lint、5 个 Vitest 和 production build 均通过。真实 Web/executor 进程中的权限黑盒、生产旧库迁移、三平台真实发布、二维码跨重启、Docker split、强杀/分片崩溃恢复和性能基线仍未验证。因此可以进入受控企业验收，但尚不能宣称企业级权限和真实发布已经完成生产验收；当前也没有任何可声明的性能提升百分比。
