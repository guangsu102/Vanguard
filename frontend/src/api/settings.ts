import apiClient from './client'

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
}

export interface XBoardSettings {
  enabled: boolean
  callbackEnabled: boolean
  protocol: string
  source: string
}

export interface AiReplySettings {
  enabled: boolean
}

export interface GroupAiInteractionSettings {
  enabled: boolean
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

export interface OwnedGroupMessagingSettings {
  enabled: boolean
  dryRun: boolean
  globalMaxPerGroupPerDay: number
  globalMaxPerAccountPerDay: number
  minGroupCooldownSeconds: number
  contentDedupeWindowSeconds: number
  reviewTtlHours: number
  maxSendAttempts: number
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
  autoReplyEnabled: boolean
  inboundRepliesEnabled?: boolean
  manualReplyEnabled: boolean
  proactiveEnabled: boolean
  templates?: Partial<PrivateReplyTemplatesSettings>
}

export interface SettingsUpdatePayload {
  notification?: Partial<NotificationSettings>
  xboard?: Partial<XBoardSettings>
  aiReply?: Partial<AiReplySettings>
  groupAiInteraction?: Partial<GroupAiInteractionSettings>
  ownedGroupMessaging?: Partial<OwnedGroupMessagingSettings>
  keywordPrivateReply?: Partial<KeywordPrivateReplySettings>
  privateMessaging?: Partial<PrivateMessagingSettings>
}

export interface OwnedGroupAiPersonaFeatureSettings {
  enabled: boolean
  revision: number
  updatedAt: string | null
  updatedBy: number | null
  staticEnabled: boolean
  effectiveEnabled: boolean
}

export interface SettingsReadData extends SettingsUpdatePayload {
  readonly ownedGroupAiPersona: Readonly<OwnedGroupAiPersonaFeatureSettings>
}

export interface OwnedGroupAiPersonaFeatureUpdate {
  expectedRevision: number
  enabled: boolean
}

export interface SettingsApiError {
  code: string
  status: number | null
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  Boolean(value) && typeof value === 'object' && !Array.isArray(value)

export const getSettingsApiError = (error: unknown): SettingsApiError => {
  const outer = isRecord(error) ? error : {}
  const response = isRecord(outer.response) ? outer.response : {}
  const payload = isRecord(response.data) ? response.data : {}
  const nested = isRecord(payload.error)
    ? payload.error
    : isRecord(payload.detail)
      ? payload.detail
      : payload

  return {
    code: typeof nested.code === 'string' ? nested.code : 'SETTINGS_REQUEST_FAILED',
    status: typeof response.status === 'number' ? response.status : null,
  }
}

const ownedGroupAiPersonaFeatureErrorMessages: Readonly<Record<string, string>> = {
  ADMIN_REQUIRED: '仅管理员可以修改 Persona 功能开关',
  PERSONA_FEATURE_REVISION_CONFLICT: '设置已被其他管理员修改，请确认后重试',
  PERSONA_FEATURE_SETTING_NOT_INITIALIZED: 'Persona 运行时设置尚未初始化，请先完成数据库迁移',
}

export const getOwnedGroupAiPersonaFeatureErrorMessage = (error: SettingsApiError): string => {
  if (ownedGroupAiPersonaFeatureErrorMessages[error.code]) {
    return ownedGroupAiPersonaFeatureErrorMessages[error.code]
  }
  if (error.status === 403) return ownedGroupAiPersonaFeatureErrorMessages.ADMIN_REQUIRED
  if (error.status === 503) return 'Persona 功能设置暂时不可用，请稍后重试'
  return 'Persona 功能开关保存失败，请稍后重试'
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
    return apiClient.get<{ data: SettingsReadData }>('/settings')
  },

  update: (data: SettingsUpdatePayload) => {
    return apiClient.put('/settings', data)
  },

  updateOwnedGroupAiPersonaFeature: (data: OwnedGroupAiPersonaFeatureUpdate) => {
    return apiClient.put<{ data: OwnedGroupAiPersonaFeatureSettings }>(
      '/settings/owned-group-ai-persona',
      data,
    )
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
