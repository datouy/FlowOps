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

  resetMfa(id) {
    return http.post(`/api/v2/users/${id}/mfa-reset`)
  },

  unlockLogin(id) {
    return http.post(`/api/v2/users/${id}/unlock-login`)
  }
}
