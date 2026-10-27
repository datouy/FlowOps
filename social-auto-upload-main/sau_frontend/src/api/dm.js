import { http } from '@/utils/request'

export const dmApi = {
  getAnalytics: () => http.get('/dm/analytics'),
  getAiStatus: () => http.get('/ai/status'),
  getSettings: () => http.get('/dm/settings'),
  saveSettings: payload => http.put('/dm/settings', payload),
  getSyncStatus: url => http.get(url),
  saveSyncSettings: (prefix, payload) => http.put(`${prefix}/settings`, payload),
  runSync: (prefix, config = {}) => http.post(`${prefix}/run`, {}, {
    timeout: 600000,
    ...config,
  }),
  getRules: () => http.get('/dm/rules'),
  getTemplates: () => http.get('/message-templates'),
  getConversations: params => http.get('/dm/conversations', params),
  getStorageHealth: () => http.get('/dm/storage/health'),
  getLeads: params => http.get('/dm/conversations', params),
  markConversationRead: id => http.post(`/dm/conversations/${id}/read`, {}, { silent: true }),
  getConversation: (id, params) => http.get(`/dm/conversations/${id}`, params),
  generateDraft: id => http.post(`/dm/conversations/${id}/ai-draft`),
  sendReply: (id, text) => http.post(`/dm/conversations/${id}/reply`, { text }),
  updateContact: (id, payload) => http.put(`/dm/contacts/${id}`, payload),
  saveRule: (id, payload) => id
    ? http.put(`/dm/rules/${id}`, payload)
    : http.post('/dm/rules', payload),
  deleteRule: id => http.delete(`/dm/rules/${id}`),
  importMessages: payload => http.post('/dm/import', payload),
}
