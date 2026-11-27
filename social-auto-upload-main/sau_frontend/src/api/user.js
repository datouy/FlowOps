import { http } from '@/utils/request'

export const userApi = {
  getAuthConfig() {
    return http.get('/api/v2/auth/config', null, { silent: true })
  },

  getAuthStatus() {
    return http.get('/api/v2/auth/status')
  },

  login(credentials) {
    return http.post('/api/v2/auth/login', credentials)
  },

  // 单密码门禁：只需要一个密码（不需要用户名/令牌）
  unlock(password) {
    return http.post('/api/v2/auth/unlock', { password })
  },

  verifyMfa(payload) {
    return http.post('/api/v2/auth/mfa/verify', payload)
  },

  setupMfa(currentPassword) {
    return http.post('/api/v2/auth/mfa/setup', { currentPassword })
  },

  confirmMfa(code) {
    return http.post('/api/v2/auth/mfa/confirm', { code })
  },

  disableMfa(payload) {
    return http.post('/api/v2/auth/mfa/disable', payload)
  },

  logout() {
    return http.post('/api/v2/auth/logout', null, { silent: true })
  },

  changePassword(payload) {
    return http.post('/api/v2/auth/password', payload)
  },

  listUsers() {
    return http.get('/api/v2/users')
  },

  createUser(payload) {
    return http.post('/api/v2/users', payload)
  },

  updateUser(id, payload) {
    return http.patch(`/api/v2/users/${id}`, payload)
  },

  resetPassword(id, password) {
    return http.post(`/api/v2/users/${id}/password`, {
      password,
      mustChangePassword: true
    })
  },

  revokeSessions(id) {
    return http.post(`/api/v2/users/${id}/revoke-sessions`)
  },

  listQueuedJobs(id) {
    return http.get(`/api/v2/users/${id}/queued-jobs`)
  },

  cancelQueuedJobs(id, payload = {}) {
    return http.post(`/api/v2/users/${id}/cancel-queued-jobs`, payload)
  },

  resetMfa(id) {
    return http.post(`/api/v2/users/${id}/mfa-reset`)
  },

  unlockLogin(id) {
    return http.post(`/api/v2/users/${id}/unlock-login`)
  }
}
