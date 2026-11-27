import { http } from '@/utils/request'

// 账号管理相关API
export const accountApi = {
  // 获取有效账号列表（带验证）
  getValidAccounts() {
    return http.post('/getValidAccounts')
  },

  // 获取账号列表（不带验证，快速加载）
  getAccounts() {
    return http.get('/getAccounts')
  },

  // 添加账号
  // 注意：后端没有 /account 路由，账号统一通过扫码登录（SSE /login）创建，
  // 因此这里不提供 addAccount，避免调用方踩到 404。

  // 更新账号
  updateAccount(data) {
    return http.post('/updateUserinfo', data)
  },

  // 删除账号
  deleteAccount(id) {
    return http.post('/deleteAccount', { id })
  },

  pauseAutomation(id, reason = '', until = '') {
    return http.post(`/api/v2/accounts/${id}/automation-pause`, { reason, until })
  },

  resumeAutomation(id) {
    return http.post(`/api/v2/accounts/${id}/automation-resume`)
  },

  // ---- 账号服务 v2（结构化账号 / 健康度 / 授权审计）----
  // v1 的 getAccounts 只回 5 列原始行，前端拿不到"身份、最后校验时间、失效原因"。
  // 这组接口对应后端 account_store.py，把登录结果当成"媒体号"来管理。

  // 结构化账号列表（平台元数据 + 身份 + 健康度 + 凭据状态 + 汇总）
  getAccountsDetailed(params = {}) {
    return http.get('/api/v2/accounts', params)
  },

  // 单个账号详情
  getAccountDetail(id) {
    return http.get(`/api/v2/accounts/${id}`)
  },

  // 单账号健康检查（立刻回写 status / lastCheckAt / failReason）
  checkAccount(id) {
    return http.post(`/api/v2/accounts/${id}/check`)
  },

  // 私信能力检测：打开一次真实页面数会话行，给出明确结论
  // （可读 N 个会话 / 需要重新扫码 / 暂时读不出来+原因）
  checkDmCapability(id) {
    return http.post(`/api/v2/accounts/${id}/dm-check`)
  },

  // ---- 会话保活（登录窗口剩余天数 / 触达证据 / 立即续期）----
  // 抖音的窗口来自 sid_guard 里的 60 天固定区间：普通网页访问不会延长它，
  // 所以这里展示的是"还剩多久到期、上次触达有没有刷新设备票"。

  // 单个账号的会话摘要 + 近 10 次触达证据
  getAccountSession(id) {
    return http.get(`/api/v2/accounts/${id}/session`)
  },

  // 立即做一次保活触达（后端后台线程执行，几秒到几十秒）
  touchAccountSession(id) {
    return http.post(`/api/v2/accounts/${id}/session/touch`)
  },

  // 保活循环状态 + 全部账号的会话摘要（一次拿全，避免逐账号 N+1）
  getKeeperStatus() {
    return http.get('/api/v2/keeper/status')
  },

  // 批量健康检查（逐个校验并回写状态，返回汇总）
  checkAllAccounts() {
    return http.post('/api/v2/accounts/check-all')
  },

  // 验证该账号代理的真实出口 IP 并落盘
  verifyProxyEgress(id) {
    return http.post(`/api/v2/accounts/${id}/proxy/verify`)
  },

  // 读取该账号最近一次出口验证结果
  getProxyEgress(id) {
    return http.get(`/api/v2/accounts/${id}/proxy/egress`)
  },

  // 凭据脱敏预览（只回 Cookie 名称/域名与 localStorage 键名，不回值）
  getAccountCredentials(id) {
    return http.get(`/api/v2/accounts/${id}/credentials`)
  },

  // 账号级持久浏览器环境与代理（密码只写不读）
  getAccountEnvironment(id) {
    return http.get(`/api/v2/accounts/${id}/environment`)
  },

  updateAccountEnvironment(id, proxy) {
    return http.put(`/api/v2/accounts/${id}/environment`, { proxy })
  },

  // 发布通道状态（API 直发 / 页面自动化）
  getPublishTransport(id) {
    return http.get(`/api/v2/accounts/${id}/publish-transport`)
  },

  clearPublishTransport(id) {
    return http.delete(`/api/v2/accounts/${id}/publish-transport`)
  },

  // 授权会话审计（含已结束/被中断的会话）
  getAuthSessions(limit = 20) {
    return http.get('/api/v2/auth-sessions', { limit })
  },

  // 清理没有被任何账号引用的 Cookie 文件（默认 dry-run）
  pruneOrphanCookies(dryRun = true) {
    return http.post('/api/v2/accounts/prune-cookies', null, {
      params: { dry: dryRun ? 1 : 0 }
    })
  },

  // 为历史账号补平台身份
  backfillIdentities(force = false) {
    return http.post('/api/v2/accounts/backfill-identities', null, {
      params: force ? { force: 1 } : {}
    })
  }
}
