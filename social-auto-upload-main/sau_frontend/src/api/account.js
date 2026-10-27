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
  }
}