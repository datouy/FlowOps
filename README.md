# 汇流运营台 FlowOps

> 多平台内容运营中枢：把账号、素材、文案、审核、发布、消息和数据复盘汇到一处。

## 项目定位

FlowOps 是一个面向国内新媒体运营的开箱即用工具，覆盖抖音、快手、视频号、小红书、微博、百家号六个平台。它不是单一的“群发脚本”，而是围绕运营日常工作流，提供从内容生产到发布复盘的一体化工作台。

本项目基于开源项目 **[dreammis/social-auto-upload](https://github.com/dreammis/social-auto-upload)** 进行二次开发。原项目提供了成熟的多平台浏览器自动化上传能力，FlowOps 在此基础上新增了 AI 文案、内容包、合规检查、任务队列、审核流、消息中心、数据回流、运营周报等运营功能。

## 二次开发声明

- 上游项目：`dreammis/social-auto-upload`
- 上游地址：https://github.com/dreammis/social-auto-upload
- 上游许可证：MIT License
- 本项目许可证：MIT License（保留原版权声明）
- 原项目版权：Copyright (c) 2023 dreammis
- 二次开发部分：Copyright (c) 2026 FlowOps contributors

本项目与上游作者没有隶属关系。二次开发部分的问题和功能建议请提交到本仓库。

## 新增功能

### 内容生产

- **AI 文案生成**：按平台风格生成标题、正文、话题和发布建议；支持旅行社、餐饮美食、美妆个护、教育培训、健身运动、本地生活、电商零售、房产家居及自定义行业。
- **多平台内容包**：一次输入行业和主题，生成 6 个平台的差异化内容；AI 接口不可用时自动使用本地模板兜底。
- **自动封面**：从视频首帧生成 3:4 和 9:16 封面。
- **发布前合规检查**：检查标题长度、绝对化用语、联系方式、外链、话题数量、素材和封面要求。

### 发布与审核

- **后台任务队列**：发布立即入队，不阻塞页面。
- **失败自动重试**：失败自动重试一次，并记录完整失败原因。
- **发布记录**：记录平台、账号、素材、状态、错误原因和时间。
- **定时发布与发布日历**：按天查看排期，支持周历视图。
- **智能排期建议**：根据历史作品表现推荐发布时间，并提示撞车。
- **团队审核流**：内容可先提交审核，通过后才进入发布队列。

### 账号与数据

- **多账号管理**：支持扫码登录、登录态保存和有效性巡检。
- **账号过期提醒**：登录失效后在发布记录页和今日待办中提示。
- **数据回流**：抓取平台后台作品播放、点赞、评论等数据。
- **手工关联**：自动匹配失败时，可把平台作品手动关联到发布记录。
- **运营周报**：汇总发布量、成功失败、播放、互动和最佳内容，支持复制和 CSV 导出。

### 运营协作

- **统一消息中心**：集中管理评论/私信，自动识别高意向留言，支持快捷回复和 AI 回复草稿。
- **今日运营待办**：汇总待回复、失败发布、待审核、定时任务、账号过期和数据同步提醒。
- **素材标签与去重**：按标签筛选素材，MD5 检测重复文件，显示发布状态。

## 支持平台

| 平台 | 登录 | 视频发布 | 图文/笔记 | 定时发布 | 数据回流 |
| --- | --- | --- | --- | --- | --- |
| 抖音 | 支持 | 支持 | 支持 | 支持 | 支持 |
| 快手 | 支持 | 支持 | 支持 | 支持 | 支持 |
| 视频号 | 支持 | 支持 | - | 支持 | 支持 |
| 小红书 | 支持 | 支持 | 支持 | 支持 | 支持 |
| 微博 | 支持 | 支持 | - | 平台能力为准 | - |
| 百家号 | 支持 | 支持 | - | 平台能力为准 | - |

平台页面和风控策略会变化，具体可用性以实际运行结果为准。

## 快速开始（Windows）

### 环境要求

- Python 3.10+（建议 3.12）
- Node.js 18+
- Windows 10/11

### 安装

```powershell
python -m venv venv
venv\Scripts\pip install -r social-auto-upload-main\requirements.txt
venv\Scripts\python -m playwright install chromium

cd social-auto-upload-main\sau_frontend
npm install
cd ..\..
```

### 配置 AI

复制配置模板并填入自己的 API Key：

```powershell
Copy-Item social-auto-upload-main\.env.example social-auto-upload-main\.env
notepad social-auto-upload-main\.env
```

`.env` 已被 `.gitignore` 排除，不会进入仓库。

### 启动

双击根目录的 `start.bat`。脚本会自动：

1. 检查 5409 / 5173 端口；
2. 启动 Python 后端；
3. 启动 Vue 前端；
4. 打开 http://localhost:5173。

也可以分别运行 `run_backend.bat` 和 `run_frontend.bat`。

## 目录结构

```text
SocialOpsHub/
├─ start.bat                     # 一键启动
├─ run_backend.bat               # 单独启动后端
├─ run_frontend.bat              # 单独启动前端
├─ social-auto-upload-main/      # 基于上游二次开发的主程序
│  ├─ sau_backend.py             # Flask API
│  ├─ publish_jobs.py            # 发布队列、重试、巡检
│  ├─ stats_reflow.py            # 平台数据回流
│  ├─ ops_center.py              # 消息中心、待办、周报
│  ├─ ai_content.py              # AI 文案与内容包
│  ├─ compliance.py              # 发布前合规检查
│  ├─ schedule_ops.py            # 发布日历
│  ├─ schedule_suggest.py        # 智能排期建议
│  ├─ material_ops.py            # 素材标签与去重
│  ├─ media_ops.py               # 封面生成
│  ├─ sau_frontend/              # Vue 3 + Element Plus 前端
│  └─ uploader/                  # 上游各平台浏览器自动化实现
└─ README.md
```

## 安全说明

- `.env`、数据库、Cookie、日志、上传素材、虚拟环境和前端构建产物均不进入仓库。
- 数据库首次启动会自动建表；账号登录态请在本机重新扫码生成。
- 请仅操作自己拥有或获授权的账号。
- 本项目通过浏览器自动化模拟人工操作，平台没有面向个人开放的统一发布 API。请遵守各平台用户协议，控制发布频率。
- AI 生成内容需要人工复核，尤其是价格、行程、承诺和广告用语。

## 已知边界

- 平台页面改版可能导致自动化选择器失效，需要跟随上游或本仓库更新。
- 微博、百家号的数据回流和定时能力受平台后台限制。
- 统一消息中心目前以手动/批量导入和 AI 回复为主，平台评论自动抓取受页面结构和风控影响，暂未默认开启。
- 真实发布、扫码登录需要在你的本机账号环境中验证。

## 仓库

- Gitee：https://gitee.com/tokyi/flowops
- GitHub：https://github.com/datouy/FlowOps

## 许可证

本项目遵循 MIT License，详见 [LICENSE](./LICENSE)。原项目版权归 dreammis 所有。

