import apiClient from './client'

export type RuleType = 'message_moderation' | 'join_verification' | 'spam_protection' | 'custom'
export type ModerationRuleType = 'keyword' | 'domain' | 'frequency' | 'image'
export type RuleStatus = 'active' | 'inactive'
export type RuleAction = 'mute' | 'kick' | 'ban' | 'warn' | 'delete' | 'report'
export type ModerationRuleAction = 'warn' | 'mute' | 'ban' | 'kick'
export type ModerationRuleLevel = 'low' | 'medium' | 'high'

export interface RuleCondition {
  type: 'keyword' | 'regex' | 'frequency' | 'user_type' | 'media_type'
  operator: 'equals' | 'contains' | 'matches' | 'greater_than' | 'less_than'
  value: string | number
  caseSensitive?: boolean
}

export interface RuleActionConfig {
  action: RuleAction
  duration?: number
  warnMessage?: string
  notifyAdmin?: boolean
}

export interface Rule {
  id: number
  name: string
  type: RuleType
  description?: string
  conditions: RuleCondition[]
  actions: RuleActionConfig[]
  priority: number
  status: RuleStatus
  hitCount: number
  createdAt: string
  updatedAt: string
}

export interface RuleListParams {
  page?: number
  pageSize?: number
  type?: RuleType
  status?: RuleStatus
  keyword?: string
}

export interface RuleListResponse {
  list: Rule[]
  total: number
  page: number
  pageSize: number
}

export interface RuleFormData {
  name: string
  type: RuleType
  description?: string
  conditions: RuleCondition[]
  actions: RuleActionConfig[]
  priority?: number
}

export interface RuleTestResult {
  matched: boolean
  matchedConditions: string[]
  executedActions: string[]
  message?: string
}

export interface ModerationRule {
  id: number
  rule_type: ModerationRuleType
  pattern: string
  level: ModerationRuleLevel
  action: ModerationRuleAction
  group_id?: number | null
  enabled: boolean
  created_at: string
  updated_at: string
}

export interface ModerationRuleFormData {
  rule_type: ModerationRuleType
  pattern: string
  level: ModerationRuleLevel
  action: ModerationRuleAction
  group_id?: number | null
  enabled: boolean
}

export const rulesApi = {
  list: (params?: RuleListParams) => {
    return apiClient.get<{ data: RuleListResponse }>('/rules', { params })
  },

  create: (data: RuleFormData) => {
    return apiClient.post<{ data: Rule }>('/rules', data)
  },

  getById: (id: number) => {
    return apiClient.get<{ data: Rule }>(`/rules/${id}`)
  },

  update: (id: number, data: Partial<RuleFormData>) => {
    return apiClient.put<{ data: Rule }>(`/rules/${id}`, data)
  },

  delete: (id: number) => {
    return apiClient.delete(`/rules/${id}`)
  },

  enable: (id: number) => {
    return apiClient.post(`/rules/${id}/enable`)
  },

  disable: (id: number) => {
    return apiClient.post(`/rules/${id}/disable`)
  },

  test: (id: number, data: { message?: string; userId?: number }) => {
    return apiClient.post<{ data: RuleTestResult }>(`/rules/${id}/test`, data)
  },

  updatePriority: (data: { rules: Array<{ id: number; priority: number }> }) => {
    return apiClient.put('/rules/priority', data)
  },

  getHitStats: () => {
    return apiClient.get<{ data: Array<{ ruleId: number; ruleName: string; hitCount: number }> }>('/rules/hit-stats')
  },

  listModeration: (params?: {
    cursor?: string
    limit?: number
    rule_type?: ModerationRuleType
    level?: ModerationRuleLevel | ''
    enabled?: boolean
    group_id?: number
  }) => {
    return apiClient.get<{ data: ModerationRule[]; total: number }>('/rules', { params })
  },

  createModeration: (data: ModerationRuleFormData) => {
    return apiClient.post<ModerationRule>('/rules', data)
  },

  updateModeration: (id: number, data: Partial<ModerationRuleFormData>) => {
    return apiClient.put<ModerationRule>(`/rules/${id}`, data)
  },

  deleteModeration: (id: number) => {
    return apiClient.delete(`/rules/${id}`)
  },
}
