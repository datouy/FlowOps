import axios from 'axios'
import { API_BASE, authHeaders, setAccessToken } from '@/config'

// 创建axios实例
const request = axios.create({
  baseURL: API_BASE,
  timeout: 30000,
  headers: {
    'Content-Type': 'application/json'
  }
})

// ---------------------------------------------------------------- 静默续期
//
// 后端签发两颗令牌：
//   * access  12 小时，放 localStorage（兼容旧的 X-YunCe-Token/OAuth Bearer 用法）
//   * refresh 30 天，同源部署时是 HttpOnly Cookie；跨域开发环境拿不到 Cookie，
//     由登录响应里的 refreshToken 落到 localStorage 兜底
//
// 语义：只要用户在窗口内用过一次，access 会自动续到完整 12 小时（后端滑动）；
// 彻底关掉浏览器很久之后再打开，靠 refresh 换一张新 access，不弹登录框。

const REFRESH_KEY = 'yunce_refresh'
const AUTH_PATHS = ['/auth/login', '/auth/mfa/verify', '/auth/refresh']

let refreshInFlight = null

function readRefreshToken() {
  try {
    return localStorage.getItem(REFRESH_KEY) || ''
  } catch {
    return ''
  }
}

export function setRefreshToken(token) {
  try {
    if (token) localStorage.setItem(REFRESH_KEY, token)
    else localStorage.removeItem(REFRESH_KEY)
  } catch {
    // localStorage 不可用时仅影响跨域开发环境的兜底
  }
}

/** 登录/双因素成功后被 App.vue 调用，记住 refreshToken 兜底值。 */
export function rememberRefreshToken(payload) {
  if (payload && payload.refreshToken) setRefreshToken(payload.refreshToken)
}

function isAuthPath(url) {
  const text = String(url || '')
  return AUTH_PATHS.some((path) => text.includes(path))
}

/**
 * 单飞刷新：并发 401 只发一次 /auth/refresh，其余请求等同一结果。
 * 成功返回新的 access token；失败抛出，由调用方派发登录失效事件。
 */
export function refreshSession() {
  if (refreshInFlight) return refreshInFlight
  const body = { refreshToken: readRefreshToken() }
  refreshInFlight = axios
    .post(`${API_BASE}/api/v2/auth/refresh`, body, {
      timeout: 15000,
      withCredentials: true
    })
    .then(({ data }) => {
      const payload = (data && data.data) || {}
      if (!payload.token) throw new Error((data && data.msg) || '续期失败')
      setAccessToken(payload.token)
      if (payload.refreshToken) setRefreshToken(payload.refreshToken)
      return payload.token
    })
    .finally(() => {
      refreshInFlight = null
    })
  return refreshInFlight
}

// 请求拦截器
request.interceptors.request.use(
  (config) => {
    // 可选访问令牌（后端设置 YUNCE_ACCESS_TOKEN 时生效）
    Object.assign(config.headers, authHeaders())
    const token = localStorage.getItem('token')
    if (token) {
      config.headers.Authorization = `Bearer ${token}`
    }
    return config
  },
  (error) => {
    console.error('请求错误:', error)
    return Promise.reject(error)
  }
)

// 响应拦截器
request.interceptors.response.use(
  (response) => {
    const { data } = response

    // 根据后端接口规范处理响应（202 表示异步任务已受理，不是错误）
    if (data.code === 200 || data.code === 202 || data.success) {
      return data
    } else {
      const message = data.msg || data.message || '请求失败'
      if (!response.config?.silent) {
        ElMessage.error(message)
      }
      return Promise.reject(new Error(message))
    }
  },
  async (error) => {
    console.error('响应错误:', error)

    if (error.config?.silent) {
      return Promise.reject(error)
    }

    const original = error.config || {}
    const status = error.response?.status
    const canRetry = status === 401 && !original._yunceRetried && !isAuthPath(original.url)

    if (canRetry) {
      // 先静默续期再重放一次；失败才认定登录失效。
      original._yunceRetried = true
      try {
        await refreshSession()
        return request(original)
      } catch {
        if (typeof window !== 'undefined') {
          window.dispatchEvent(new CustomEvent('yunce:auth-expired'))
        }
        return Promise.reject(error)
      }
    }

    if (status === 401 && typeof window !== 'undefined' && !isAuthPath(original.url)) {
      window.dispatchEvent(new CustomEvent('yunce:auth-expired'))
    }

    const serverMessage = error.response?.data?.msg
    if (serverMessage) {
      ElMessage.error(serverMessage)
      return Promise.reject(error)
    }

    // 处理HTTP错误状态码
    if (error.response) {
      switch (status) {
        case 401:
          ElMessage.error('未授权，请重新登录')
          break
        case 403:
          ElMessage.error('拒绝访问')
          break
        case 404:
          ElMessage.error('请求地址不存在')
          break
        case 500:
          ElMessage.error('服务器内部错误')
          break
        default:
          ElMessage.error('网络错误')
      }
    } else {
      ElMessage.error('网络连接失败')
    }

    return Promise.reject(error)
  }
)

// 封装常用的请求方法
export const http = {
  get(url, params, config = {}) {
    return request.get(url, { ...config, params })
  },
  
  post(url, data, config = {}) {
    return request.post(url, data, config)
  },
  
  put(url, data, config = {}) {
    return request.put(url, data, config)
  },

  patch(url, data, config = {}) {
    return request.patch(url, data, config)
  },
  
  delete(url, params, config = {}) {
    return request.delete(url, { ...config, params })
  },
  
  upload(url, formData, onUploadProgress) {
    return request.post(url, formData, {
      timeout: 600000,
      headers: {
        'Content-Type': 'multipart/form-data'
      },
      onUploadProgress
    })
  }
}

export default request
