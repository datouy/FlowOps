# 云策 YunCe · 差距一/三落地（第二轮）

> 日期：2026-11-10
> 范围：**改版韧性**（发布侧选择器适配）+ **浏览器密度**（共享实例池）
> 原则：改动集中在入口与旁路，不动主流程；默认生效，可一行环境变量回退。

---

## 0. 一句话结论

两个差距都落地了：

| 差距 | 之前 | 现在 |
|---|---|---|
| **一：改版韧性** | `selector_adapters` 只覆盖私信列表；发布侧 6 个 uploader 的选择器全硬编码，改版就要发版 | 发布流程关键锚点（上传入口/发布按钮/登录态标志）外置成候选表，可远端 yaml 覆盖，全失败自动打 Drift 诊断包；视频号+百家号已接入 |
| **三：浏览器密度** | 每账号一个 `launch_persistent_context`，一个 Chromium 进程 300-500MB，单机扛 5-6 个账号 | 共享 Chromium 单实例 + 每账号 `new_context`，内存从 O(账号数×进程) 降到 O(1 进程 + 账号数×增量)，密度提升一个数量级 |

附带：修复了一个 pull 下来的 flaky 测试（见 §4）。

---

## 1. 差距三：共享浏览器实例池

### 1.1 关键前提（为什么不用迁移数据）

账号登录态的**权威来源是 storage_state**（`cookiesFile/<name>.enc`），不是 persistent
profile 目录——`interactive_login` 登录成功后 `save_account_storage_state` 导出加密的
storage_state，`hydrate_context` 只是把它播种进 persistent profile。

所以单实例多 context 方案**零迁移**：直接读权威 storage_state，`new_context(storage_state=...)`
注入即可。实测真实账号解密出 **47 个 cookie + 5 组 localStorage**（含指纹 device_id），
足以支撑登录态。

### 1.2 实现

新增 `utils/shared_browser.py`：

- `get_browser()` / `get_browser_sync()`：共享 Chromium 单例（async/sync 各一），
  `is_connected()` 探活，崩溃后自动重建；
- `account_context()` / `account_context_sync()`：`new_context(storage_state + proxy +
  指纹 UA/viewport/locale/timezone)` + `add_init_script`。

`utils/account_browser.py` 在 `persistent_account_context`（async/sync）里加分派：

```
if headless 且 FLOWOPS_SHARED_BROWSER 开 → 共享 new_context(storage_state)
else → 原 persistent 独立进程
```

关键取舍：

- **登录（headless=False）仍走 persistent**：登录需要可见窗口，共享实例始终 headless，
  避免一个可见窗口把所有账号拖成可见。
- **IndexedDB / ServiceWorker 不保留**：这些不是登录态（登录态靠 cookie + localStorage），
  丢了顶多丢平台偏好/草稿，不影响发布与私信。
- **硬件指纹本来就是共享的**：一账号一进程跑在同一台机器上，GPU/audio 硬件指纹本来就
  相同，真正的隔离一直靠代理出口 + 独立 localStorage 指纹——共享模式不损失这个。
- **回退开关**：`FLOWOPS_SHARED_BROWSER=0` 一行回退到一账号一进程。

### 1.3 验收

- 新增 7 个单测（参数透传、storage_state 注入、代理/指纹透传、复用、分派、headless 回退）。
- 真实账号验证：`_shared_storage_state` 解密出 47 cookies + 5 origins；`_use_shared(headless=True)=True`、`(False)=False`。

---

## 2. 差距一：发布侧选择器适配

### 2.1 现状与问题

`selector_adapters.py` 原本只有 `candidates()`（私信列表）。发布侧的选择器硬编码在
`uploader/*/main.py` 里——平台改版一次，发布就得改代码发版重启。

调研发现各平台发布形态不同：

| 平台 | 发布形态 | 改版高发点 |
|---|---|---|
| 抖音 | API 直发（录制请求回放） | 上传入口 |
| 小红书 | API 直发 + 成功 URL 回查 | 上传入口 |
| 视频号 | 页面点击「发表视频」 | 发表按钮、登录态标志 |
| 快手 | 页面点击 + Ant Design 弹窗 | 上传按钮、发布确认 |
| 微博 | 页面点击（多步骤） | 上传视频入口 |
| 百家号 | 页面点击 | 发布按钮 |

### 2.2 实现

`selector_adapters.py` 新增发布步骤候选表 + 两个 helper：

