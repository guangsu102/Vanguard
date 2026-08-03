import { ElMessage } from 'element-plus'
import axios, { AxiosInstance, AxiosError, InternalAxiosRequestConfig, AxiosResponse } from 'axios'

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || '/api'

export const getApiErrorMessage = (data: unknown): string => {
  if (!data || typeof data !== 'object') return 'Request failed'

  const payload = data as { message?: unknown; detail?: unknown }
  if (typeof payload.message === 'string' && payload.message.trim()) {
    return payload.message
  }
  if (typeof payload.detail === 'string' && payload.detail.trim()) {
    return payload.detail
  }
  if (Array.isArray(payload.detail)) {
    const messages = payload.detail
      .map((item) => {
        if (!item || typeof item !== 'object' || !('msg' in item)) return null
        const message = (item as { msg?: unknown }).msg
        return typeof message === 'string' && message.trim() ? message : null
      })
      .filter((item): item is string => item !== null)
    if (messages.length > 0) return messages.join('; ')
  }
  return 'Request failed'
}

const apiClient: AxiosInstance = axios.create({
  baseURL: API_BASE_URL,
  timeout: 30000,
  headers: {
    'Content-Type': 'application/json',
  },
})

// Request interceptor
apiClient.interceptors.request.use(
  (config: InternalAxiosRequestConfig) => {
    const token = localStorage.getItem('token')
    if (token && config.headers) {
      config.headers.Authorization = `Bearer ${token}`
    }
    return config
  },
  (error: AxiosError) => {
    return Promise.reject(error)
  }
)

// Response interceptor
apiClient.interceptors.response.use(
  (response: AxiosResponse) => {
    const res = response.data
    if (res.code !== undefined && res.code !== 0) {
      ElMessage.error(res.message || 'Request failed')
      return Promise.reject(new Error(res.message || 'Request failed'))
    }
    return response
  },
  (error: AxiosError) => {
    if (error.response) {
      const status = error.response.status
      switch (status) {
        case 401:
          ElMessage.error('Unauthorized, please login')
          localStorage.removeItem('token')
          window.location.href = '/login'
          break
        case 403:
          ElMessage.error('Access denied')
          break
        case 404:
          ElMessage.error('Resource not found')
          break
        case 500:
          ElMessage.error('Server error')
          break
        default:
          ElMessage.error(getApiErrorMessage(error.response.data))
      }
    } else if (error.request) {
      ElMessage.error('Network error, please try again')
    }
    return Promise.reject(error)
  }
)

export default apiClient
