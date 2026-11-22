import { ElMessage } from 'element-plus'
import axios, { AxiosInstance, AxiosError, InternalAxiosRequestConfig, AxiosResponse } from 'axios'

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || '/api'

const ACCOUNT_RECOVERY_MESSAGES: Record<string, string> = {
  account_status_blocked: '账号已封禁或受限，请先处理平台限制',
  account_not_error: '账号状态已变化，请刷新后重试',
  account_missing: '账号不存在，请刷新列表',
  account_risk_blocked: '账号已停用或存在风控限制，请先检查启用状态和安全设置',
  operation_disabled: '账号自动化配置已关闭，请先启用后再恢复',
  recovery_episode_changed: '账号异常记录已变化，请刷新后重试',
  recovery_source_invalid: '恢复请求来源无效，请刷新后重试',
  static_proxy_required: '恢复需要绑定独享的静态代理',
  static_proxy_unavailable: '静态代理不可用，请先检查代理配置和健康状态',
  static_proxy_shared: '静态代理被其他启用账号共用，请先分配独享代理',
  session_reauthentication_required: '登录授权已失效，请重新登录绑定',
  session_unauthorized: '当前会话未获授权，请重新登录绑定',
  session_missing: '缺少有效会话，请重新登录绑定',
  account_deactivated: 'Telegram 账号已停用或封禁，无法自动恢复',
  account_frozen: 'Telegram 账号已冻结，请先处理平台限制',
  session_quarantined: '会话仍在隔离保护中，请稍后再次核验；反复失败请检查会话冲突',
  identity_mismatch: '会话手机号与账号不一致，请重新绑定正确账号',
  identity_unavailable: '无法核实账号身份，请检查手机号和会话配置',
  lease_busy: '账号会话正被其他进程使用，请稍后重试',
  lease_unavailable: '暂时无法取得安全租约，请稍后重试',
  session_lease_required: '安全隔离尚未就绪，请稍后重试',
  operation_lease_lost: '恢复期间租约已变化，请稍后重试',
  read_budget_wait: '正在等待读取预算，请到期后重试',
  telegram_cooldown: 'Telegram 限流冷却中，请等待冷却结束',
  connection_timeout: '连接超时，请检查网络或代理后重试',
  connection_failed: '连接失败，请检查网络或代理后重试',
  proxy_connection_failed: '代理连接失败，请检查代理后重试',
  network_unavailable: '网络暂时不可用，请检查连接后重试',
  proxy_unavailable: '代理暂时不可用，请检查代理后重试',
  account_changed_during_verification: '核验期间账号配置或状态已变化，请刷新后重试',
  cleanup_failed: '恢复连接尚未安全清理，账号继续保持隔离，请稍后重试',
  verification_failed: '身份核验未通过，账号继续保持异常，请查看安全记录',
  verification_cancelled: '身份核验已取消，账号继续保持异常',
  probe_read_limit: '本次核验已达到读取上限，请稍后重试',
}

export const getApiErrorMessage = (data: unknown): string => {
  if (!data || typeof data !== 'object') return 'Request failed'

  const payload = data as { message?: unknown; detail?: unknown; error?: unknown }
  if (payload.error && typeof payload.error === 'object' && !Array.isArray(payload.error)) {
    const nested = payload.error as { message?: unknown }
    if (typeof nested.message === 'string' && nested.message.trim()) {
      return nested.message
    }
  }
  if (typeof payload.message === 'string' && payload.message.trim()) {
    return payload.message
  }
  if (typeof payload.detail === 'string' && payload.detail.trim()) {
    return payload.detail
  }
  if (payload.detail && typeof payload.detail === 'object' && !Array.isArray(payload.detail)) {
    const detail = payload.detail as { message?: unknown; reason?: unknown }
    if (typeof detail.message === 'string' && detail.message.trim()) return detail.message
    if (typeof detail.reason === 'string' && Object.prototype.hasOwnProperty.call(ACCOUNT_RECOVERY_MESSAGES, detail.reason)) {
      return ACCOUNT_RECOVERY_MESSAGES[detail.reason]
    }
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
