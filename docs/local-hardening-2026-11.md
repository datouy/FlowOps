# 本地部署加固说明（2026-11）

本轮优化以“本地单用户、单进程”为前提，不引入登录会话、多租户或分布式执行器。

## 访问与接口

- 未配置 Token 时保持零配置本地模式。
- 配置用户 Token 后，写接口按 `publish / message / account / team` 权限点统一拦截。
- `/health` 与 `/health/ready` 提供给 Docker/Nginx 无 Token 探测；详细信息仍在受鉴权的 `/api/v2/system/health`。
- `/dm/inbound` 不再要求后台管理 Token，改用独立接入密钥或 HMAC。

Webhook HMAC 算法：

```text
signature = hex(hmac_sha256(secret, timestamp + "." + raw_request_body))
headers:
  X-YunCe-Timestamp: <unix-seconds>
  X-YunCe-Signature: <signature>
```

时间戳允许误差 300 秒；单次最多 200 条消息，请求体上限 2MB。

## 异步任务

以下操作返回 HTTP 202 和任务 ID：

- AI 文案与内容包
- 全量账号检查
- 评论同步
- 私信发送与 AI 草稿

通过 `GET /api/v2/tasks/<id>` 查询状态，通过 `POST /api/v2/tasks/<id>/cancel` 请求取消。任务保存在 `db/async_tasks.db`，服务重启后会恢复未完成任务。

## 安全边界

- 上传仅允许常见图片/视频扩展名，并校验文件头；HTML、SVG、脚本等主动内容会拒绝。
- 预览只服务白名单媒体，响应带 `nosniff`、`Referrer-Policy`、CSP 等安全头。
- AI 只允许 HTTPS 端点；仅本机回环地址可使用 HTTP。
- AI 调用记录在 `db/ai_usage.db`，支持日配额、用量和可选成本统计。
- 远端选择器适配包必须 HMAC 签名，并带 `X-FlowOps-Version` 后原子替换。

## 数据保留

每日维护只自动清理可重建或过期的派生数据：

- `daily_metrics`
- `ai_usage_log`
- `job_events`
- `account_patrol`
- 过期 AI 内容包缓存

业务素材、私信和发布历史不会自动删除。可通过 `FLOWOPS_MEDIA_QUOTA_GB` 设置媒体目录上限，通过 `/api/v2/storage/overview` 查看使用情况。

## 验证

```bash
cd social-auto-upload-main
python -m pytest -q
ruff check .

cd sau_frontend
npm test
npm run lint
npm run build
npm run e2e
```
