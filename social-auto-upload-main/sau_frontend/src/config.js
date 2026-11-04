// 统一的运行期配置：后端地址与可选访问令牌都从这里取，避免各处重复硬编码。
const rawBase = import.meta.env.VITE_API_BASE_URL || 'http://localhost:5409'

const TOKEN_KEY = 'yunce_access_token'

function captureAccessTokenFromUrl() {
  try {
    const url = new URL(window.location.href)
    const token = url.searchParams.get('access_token')
    if (!token) return
    localStorage.setItem(TOKEN_KEY, token)
    url.searchParams.delete('access_token')
    window.history.replaceState(null, '', `${url.pathname}${url.search}${url.hash}`)
  } catch {
    // 地址栏或 localStorage 不可用时，仍允许后续手动写入令牌。
  }
}

captureAccessTokenFromUrl()

function resolveBase(value) {
  const text = String(value || '').trim()
  if (/^https?:\/\//i.test(text)) return text.replace(/\/+$/, '')
  // 相对路径：本机开发直连后端端口，其他部署环境跟随当前域名
  if (/^(localhost|127\.0\.0\.1)$/.test(window.location.hostname)) return 'http://127.0.0.1:5409'
  return `${window.location.origin}${text.replace(/\/+$/, '')}`
}

export const API_BASE = resolveBase(rawBase)

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

export async function ensureAccessToken() {
  const hostname = String(window.location.hostname || '')
  if (!/^(localhost|127\.0\.0\.1)$/.test(hostname)) return
  try {
    const response = await fetch('/__yunce_local_token', { cache: 'no-store' })
    if (!response.ok) return
    const payload = await response.json()
    if (payload?.token) setAccessToken(payload.token)
  } catch {
    // 生产环境没有该开发服务器端点，继续使用邀请链接或手动输入令牌。
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
