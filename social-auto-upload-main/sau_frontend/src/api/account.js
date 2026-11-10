import { http } from '@/utils/request'

// 账号管理相关API
export const accountApi = {
  // 获取有效账号列表（带验证）
  getValidAccounts() {
    return http.get('/getValidAccounts')
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
    return http.get(`/deleteAccount?id=${id}`)
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
