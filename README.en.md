<div align="center">

# 云策 YunCe

**All-in-One Social Media Operations Workbench for China's Domestic Platforms**

Multi-account management · AI content generation · Unified DM inbox · Lead extraction & priority follow-up

[![简体中文](https://img.shields.io/badge/语言-简体中文-FF4D4F?style=for-the-badge&logo=readme&logoColor=white)](./README.md)
[![English](https://img.shields.io/badge/Lang-English-1677FF?style=for-the-badge&logo=readme&logoColor=white)](./README.en.md)

[![License](https://img.shields.io/badge/License-MIT-yellow.svg?style=flat-square)](./LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![Node.js](https://img.shields.io/badge/Node.js-18%2B-339933?style=flat-square&logo=nodedotjs&logoColor=white)](https://nodejs.org/)
[![Vue](https://img.shields.io/badge/Vue-3-4FC08D?style=flat-square&logo=vuedotjs&logoColor=white)](https://vuejs.org/)
[![Flask](https://img.shields.io/badge/Flask-API-000000?style=flat-square&logo=flask&logoColor=white)](https://flask.palletsprojects.com/)
[![Playwright](https://img.shields.io/badge/Playwright-Automation-2EAD33?style=flat-square&logo=playwright&logoColor=white)](https://playwright.dev/)
[![Windows](https://img.shields.io/badge/OS-Windows%2010%2F11-0078D6?style=flat-square&logo=windows&logoColor=white)](https://www.microsoft.com/windows)

[![Douyin](https://img.shields.io/badge/Douyin-FE2C55?style=flat-square&logo=tiktok&logoColor=white)](#-supported-platforms)
[![Kuaishou](https://img.shields.io/badge/Kuaishou-FF4906?style=flat-square&logo=kuaishou&logoColor=white)](#-supported-platforms)
[![WeChat Channels](https://img.shields.io/badge/Channels-07C160?style=flat-square&logo=wechat&logoColor=white)](#-supported-platforms)
[![Xiaohongshu](https://img.shields.io/badge/Xiaohongshu-FF2442?style=flat-square&logo=xiaohongshu&logoColor=white)](#-supported-platforms)
[![Weibo](https://img.shields.io/badge/Weibo-E6162D?style=flat-square&logo=sinaweibo&logoColor=white)](#-supported-platforms)
[![Baijiahao](https://img.shields.io/badge/Baijiahao-2932E1?style=flat-square&logo=baidu&logoColor=white)](#-supported-platforms)

</div>

---

> Centrally manage Douyin, Xiaohongshu, and WeChat Channels accounts; generate platform-specific content in one shot; route all direct messages into a single inbox; automatically extract travel dates, party size, and budget; and tell your operators which inquiries to prioritize every day.

## 🎯 Overview

YunCe is a ready-to-use tool for domestic (China) new-media operations, covering six platforms: Douyin, Kuaishou, WeChat Channels, Xiaohongshu, Weibo, and Baijiahao. It is not a simple "mass-posting script" — it is an integrated workbench built around the daily operations workflow, from content production to publishing and post-publishing review.

This project is a fork of the open-source project **[dreammis/social-auto-upload](https://github.com/dreammis/social-auto-upload)**. The upstream project provides mature multi-platform browser-automation upload capabilities. On top of that, YunCe adds AI copywriting, content packs, compliance checks, task queues, review workflows, unified multi-platform DMs, data reflow, weekly operations reports, and more.

## 📌 Fork Declaration

- Upstream project: `dreammis/social-auto-upload`
- Upstream URL: https://github.com/dreammis/social-auto-upload
- Upstream license: MIT License
- This project's license: MIT License (original copyright notice retained)
- Original project copyright: Copyright (c) 2023 dreammis
- Fork contributions: Copyright (c) 2026 YunCe contributors

This project is not affiliated with the upstream author. Please submit issues and feature requests for fork-specific changes to this repository.

## ✨ New Features

### ✍️ Content Production

- **AI copywriting**: Generate titles, body text, hashtags, and publishing suggestions per platform style. Ships with a general-purpose template and supports common industries (food & beverage, beauty & personal care, education & training, fitness, local services, e-commerce, real estate & home) plus custom industries.
- **Multi-platform content packs**: Enter an industry and topic once, and generate differentiated content for all 6 platforms. Supports background jobs, real-time progress, 24-hour caching, and automatic fallback to local templates when the AI API is unavailable.
- **Auto cover generation**: Generate 3:4 and 9:16 covers from the first frame of a video.
- **Pre-publish compliance check**: Validates title length, absolute/overpromising wording, contact info, external links, hashtag count, asset and cover requirements.

### 🚀 Publishing & Review

- **Background task queue**: Publishing jobs are queued immediately without blocking the UI.
- **Automatic retry**: Failed publishes are retried once automatically, with the full failure reason recorded.
- **Publish records**: Track platform, account, asset, status, error reason, and time.
- **Scheduled publishing & calendar**: View schedules by day with a weekly calendar view.
- **Smart scheduling suggestions**: Recommends posting times based on historical content performance and flags scheduling conflicts.
- **Team review workflow**: Content can be submitted for review and only enters the publish queue after approval.

### 📊 Accounts & Data

- **Multi-account management**: QR-code login, session persistence, and validity checks.
- **Expired-session alerts**: Expired logins are surfaced on the publish records page and in today's to-do list.
- **Data reflow**: Scrape platform dashboard metrics such as views, likes, and comments.
- **Manual linking**: When auto-matching fails, platform posts can be manually linked to publish records.
- **Weekly operations report**: Aggregates publish volume, success/failure, views, engagement, and best-performing content, with copy and CSV export.
- **Alert notifications**: Publish failures, expired accounts, and AI content-pack failures can be pushed to WeCom (WeChat Work) bots, Feishu bots, or a generic webhook.

### 💬 Unified Multi-Platform DM

- **Unified inbox**: View Douyin, Xiaohongshu, and WeChat Channels DMs in one place, filterable by platform, account, and customer. Every conversation carries its source account so messages are never mixed up across accounts.
- **Multi-account isolation**: Multiple accounts per platform are supported. Account cookies, sync cursors, conversations, messages, and leads are stored per account; re-logging into an existing account updates the original record rather than duplicating it.
- **All-account sync**: Sync settings support multi-select accounts; when no specific account is checked, all logged-in accounts on that platform are synced by default. Each platform can have its own sync interval, per-sync conversation limit, and history look-back window (7 days by default, up to 30).
- **Douyin DM sync**: Uses local Playwright with saved login state to read creator-center web DMs; does not open conversations or mark them as read, and automatically skips group chats, self-sent messages, video/share cards, recalled messages, and platform placeholder prompts.
- **Xiaohongshu main-site DM sync**: Reads real DM conversations from `www.xiaohongshu.com/chat` on the Xiaohongshu main site — not the professional account backend message pool. DMs from regular user accounts also arrive here.
- **WeChat Channels DM sync**: Reads conversations and messages via the Channels Assistant login state, with per-account sync support.
- **Direct reply from the workbench**: Douyin, Xiaohongshu, and WeChat Channels all support sending text replies directly from the workbench using locally logged-in accounts, with results written back as "sent" or the failure reason.
- **Auto-reply draft protection**: When scheduled sync triggers auto-reply rules on these three platforms, only "pending manual send" drafts are generated — nothing is silently sent in the background. Human confirmation is required before sending.
- **Intent detection & lead extraction**: Identifies purchase intent and extracts specs, budget, timing, company, and contact info as leads. Supports custom keyword rules, tags, follow-up stages, notes, and CSV export.
- **Auto-reply engine**: Matches rules by audience, keyword, platform, and priority; supports both fixed templates and AI-powered replies, plus cooldowns, daily caps, sending windows, comment toggle, human-handoff keywords, AI fallback, AI daily usage cap, and one-reply-per-customer.
- **DM webhook**: Receive external DM events via `/dm/inbound`, with access-key auth, message idempotency/deduplication, and send-result write-back.
- **Today's priority follow-ups**: Scores leads by intent, unreplied messages, follow-up stage, contact-info completeness, and most recent inquiry time, directly surfacing "contact now / prioritize / follow up today" and next actions.
- **Today's operations to-do**: Aggregates priority leads, pending replies, failed publishes, pending reviews, scheduled tasks, expired accounts, and data-sync reminders.
- **Asset tagging & dedup**: Filter assets by tag, detect duplicates via MD5, show publish status, and clean up by days-since-last-use.
- **Log rotation**: Auto-rotates at 10 MB, retaining 14 days.

### 🔄 DM Sync & Reply

#### Usage Steps

1. Scan the QR code to log in under "Account Management". Each account must use the **actual platform account that receives the DMs**.
2. Open "DM Center → Auto-Reply Rules", enable scheduled sync on each platform card, multi-select accounts as needed, and set the sync interval and 48-hour filter.
3. Click "Sync Now" once, then enter the unified inbox and use platform/account filters to confirm message sources.
4. Open a conversation to type and send text directly; the send button only uses the source account of the selected conversation.

When no account is checked in sync settings, all accounts with valid cookies on that platform will be synced. Multi-account messages are isolated per account, and the unified inbox shows which platform account each message came from.

#### Platform DM Capabilities

| Platform | Message Source | Login Requirement | Sync | Workbench Direct Reply |
| --- | --- | --- | --- | --- |
| Douyin | Douyin Creator Center DMs | Scan QR with the Douyin account that receives DMs | Yes | Yes |
| Xiaohongshu | Xiaohongshu main-site DMs `/chat` | Scan QR on the main site with the Xiaohongshu account that receives DMs | Yes | Yes |
| WeChat Channels | Channels Assistant DMs | Scan QR with the WeChat/Channels account that receives DMs | Yes | Yes |

Sync reads the last 7 days of conversations by default (configurable 1–30 days; turning the time range off pulls as much history as the platform exposes) and deduplicates repeated messages. Raising the window clears the per-conversation "history already synced" markers so the next run backfills the older conversations. Sync results show account, scan count, stored count, duplicate count, and failure reasons.

#### Chat history persistence

Chat history lives in a local SQLite database (`social-auto-upload-main/db/ops_center.db`) and survives restarts:

- **Connections are closed immediately** after each read/write, so leaked handles can no longer lock the WAL (the old behaviour caused silently dropped writes).
- **WAL checkpointing** on startup, on shutdown, and every 10 minutes merges committed history into the main database file, so a hard kill cannot leave records stranded in the `-wal` file.
- **Daily backup** into `social-auto-upload-main/db/backups/`, keeping the last 7 copies.
- **Health endpoints**: `GET /dm/storage/health` reports file/WAL size, integrity, message counts, per-platform oldest/newest message times, and the backup list; `POST /dm/storage/checkpoint` and `POST /dm/storage/backup` force a merge or a backup.

#### Xiaohongshu Login & Conversation Troubleshooting

- **You must log in with the account that actually receives the DMs.** Xiaohongshu main-site DMs and the professional-account backend message pool are different data sources; using the wrong account or only checking the professional backend will miss messages from regular users.
- Login simultaneously validates main-site user info, main-site DM conversation list, and creator-service-platform login state. If any part expires, re-scan the QR in Account Management.
- Anonymous QR login on the Xiaohongshu main site in headless mode may trigger `300012 IP risk`. Login must use visible Chrome with a headed browser; keep your local network environment stable during sync and avoid frequently switching egress IPs.
- After re-login, click "Sync Now" first, then verify the account is selected in sync settings and its status is healthy. Conversations not displayed, collapsed, or restricted by stranger-DM limits cannot be read.
- If WeChat Channels shows no new messages after login, confirm you logged in with the DM-receiving account and run "Sync Now"; refreshing the frontend page alone will not re-fetch platform conversations.

### 🤖 Auto-Reply & Webhook

Configure the master switch, sending window, keyword rules, and connectors in "DM Center → Auto-Reply Rules". External systems can also POST new DMs to:

```text
http://127.0.0.1:5409/dm/inbound
```

Example:

```json
{
  "platform": "抖音",
  "account": "your-account-name",
  "external_user_id": "stable-platform-user-id",
  "external_message_id": "platform-message-id",
  "author": "Ms. Wang",
  "msg_type": "dm",
  "content": "What specs does this product have? How much? Can I book?",
  "source_content": "product intro video"
}
```

`external_user_id` merges the same customer; `external_message_id` prevents duplicate replies. If an access key is configured in settings, requests must include the `X-YunCe-Secret` header.

Sending behavior falls into three categories:

- **Manual reply from the workbench**: Sent directly to Douyin, Xiaohongshu, or WeChat Channels via local login state, with the result written back.
- **Auto-reply triggered by scheduled sync**: Always generates a "pending manual send" draft; nothing is actually sent automatically.
- **Auto-reply triggered by webhook**: Processed according to auto-reply rules, the sending toggle, and account login state; if send conditions are not met, it is saved as "pending manual send".

Reply rules support two generation modes: **fixed templates** (with variables like `{nickname}`, `{origin}`, `{travelers}`) and **AI smart replies** (calls AI on matched keywords, optionally with tone constraints such as "don't mention price; ask for contact info first"). When no rule matches and AI fallback is enabled, AI also generates a reply. AI generation feeds known leads (party size, origin, dates, budget, source content) into the prompt to avoid re-asking; on AI failure, it falls back to the fixed template in the rule. AI-generated replies go through a safety filter — suspected fabricated phone numbers, WeChat IDs, or absolute promises (e.g. "guaranteed", "lowest on the entire internet") are automatically flagged as "needs human review" and kept as drafts. The "AI daily cap" in settings counts global AI replies per day to control API costs.

## 🧩 Supported Platforms

| Platform | Login | Video Publishing | Image/Note | Scheduled Publishing | Data Reflow | DM Sync | Workbench Direct Reply |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Douyin | Yes | Yes | Yes | Yes | Yes | Yes | Yes |
| Kuaishou | Yes | Yes | Yes | Yes | Yes | - | - |
| WeChat Channels | Yes | Yes | - | Yes | Yes | Yes | Yes |
| Xiaohongshu | Yes | Yes | Yes | Yes | Yes | Yes | Yes |
| Weibo | Yes | Yes | - | Platform-dependent | - | - | - |
| Baijiahao | Yes | Yes | - | Platform-dependent | - | - | - |

Local DM sync and workbench direct reply currently cover Douyin, Xiaohongshu, and WeChat Channels. Platform pages and risk-control policies change over time; actual availability depends on runtime results.

## ⚡ Quick Start (Windows)

### 📋 Prerequisites

- Python 3.10+ (3.12 recommended)
- Node.js 18+
- Google Chrome (keep up to date; Xiaohongshu login and workbench replies use the local browser)
- Windows 10/11

### 📦 Installation

```powershell
python -m venv venv
venv\Scripts\pip install -r social-auto-upload-main\requirements.txt
venv\Scripts\python -m playwright install chromium
venv\Scripts\patchright install chromium

cd social-auto-upload-main\sau_frontend
npm install
cd ..\..
```

`requirements.txt` only declares direct dependencies. To reproduce the full verified environment, use `social-auto-upload-main\requirements.lock` instead. Development and test dependencies are in `social-auto-upload-main\requirements-dev.txt`.

### 🔑 Configure AI

Copy the config template and fill in your API key:

```powershell
Copy-Item social-auto-upload-main\.env.example social-auto-upload-main\.env
notepad social-auto-upload-main\.env
```

`.env` is excluded by `.gitignore` and will not be committed. Alert notifications can be configured in the same file:

```text
OPS_WEBHOOK_URL=your-wecom/feishu-bot-url
OPS_WEBHOOK_TYPE=auto
```

### 🏃 Launch

Double-click `start.bat` in the project root. The script will automatically:

1. Check ports 5409 / 5173;
2. Start the Python backend;
3. Start the Vue frontend;
4. Open http://localhost:5173.

You can also run `run_backend.bat` and `run_frontend.bat` separately.

## 📁 Project Structure

```text
FlowOps/
├─ start.bat                     # One-click launcher
├─ run_backend.bat               # Start backend only
├─ run_frontend.bat              # Start frontend only
├─ deploy/                       # Server deployment scripts & docs (Docker / bare metal / Nginx)
├─ social-auto-upload-main/      # Main app (forked from upstream)
│  ├─ sau_backend.py             # Flask API (served by waitress in production)
│  ├─ publish_jobs.py            # Publish queue, retry, health checks
│  ├─ stats_reflow.py            # Platform data reflow
│  ├─ ops_center.py              # Message center, to-do, weekly report
│  ├─ dm_ops.py                  # DM conversations, auto-reply, leads, webhook
│  ├─ dm_platform_common.py      # Multi-platform account parsing, cookies, time utils
│  ├─ douyin_dm_sync.py          # Douyin multi-account DM sync
│  ├─ xiaohongshu_dm.py          # Xiaohongshu main-site DM read & send
│  ├─ xiaohongshu_dm_sync.py     # Xiaohongshu scheduled DM sync
│  ├─ channels_dm.py             # WeChat Channels DM read & send
│  ├─ channels_dm_sync.py        # WeChat Channels scheduled DM sync
│  ├─ platform_reply.py          # Unified reply sender for 3 platforms
│  ├─ ai_content.py              # AI copywriting & content packs
│  ├─ compliance.py              # Pre-publish compliance checks (rules shared with platform registry)
│  ├─ myUtils/platform_registry.py  # Platform adapter registry (single source of truth for publishing)
│  ├─ schedule_ops.py            # Publishing calendar
│  ├─ schedule_suggest.py        # Smart scheduling suggestions
│  ├─ material_ops.py            # Asset tagging & dedup
│  ├─ media_ops.py               # Cover generation
│  ├─ sau_frontend/              # Vue 3 + Element Plus frontend
│  └─ uploader/                  # Upstream browser-automation implementations
└─ README.md
```

## 🚢 Server Deployment

Beyond running locally on Windows, the repo ships a set of server deployment scripts:

- `deploy/docker-compose.yml` + `deploy/Dockerfile`: containerized deployment;
- `deploy/install-baremetal.sh`: one-shot Linux bare-metal install (generates the access token and writes a systemd service);
- `deploy/nginx-yunce.conf`: reference Nginx reverse-proxy config;
- `deploy/yunce.service`: systemd service template;
- `deploy/backup.sh`: daily database backup script;
- `deploy/smoke.sh`: post-deployment smoke-test script.

See [deploy/README.md](./deploy/README.md) for full steps and parameters. Production serves backend traffic through waitress (still a single-process model) instead of the Flask development server.

## 🔒 Security Notes

- Backend and frontend listen on `127.0.0.1` by default; devices on the same LAN cannot directly access the workbench. For remote access, you must set `YUNCE_HOST`, `YUNCE_ALLOW_REMOTE=1`, and `YUNCE_ACCESS_TOKEN` in `.env` simultaneously; the backend refuses to start without a token. Frontend requests automatically carry `X-YunCe-Token`.
- After setting an access token, run `localStorage.setItem('yunce_access_token', 'your-token')` in the browser console so the frontend carries it; do not put the token in `VITE_*` variables or it will be bundled into frontend code.
- `.env`, databases, cookies, logs, uploaded assets, virtual environments, and frontend build artifacts are all excluded from the repository.
- Login credentials in `cookiesFile/` remain plaintext JSON — do not copy, commit, or share them via cloud drives. The workbench's local-only default access exists precisely to prevent these credentials from being abused by other devices on the same network.
- The database auto-creates tables on first launch; account login states must be generated by re-scanning QR codes on your machine.
- Only operate accounts you own or are authorized to use.
- This project automates browser actions to simulate human operations. Platforms do not offer unified publishing or DM APIs for individuals. Please follow each platform's terms of service and control posting and reply frequency.
- Workbench direct replies send real messages from real accounts to real users. Verify content and target conversations with a test account before going live to avoid misdirected sends.
- Xiaohongshu main-site login must be completed in a visible browser. If you encounter IP or account risk controls, stop high-frequency operations and investigate manually — do not attempt to bypass platform restrictions.
- AI-generated content requires human review, especially regarding pricing, stock, efficacy, service commitments, and advertising language.

## ⚠️ Known Limitations

- Platform page redesigns may break automation selectors; updates from upstream or this repo may be needed.
- Data reflow and scheduling for Weibo and Baijiahao are limited by platform backends.
- The three DM platforms only read conversations actually displayed on the web; stranger-DM restrictions, collapsed conversations, recalled messages, and non-text messages may not sync.
- Xiaohongshu main-site DMs differ from the professional-account backend message pool; you must log in with the account that actually receives DMs and keep both main-site and creator-service-platform sessions valid.
- Workbench direct reply currently supports text only — no images, files, voice, or stickers; send failures are written back into the conversation.
- Auto-replies from scheduled sync only produce "pending manual send" drafts and are never auto-sent; webhook-sent replies are still governed by rules and account login state.
- Real publishing, QR login, and DM sending must be verified in your own local account environment; platform risk controls or page redesigns may cause temporary unavailability.

## 🔗 Repositories

- Gitee: https://gitee.com/tokyi/flowops
- GitHub: https://github.com/datouy/FlowOps

## 📄 License

This project is licensed under the MIT License — see [LICENSE](./LICENSE). Original project copyright belongs to dreammis.
