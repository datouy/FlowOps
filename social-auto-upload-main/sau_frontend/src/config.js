// 统一的运行期配置：后端地址与可选访问令牌都从这里取，避免各处重复硬编码。
const rawBase = import.meta.env.VITE_API_BASE_URL || 'http://localhost:5409'

function resolveBase(value) {
  const text = String(value || '').trim()
  if (/^https?:\/\//i.test(text)) return text.replace(/\/+$/, '')
  // 相对路径：本机开发直连后端端口，其他部署环境跟随当前域名
  if (/^(localhost|127\.0\.0\.1)$/.test(window.location.hostname)) return 'http://127.0.0.1:5409'
  return `${window.location.origin}${text.replace(/\/+$/, '')}`
}

export const API_BASE = resolveBase(rawBase)

const TOKEN_KEY = 'yunce_access_token'

export function getAccessToken() {
  try {
    return localStorage.getItem(TOKEN_KEY) || ''
  } catch {
    return ''
  }
}

export function setAccessToken(token) {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token)
    else localStorage.removeItem(TOKEN_KEY)
  } catch {
    // localStorage 不可用时忽略，仅影响本次会话
  }
}

export function authHeaders() {
  const token = getAccessToken()
  return token ? { 'X-YunCe-Token': token } : {}
}

// 用于 <img src>、下载链接、EventSource 等无法自定义请求头的场景。
export function withToken(url) {
  const token = getAccessToken()
  if (!token) return url
  const sep = url.includes('?') ? '&' : '?'
  return `${url}${sep}token=${encodeURIComponent(token)}`
}