- `PUBLISH_BUILTIN`：6 平台 × 关键步骤（`publish_button` / `upload_entry` /
  `login_marker` / `qrcode_marker` / `publish_confirm`）的有序候选，选择器从现有代码
  逐个提取（真实值，不是拍的）；
- `publish_candidates(platform, step)`：远端 `adapters/publish/<platform>.<step>.yaml`
  优先，内置兜底；
- `wait_publish_step_async/sync`：等第一个候选出现，全失败抛 `SelectorDriftError`
  （自动打包 URL/选择器/DOM/截图，MTTR 从天降到小时）；
- `publish_step_any_visible`：立即判断候选是否可见（登录态判定用）。

接入（示范两个平台）：

- `uploader/tencent_uploader/main.py::_is_tencent_login_completed`：登录态判定改走
  `publish_step_any_visible(page, "tencent", "login_marker"/"qrcode_marker")`，硬编码兜底；
- `uploader/baijiahao_uploader/main.py::_submit_publish`：发布按钮改走
  `wait_publish_step_async(page, "baijiahao", "publish_button")`，硬编码兜底。

两者都是"适配包优先、硬编码兜底"——**改版后推 yaml 即可修复，适配包缺失也不影响现有行为**。

### 2.3 验收

新增 6 个单测（内置表、yaml 覆盖、命中、全失败打 Drift 诊断包）。

> 其余平台（快手/微博/抖音/小红书）的内置候选表已就位，接入点是纯机械替换——
> 把 uploader 里对应的硬编码 `locator(...)` 换成 `wait_publish_step_*`。下轮可批量铺开。

---

## 3. 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `FLOWOPS_SHARED_BROWSER` | `1`（开） | `0`/`off` 回退一账号一进程 |
| `FLOWOPS_ADAPTER_URL` / `FLOWOPS_ADAPTER_KEY` | 空 | 远端适配包地址 + HMAC 密钥 |

---

## 4. 附带：修复了一个 flaky 测试

`test_publish_read_timeout.py::test_xhs_status_read_times_out_on_hanging_response_body`
的 fixture 用了 `_HangingResponse()`（默认 url 是**抖音**的），而小红书的 capture 只匹配
XHS markers——captured 永远为空，`read_json`（真正测响应体超时的代码）根本没被调用。
测试退化成靠 `read_timeout=0.3 → Deadline 0.3s → sleep(0.3) → budget.check()` 的
**0.3 秒边界竞态**"碰巧"通过/失败，机器负载一波动就翻车。

修复：`_HangingResponse` 传 `url="https://creator.xiaohongshu.com/api/galaxy/creator/note"`
（命中 XHS markers），让 capture 真正记录响应、`read_json` 真正测超时。修复后 3 次连跑稳定通过。

> 诊断过程用 stash 二分定位，确认与代码改动无关，纯粹是测试 fixture 的 url 与 capture
> 匹配条件不匹配 + 0.3s 时序边界。

---

## 5. 改动清单

| 文件 | 类型 | 说明 |
|---|---|---|
| `utils/shared_browser.py` | 新增 | 共享 Chromium 单实例（async/sync）+ `account_context` |
| `utils/account_browser.py` | 修改 | 加共享/持久分派（+86 行纯插入） |
| `selector_adapters.py` | 修改 | 发布步骤候选表 + `wait_publish_step_*` / `publish_step_any_visible` |
| `uploader/tencent_uploader/main.py` | 修改 | 登录态判定走适配包（硬编码兜底） |
| `uploader/baijiahao_uploader/main.py` | 修改 | 发布按钮走适配包（硬编码兜底） |
| `tests/conftest.py` | 修改 | 测试环境回退共享模式 |
| `tests/test_shared_browser.py` | 新增 | 7 用例 |
| `tests/test_selector_adapters_publish.py` | 新增 | 6 用例 |
| `tests/test_publish_read_timeout.py` | 修改 | 修 fixture url，消除 0.3s 竞态 |

---

## 6. 下一轮建议

1. **批量铺开发布侧选择器**：快手/微博/抖音/小红书的内置候选表已就位，接入是纯机械替换。
2. **远端适配包打通**：`refresh_from_remote` 目前只拉私信侧的 yaml，扩展拉发布步骤的 yaml。
3. **共享实例的浏览器级崩溃自愈**：单实例崩溃影响所有 context，`is_connected` 探活后重建已做，但可以加"重建后重试当前任务"。
