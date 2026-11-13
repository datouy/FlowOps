import axios from 'axios'
import { API_BASE, authHeaders } from '@/config'

// 创建axios实例
const request = axios.create({
  baseURL: API_BASE,
  timeout: 30000,
  headers: {
    'Content-Type': 'application/json'
  }
})

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
    
    // 根据后端接口规范处理响应
    if (data.code === 200 || data.success) {
      return data
    } else {
      const message = data.msg || data.message || '请求失败'
      if (!response.config?.silent) {
        ElMessage.error(message)
      }
      return Promise.reject(new Error(message))
    }
  },
  (error) => {
    console.error('响应错误:', error)

    if (error.config?.silent) {
      return Promise.reject(error)
    }

    const serverMessage = error.response?.data?.msg
    if (serverMessage) {
      ElMessage.error(serverMessage)
      return Promise.reject(error)
    }
    
    // 处理HTTP错误状态码
    if (error.response) {
      const { status } = error.response
      switch (status) {
        case 401:
          ElMessage.error('未授权，请重新登录')
          // 可以在这里处理登录跳转
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
