import apiClient from './client'

export interface PunishmentRecord {
  id: number
  user_id: number
  group_id: number
  rule_type: string
  rule_pattern?: string
  content?: string
  action: 'warn' | 'mute' | 'ban' | 'kick'
  action_duration?: number
  created_at: string
}

export interface PunishmentCreateParams {
  user_id: number
  group_id: number
  rule_type: string
  action: 'warn' | 'mute' | 'ban' | 'kick'
  content?: string
  duration?: number
}

export interface PunishmentStats {
  warn?: number
  mute?: number
  ban?: number
  kick?: number
}

export const punishmentsApi = {
  record: (data: PunishmentCreateParams) => {
    return apiClient.post<{ data: { id: number } }>('/punishments', data)
  },

  getHistory: (params: { user_id: number; group_id?: number; limit?: number }) => {
    return apiClient.get<{
      data: PunishmentRecord[]
      total: number
    }>('/punishments/history', { params })
  },

  getCount: (params: { user_id: number; group_id?: number }) => {
    return apiClient.get<{ data: { count: number } }>('/punishments/count', { params })
  },

  getStatsByAction: (params?: { group_id?: number; start_date?: string; end_date?: string }) => {
    return apiClient.get<{ data: PunishmentStats }>('/punishments/stats/by-action', { params })
  },

  getStatsByRuleType: (params?: { group_id?: number }) => {
    return apiClient.get<{ data: Record<string, number> }>('/punishments/stats/by-rule-type', { params })
  },
}
