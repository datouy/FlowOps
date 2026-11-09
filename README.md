<div align="center">

# 云策 YunCe

**国内新媒体运营一体化工作台**

集中管理多平台账号 · AI 内容生产 · 统一私信收件箱 · 线索自动提取与优先跟进

[![简体中文](https://img.shields.io/badge/语言-简体中文-FF4D4F?style=for-the-badge&logo=readme&logoColor=white)](./README.md)
[![English](https://img.shields.io/badge/Lang-English-1677FF?style=for-the-badge&logo=readme&logoColor=white)](./README.en.md)

[![License](https://img.shields.io/badge/License-MIT-yellow.svg?style=flat-square)](./LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![Node.js](https://img.shields.io/badge/Node.js-18%2B-339933?style=flat-square&logo=nodedotjs&logoColor=white)](https://nodejs.org/)
[![Vue](https://img.shields.io/badge/Vue-3-4FC08D?style=flat-square&logo=vuedotjs&logoColor=white)](https://vuejs.org/)
[![Flask](https://img.shields.io/badge/Flask-API-000000?style=flat-square&logo=flask&logoColor=white)](https://flask.palletsprojects.com/)
[![Playwright](https://img.shields.io/badge/Playwright-Automation-2EAD33?style=flat-square&logo=playwright&logoColor=white)](https://playwright.dev/)
[![Windows](https://img.shields.io/badge/OS-Windows%2010%2F11-0078D6?style=flat-square&logo=windows&logoColor=white)](https://www.microsoft.com/windows)

[![抖音](https://img.shields.io/badge/抖音-FE2C55?style=flat-square&logo=tiktok&logoColor=white)](#-支持平台)
[![快手](https://img.shields.io/badge/快手-FF4906?style=flat-square&logo=kuaishou&logoColor=white)](#-支持平台)
[![视频号](https://img.shields.io/badge/视频号-07C160?style=flat-square&logo=wechat&logoColor=white)](#-支持平台)
[![小红书](https://img.shields.io/badge/小红书-FF2442?style=flat-square&logo=xiaohongshu&logoColor=white)](#-支持平台)
[![微博](https://img.shields.io/badge/微博-E6162D?style=flat-square&logo=sinaweibo&logoColor=white)](#-支持平台)
[![百家号](https://img.shields.io/badge/百家号-2932E1?style=flat-square&logo=baidu&logoColor=white)](#-支持平台)

</div>

---

> 集中管理抖音、小红书、视频号账号；一次生成各平台内容；所有私信进入一个收件箱；自动提取出行日期、人数和预算；每天告诉运营哪些询盘该优先跟进。

## 🎯 项目定位

云策是一个面向国内新媒体运营的开箱即用工具，覆盖抖音、快手、视频号、小红书、微博、百家号六个平台。它不是单一的"群发脚本"，而是围绕运营日常工作流，提供从内容生产到发布复盘的一体化工作台。

本项目基于开源项目 **[dreammis/social-auto-upload](https://github.com/dreammis/social-auto-upload)** 进行二次开发。原项目提供了成熟的多平台浏览器自动化上传能力，云策在此基础上新增了 AI 文案、内容包、合规检查、任务队列、审核流、多平台统一私信、数据回流、运营周报等运营功能。

## 📌 二次开发声明

- 上游项目：`dreammis/social-auto-upload`
- 上游地址：https://github.com/dreammis/social-auto-upload
- 上游许可证：MIT License
- 本项目许可证：MIT License（保留原版权声明）
- 原项目版权：Copyright (c) 2023 dreammis
- 二次开发部分：Copyright (c) 2026 YunCe contributors

本项目与上游作者没有隶属关系。二次开发部分的问题和功能建议请提交到本仓库。

## ✨ 新增功能

### ✍️ 内容生产

- **AI 文案生成**：按平台风格生成标题、正文、话题和发布建议；默认使用通用模板，并支持餐饮美食、美妆个护、教育培训、健身运动、本地生活、电商零售、房产家居等常见行业及自定义行业。
- **多平台内容包**：一次输入行业和主题，生成 6 个平台的差异化内容；支持后台任务、实时进度、24 小时缓存，AI 接口不可用时自动使用本地模板兜底。
- **自动封面**：从视频首帧生成 3:4 和 9:16 封面。
- **发布前合规检查**：检查标题长度、绝对化用语、联系方式、外链、话题数量、素材和封面要求。

### 🚀 发布与审核

- **后台任务队列**：发布立即入队，不阻塞页面。
- **失败自动重试**：失败自动重试一次，并记录完整失败原因。
- **发布记录**：记录平台、账号、素材、状态、错误原因和时间。
- **定时发布与发布日历**：按天查看排期，支持周历视图。
- **智能排期建议**：根据历史作品表现推荐发布时间，并提示撞车。
- **团队审核流**：内容可先提交审核，通过后才进入发布队列。

### 📊 账号与数据

- **多账号管理**：支持扫码登录、登录态保存和有效性巡检。
- **账号过期提醒**：登录失效后在发布记录页和今日待办中提示。
- **数据回流**：抓取平台后台作品播放、点赞、评论等数据。
- **手工关联**：自动匹配失败时，可把平台作品手动关联到发布记录。
- **运营周报**：汇总发布量、成功失败、播放、互动和最佳内容，支持复制和 CSV 导出。
- **异常通知**：发布失败、账号过期、AI 内容包失败可推送到企业微信机器人、飞书机器人或通用 webhook。

### 💬 多平台统一私信

- **统一收件箱**：集中查看抖音、小红书、视频号私信，可按平台、账号和客户筛选；每条会话带来源账号，多个账号同时运营时不会混淆消息归属。
- **多账号隔离**：同一平台可登录多个账号。账号 Cookie、同步游标、会话、消息和线索按账号分别保存；重新登录已有账号会更新原记录，不会重复新增。
- **全账号同步**：同步设置支持多选账号；不勾选具体账号时，默认同步当前平台下所有登录有效的账号。每个平台可分别设置同步间隔、单次会话数量和回补最近多少天的聊天记录（默认 7 天，最大 30 天）。
- **抖音私信同步**：使用本机 Playwright 和已保存的登录态读取创作者中心网页版私信；不点开会话、不标记已读，自动跳过群聊、自己发出的消息、视频/分享卡片、撤回消息和平台占位提示。
- **小红书主站私信同步**：读取小红书主站 `www.xiaohongshu.com/chat` 的真实私信会话，而不是专业号后台的消息池；普通用户小号发来的私信也应从这里进入。
- **视频号私信同步**：通过视频号助手登录态读取会话和消息，支持多账号分别同步。
- **工作台直接回复**：抖音、小红书、视频号均可基于本机已登录账号直接发送文本回复，并把发送结果回写为「已发送」或失败原因。
- **自动回复草稿保护**：三个平台的定时同步命中自动回复规则后，只生成「待人工发送」草稿，不会后台静默真实发送；需人工确认后再发送。
- **意向识别与线索**：识别购买意向并提取规格、预算、时间、公司、联系方式等线索；支持自定义关键词规则、标签、跟进阶段、备注与 CSV 导出。
- **自动回复引擎**：按客群、关键词、平台和优先级匹配规则，支持固定模板与 AI 智能回复两种模式，以及冷却时间、每日上限、发送时段、评论开关、转人工词、AI 兜底、AI 每日用量上限与单客只回一次。
- **私信 Webhook 接入**：通过 `/dm/inbound` 接收外部私信事件，支持接入密钥、消息幂等去重和发送结果回写。
- **今日优先跟进**：按意向、未回复消息、跟进阶段、留资完整度和最近咨询时间给线索评分，直接提示"立即联系 / 优先跟进 / 今日跟进"和下一步动作。
- **今日运营待办**：汇总优先线索、待回复、失败发布、待审核、定时任务、账号过期和数据同步提醒。
- **素材标签与去重**：按标签筛选素材，MD5 检测重复文件，显示发布状态，并支持按未使用天数清理。
- **日志轮转**：按 10MB 自动轮转，保留 14 天。

### 🔄 私信同步与回复

#### 使用步骤

1. 在「账号管理」中扫码登录。每个账号必须使用**实际接收私信的那个平台账号**登录。
2. 打开「私信中心 → 自动回复规则」，在每个平台卡片中开启定时同步，按需多选账号，设置同步间隔和回补天数（默认近 7 天）。
3. 先点一次「立即同步」，再进入统一收件箱，用平台和账号筛选确认消息来源。
4. 打开会话后可直接输入文本并发送；发送按钮只使用所选会话对应的来源账号。

同步设置不勾选账号时，会同步该平台下所有带有效 Cookie 的账号。多账号消息按账号隔离，统一收件箱会显示消息来自哪个平台账号。

#### 平台私信能力

| 平台 | 消息来源 | 登录要求 | 同步 | 工作台直发 |
| --- | --- | --- | --- | --- |
| 抖音 | 抖音创作者中心私信 | 使用接收私信的抖音账号扫码 | 支持 | 支持 |
| 小红书 | 小红书主站私信 `/chat` | 使用接收私信的小红书账号在主站扫码 | 支持 | 支持 |
| 视频号 | 视频号助手私信 | 使用接收私信的微信/视频号账号扫码 | 支持 | 支持 |

三个平台的同步窗口由「同步范围」里的天数决定，**默认回补最近 7 天**（可设 1–30 天，关掉时间范围则尽量拉取平台能给出的全部历史），并对重复消息去重。窗口调大后会自动清掉"这份历史已同步过"的标记，让下一轮重新回补更早的会话。同步结果会显示账号、扫描数、入库数、重复数和失败原因。

#### 聊天记录持久化

聊天记录保存在本机 SQLite（`social-auto-upload-main/db/ops_center.db`），重启项目不会丢失，具体保障：

- **连接即用即关**：每次读写结束后立即提交并关闭连接，避免连接泄漏把 WAL 锁死（旧版本因此会丢写入）。
- **WAL 合并**：启动、退出和每 10 分钟做一次 checkpoint，把已提交的聊天记录合并回主库文件；即使进程被强制结束，也不会出现"只存在于 `-wal` 里"的记录。
- **每天自动备份**：启动时在 `social-auto-upload-main/db/backups/` 生成当天的一份一致性备份，保留最近 7 份。
- **自检接口**：`GET /dm/storage/health` 返回库文件与 WAL 体积、完整性检查、消息条数、各平台最早/最晚消息时间和备份列表；`POST /dm/storage/checkpoint`、`POST /dm/storage/backup` 可手动合并与备份。

> 备份请连同 `db/` 整个目录一起复制；如果只想拷单个文件，先调用一次 checkpoint 接口。

#### 小红书登录与会话排查

- **必须登录实际接收私信的账号。** 小红书主站私信和专业号后台的消息池不是同一套数据，用错账号或只看专业号后台会看不到普通用户发来的消息。
- 登录会同时验证主站用户信息、主站私信会话列表和创作服务平台登录态。任一环节失效，都需要在账号管理中重新扫码。
- 小红书主站匿名扫码在无头模式下可能触发 `300012 IP 存在风险`，登录时必须使用可见的 Chrome 和有头浏览器；同步时也建议保持稳定的本机网络环境，不要频繁切换出口 IP。
- 重新登录后先点「立即同步」，再检查同步设置中的账号是否选中、账号状态是否正常。平台未展示、被折叠或受陌生人私信限制的会话无法被读取。
- 视频号若登录后仍无新消息，先确认登录的是接收私信的账号，并执行一次「立即同步」；只刷新前端页面不会重新拉取平台会话。

### 🤖 自动回复与 Webhook

在「私信中心 → 自动回复规则」里设置总开关、发送时段、关键词规则和连接器。外部系统也可以把新私信 POST 到：

```text
http://127.0.0.1:5409/dm/inbound
```

示例：

```json
{
  "platform": "抖音",
  "account": "你的账号名",
  "external_user_id": "平台内稳定用户ID",
  "external_message_id": "平台内消息ID",
  "author": "王姐",
  "msg_type": "dm",
  "content": "这个产品有哪些规格？多少钱？可以预约吗？",
  "source_content": "产品介绍视频"
}
```

`external_user_id` 用于合并同一客户，`external_message_id` 用于防止重复回复。若在设置中填写了接入密钥，请求需带 `X-YunCe-Secret` 请求头。

发送行为分为三种：

- **工作台手动回复**：通过本机登录态直接发送到抖音、小红书或视频号，并回写发送结果。
- **定时同步触发的自动回复**：固定生成「待人工发送」，不会自动真实发送。
- **Webhook 触发的自动回复**：按自动回复规则、发送开关和账号登录态处理；未满足发送条件时保存为「待人工发送」。

回复规则支持两种生成方式：**固定模板**（支持 `{nickname}`、`{origin}`、`{travelers}` 等变量）和 **AI 智能回复**（按命中关键词调用 AI，可附加回复口径，例如"不要提价格，先要联系方式"）。未命中任何规则且开启 AI 兜底时，也会用 AI 生成回复。AI 生成会把已沉淀的线索（人数、出发地、日期、预算、来源内容）带入提示词，避免重复提问；AI 模式下生成失败会回退到规则里的固定文案。AI 生成的回复会做一次安全过滤，疑似编造手机号、微信号或绝对化承诺（如"保证""全网最低"）时自动改标「需人工」并保留草稿，不会直接发送。设置中的「AI 每日上限」按天统计全局 AI 回复数量，用于控制接口费用。

## 🧩 支持平台

| 平台 | 登录 | 视频发布 | 图文/笔记 | 定时发布 | 数据回流 | 私信同步 | 工作台直发 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 抖音 | 支持 | 支持 | 支持 | 支持 | 支持 | 支持 | 支持 |
| 快手 | 支持 | 支持 | 支持 | 支持 | 支持 | - | - |
| 视频号 | 支持 | 支持 | - | 支持 | 支持 | 支持 | 支持 |
| 小红书 | 支持 | 支持 | 支持 | 支持 | 支持 | 支持 | 支持 |
| 微博 | 支持 | 支持 | - | 平台能力为准 | - | - | - |
| 百家号 | 支持 | 支持 | - | 平台能力为准 | - | - | - |

本地私信同步和工作台直发目前覆盖抖音、小红书、视频号。平台页面和风控策略会变化，具体可用性以实际运行结果为准。

## ⚡ 快速开始（Windows）

### 📋 环境要求

- Python 3.10+（建议 3.12）
- Node.js 18+
- Google Chrome（建议保持最新版，小红书登录和工作台发送会使用本机浏览器）
- Windows 10/11

### 📦 安装

```powershell
python -m venv venv
venv\Scripts\pip install -r social-auto-upload-main\requirements.txt
venv\Scripts\python -m playwright install chromium
venv\Scripts\patchright install chromium

cd social-auto-upload-main\sau_frontend
npm install
cd ..\..
```

`requirements.txt` 只声明直接依赖；如需复现当前已验证的完整环境，可改用
`social-auto-upload-main\requirements.lock`。开发与测试依赖见
`social-auto-upload-main\requirements-dev.txt`。

### 🔑 配置 AI

复制配置模板并填入自己的 API Key：

```powershell
Copy-Item social-auto-upload-main\.env.example social-auto-upload-main\.env
notepad social-auto-upload-main\.env
```

`.env` 已被 `.gitignore` 排除，不会进入仓库。**请务必设置凭据加密主密钥**（不设置会用系统凭据库兜底，但换机器后无法解密已保存的账号凭据）：

```text
FLOWOPS_SECRET_KEY=一串足够长的随机口令
```

异常通知可在同一文件中配置：

```text
OPS_WEBHOOK_URL=你的企业微信/飞书机器人地址
OPS_WEBHOOK_TYPE=auto
```

### 🏃 启动

双击根目录的 `start.bat`。脚本会自动：

1. 检查 5409 / 5173 端口；
2. 启动 Python 后端；
3. 启动 Vue 前端；
4. 打开 http://localhost:5173。

也可以分别运行 `run_backend.bat` 和 `run_frontend.bat`。

## 📁 目录结构

```text
FlowOps/
├─ start.bat                     # 一键启动
├─ run_backend.bat               # 单独启动后端
├─ run_frontend.bat              # 单独启动前端
├─ deploy/                       # 服务器部署脚本与说明（Docker / 裸机 / Nginx）
├─ social-auto-upload-main/      # 基于上游二次开发的主程序
│  ├─ sau_backend.py             # Flask API（waitress 承载生产流量）
│  ├─ publish_jobs.py            # 发布队列、重试、巡检
│  ├─ stats_reflow.py            # 平台数据回流
│  ├─ ops_center.py              # 消息中心、待办、周报
│  ├─ dm_ops.py                  # 私信会话、自动回复、线索与 Webhook
│  ├─ dm_platform_common.py      # 多平台账号解析、Cookie 与时间工具
│  ├─ douyin_dm_sync.py          # 抖音多账号私信同步
│  ├─ xiaohongshu_dm.py          # 小红书主站私信读取与发送
│  ├─ xiaohongshu_dm_sync.py     # 小红书私信定时同步
│  ├─ channels_dm.py             # 视频号私信读取与发送
│  ├─ channels_dm_sync.py        # 视频号私信定时同步
│  ├─ platform_reply.py          # 三平台统一回复发送器
│  ├─ ai_content.py              # AI 文案与内容包
│  ├─ compliance.py              # 发布前合规检查（规则与平台注册表同源）
│  ├─ myUtils/platform_registry.py  # 平台适配器注册表（发布分发单一事实来源）
│  ├─ schedule_ops.py            # 发布日历
│  ├─ schedule_suggest.py        # 智能排期建议
│  ├─ material_ops.py            # 素材标签与去重
│  ├─ media_ops.py               # 封面生成
│  ├─ sau_frontend/              # Vue 3 + Element Plus 前端
│  └─ uploader/                  # 上游各平台浏览器自动化实现
└─ README.md
```

## 🚢 服务器部署

除了 Windows 本机运行，仓库还提供一套服务器部署脚本：

- `deploy/docker-compose.yml` + `deploy/Dockerfile`：容器化部署；
- `deploy/install-baremetal.sh`：Linux 裸机一键安装（自动生成访问令牌并写入 systemd 服务）；
- `deploy/nginx-yunce.conf`：Nginx 反向代理参考配置；
- `deploy/yunce.service`：systemd 服务模板；
- `deploy/backup.sh`：数据库每日备份脚本；
- `deploy/smoke.sh`：部署后冒烟测试脚本。

完整步骤和参数说明见 [deploy/README.md](./deploy/README.md)。生产部署使用 waitress 承载后端流量（仍在单进程模型内），不再是 Flask 开发服务器。

## 🔒 安全说明

- 后端和前端默认只监听 `127.0.0.1`，同局域网设备无法直接打开工作台。确需远程访问时，必须在 `.env` 中同时设置 `YUNCE_HOST`、`YUNCE_ALLOW_REMOTE=1` 和 `YUNCE_ACCESS_TOKEN`；缺少令牌时后端会拒绝启动。前端请求会自动携带 `X-YunCe-Token`。
- 设置访问令牌后，在浏览器控制台执行 `localStorage.setItem('yunce_access_token', '你的令牌')` 即可让前端携带令牌；不要把令牌写进 `VITE_*` 变量，否则会被打包进前端代码。
- `.env`、数据库、Cookie、日志、上传素材、虚拟环境和前端构建产物均不进入仓库。
- **账号登录态加密存储**：`cookiesFile/` 下的 Playwright `storage_state` 用 **AES-256-GCM** 加密落盘（文件后缀 `.enc`），不再有明文 cookie 文件。密文与账号绑定（AAD），跨账号搬运、篡改、用错主密钥都会被直接拒绝。
  - 主密钥来源优先级：`FLOWOPS_SECRET_KEY` 环境变量 → 系统凭据库（Windows DPAPI / macOS Keychain / Linux keyring）→ 本机降级文件 `db/.master_key`。
  - **生产环境请设置 `FLOWOPS_SECRET_KEY`**：换机器、重装系统或清理 `db/` 目录后，降级密钥会丢，已保存的凭据将无法解密，只能重新扫码登录。
  - 迁移老凭据：`POST /api/v2/credentials/migrate`（默认 dry-run，只报告不改动；加 `confirm: true` 才真正执行并删除明文）。查看状态：`GET /api/v2/credentials/status`。
- `cookiesFile/` 中的登录凭证仍是加密 JSON，请勿复制、提交或通过网盘共享；工作台默认仅限本机访问正是为了避免这些凭证被同网段设备滥用。
- 日志与接口响应里的 cookie 值统一脱敏（`sessionid` / `SESSDATA` / `BDUSS` 等敏感项完全遮蔽）。
- 数据库首次启动会自动建表；账号登录态请在本机重新扫码生成。
- 请仅操作自己拥有或获授权的账号。
- 本项目通过浏览器自动化模拟人工操作，平台没有面向个人开放的统一发布或私信 API。请遵守各平台用户协议，控制发布和回复频率。
- 工作台直接回复会使用真实账号向真实用户发送消息。上线前请用小号验证内容和目标会话，避免误发到其他账号或客户。
- 小红书主站登录必须使用可见浏览器完成；遇到 IP 或账号风控时，请停止高频操作并人工检查，不要尝试绕过平台限制。
- AI 生成内容需要人工复核，尤其是价格、库存、效果、服务承诺和广告用语。

## ⚠️ 已知边界

- 平台页面改版可能导致自动化选择器失效，需要跟随上游或本仓库更新。
- 微博、百家号的数据回流和定时能力受平台后台限制。
- 三个平台只读取网页端实际展示的会话；陌生人私信限制、折叠会话、撤回消息和非文本消息可能无法同步。
- 小红书主站私信与专业号后台消息池不同；必须登录实际接收私信的账号，并保持主站与创作服务平台的登录态同时有效。
- 工作台直发目前只支持文本回复，不支持图片、文件、语音和表情包；发送失败会在会话中回写原因。
- 定时同步产生的自动回复只有「待人工发送」草稿，不会自动真实发送；Webhook 的发送行为仍受规则和账号登录态限制。
- 真实发布、扫码登录和私信发送需要在你的本机账号环境中验证，平台风控或页面改版可能导致临时不可用。

## 🔗 仓库

- Gitee：https://gitee.com/tokyi/flowops
- GitHub：https://github.com/datouy/FlowOps

## 📄 许可证

本项目遵循 MIT License，详见 [LICENSE](./LICENSE)。原项目版权归 dreammis 所有。
