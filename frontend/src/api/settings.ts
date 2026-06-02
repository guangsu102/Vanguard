import apiClient from './client'

export interface SystemSettings {
  siteName: string
  siteLogo?: string
  timezone: string
  language: string
  maintenanceMode: boolean
  maintenanceMessage?: string
}

export interface NotificationSettings {
  telegramEnabled: boolean
  telegramChatId?: string
  emailEnabled: boolean
  emailRecipients?: string[]
  webhookEnabled: boolean
  webhookUrl?: string
  alertOnError: boolean
  alertOnWarning: boolean
}

export interface SecuritySettings {
  loginAttempts: number
  lockoutDuration: number
  sessionTimeout: number
  allowedIpList?: string[]
  require2FA: boolean
}

export interface XBoardSettings {
  enabled: boolean
  apiUrl: string
  apiKey?: string
  webhookUrl?: string
}

export interface AiReplySettings {
  enabled: boolean
  privateOnly: boolean
  dailyTokenBudget: number
  maxRepliesPerUserPerDay: number
  cooldownSeconds: number
}

export interface SettingsFormData {
  site?: Partial<SystemSettings>
  notification?: Partial<NotificationSettings>
  security?: Partial<SecuritySettings>
  xboard?: Partial<XBoardSettings>
  aiReply?: Partial<AiReplySettings>
}

export interface SystemInfo {
  version: string
  pythonVersion: string
  database: string
  redis: string
  uptime: string
  lastBackup?: string
}

export interface OperationLog {
  id: number
  user: string
  action: string
  target: string
  ip: string
  timestamp: string
  status: 'success' | 'failed'
  details?: string
}

export interface LogListParams {
  page?: number
  pageSize?: number
  user?: string
  action?: string
  startDate?: string
  endDate?: string
}

export const settingsApi = {
  get: () => {
    return apiClient.get<{ data: SettingsFormData }>('/settings')
  },

  update: (data: SettingsFormData) => {
    return apiClient.put('/settings', data)
  },

  getSystemInfo: () => {
    return apiClient.get<{ data: SystemInfo }>('/settings/system')
  },

  getLogs: (params?: LogListParams) => {
    return apiClient.get<{ data: { list: OperationLog[]; total: number } }>('/settings/logs', { params })
  },

  exportLogs: (params?: LogListParams) => {
    return apiClient.get('/settings/logs/export', { params, responseType: 'blob' })
  },

  clearLogs: () => {
    return apiClient.post('/settings/logs/clear')
  },

  backupDatabase: () => {
    return apiClient.post<{ data: { filename: string } }>('/settings/backup')
  },

  restartService: () => {
    return apiClient.post('/settings/restart')
  },
}
