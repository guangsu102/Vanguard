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
  sub2apiAlertsEnabled: boolean
  sub2apiNotifyResolved: boolean
  sub2apiAnnouncementsEnabled: boolean
  telegramEnabled: boolean
  telegramChatId?: string
  telegramAnnouncementsEnabled: boolean
  telegramAnnouncementChatId?: string
  telegramAnnouncementPin: boolean
  telegramAnnouncementPinSilent: boolean
  qqEnabled: boolean
  qqAnnouncementsEnabled: boolean
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

export interface GroupAiInteractionSettings {
  enabled: boolean
  aiEnabled: boolean
  dailyTokenBudget: number
  maxRepliesPerGroupPerDay: number
  maxRepliesPerAccountPerDay: number
  cooldownSeconds: number
  replyMaxChars: number
  blockAiSelfDisclosure: boolean
  mode: 'assistive' | 'warmup' | 'conversion' | 'off' | string
  tone: 'natural' | 'friendly' | 'professional' | 'soft' | string
  temperature: number
  maxTokens: number
  allowKeywordTriggeredReply: boolean
  allowSemanticTriggeredReply: boolean
  semanticScanWindowMessages: number
  semanticEvaluateEveryMessages: number
  semanticMinConfidence: number
  semanticMinTextChars: number
  semanticAllowedIntents: string[]
  semanticBlockedIntents: string[]
  semanticDecisionPrompt: string
  allowProactiveWarmup: boolean
  proactiveWarmupIntervalMinutes: number
  proactiveWarmupMaxGroupsPerRun: number
  proactiveWarmupMaxPerGroupPerDay: number
  proactiveWarmupMaxPerAccountPerDay: number
  proactiveWarmupCooldownSeconds: number
  proactiveWarmupWindowStartHour: number
  proactiveWarmupWindowEndHour: number
  proactiveWarmupTopics: string[]
  proactiveWarmupTemplates: string[]
  proactiveWarmupGroupOverrides: Record<string, {
    enabled?: boolean
    topics?: string[]
    templates?: string[]
    prompt?: string
  }>
  systemPrompt: string
}

export interface KeywordPrivateReplySettings {
  enabled: boolean
}

export interface PrivateReplyTemplatesSettings {
  startWelcome: string
  help: string
  register: string
  statusFound: string
  statusPending: string
  unknownCommand: string
  thanks: string
  usageHelp: string
  registerIntent: string
  priceIntent: string
  nodeIntent: string
  default: string
  guideWelcome: string
  guideIntroduce: string
  guideInviteRegister: string
  guideConfirm: string
  guideTimeout: string
  guideNoNeed: string
  guideConfirmSuccess: string
  guideRegisterReminder: string
  guideFallback: string
  triggerInvite: string
}

export interface PrivateMessagingSettings {
  inboundRepliesEnabled: boolean
  proactiveEnabled: boolean
  templates?: Partial<PrivateReplyTemplatesSettings>
}

export interface SettingsFormData {
  site?: Partial<SystemSettings>
  notification?: Partial<NotificationSettings>
  security?: Partial<SecuritySettings>
  xboard?: Partial<XBoardSettings>
  aiReply?: Partial<AiReplySettings>
  groupAiInteraction?: Partial<GroupAiInteractionSettings>
  keywordPrivateReply?: Partial<KeywordPrivateReplySettings>
  privateMessaging?: Partial<PrivateMessagingSettings>
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
