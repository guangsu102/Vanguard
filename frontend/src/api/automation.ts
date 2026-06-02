import apiClient from './client'

export interface AutomationRunResult {
  queued?: boolean
  status?: string
  task_name?: string
  task_id?: string
  payload?: Record<string, any>
  processed: number
  created: number
  updated: number
  succeeded: number
  skipped: number
  failed: number
  errors: string[]
  details: Record<string, any>[]
}

export interface AccountOperationConfig {
  id: number
  account_id: number
  auto_join_enabled: boolean
  auto_ads_enabled: boolean
  max_groups_per_day: number
  max_groups_total: number
  join_interval_min_seconds: number
  join_interval_max_seconds: number
  next_join_after?: string
  max_messages_per_day: number
  message_interval_seconds: number
  quiet_hours_start?: string
  quiet_hours_end?: string
  keyword_types: string[]
  keyword_auto_replenish_enabled: boolean
  keyword_replenish_requires_review: boolean
  risk_level: string
  enabled: boolean
}

export interface AdCreative {
  id: number
  name: string
  content: string
  creative_type: 'text' | 'image' | 'mixed'
  media_url?: string
  link_url?: string
  weight: number
  enabled: boolean
  created_at: string
  updated_at: string
}

export interface AdCampaign {
  id: number
  name: string
  enabled: boolean
  status: string
  send_mode: 'after_join' | 'interval' | 'scheduled'
  target_group_levels: string[]
  start_at?: string
  end_at?: string
  min_wait_after_join_minutes: number
  interval_minutes: number
  scheduled_times: string[]
  max_sends_per_group_per_day: number
  max_sends_per_account_per_day: number
}

export interface AdCampaignCreatePayload {
  name: string
  enabled?: boolean
  status?: string
  send_mode?: 'after_join' | 'interval' | 'scheduled'
  target_group_levels?: string[]
  start_at?: string
  end_at?: string
  min_wait_after_join_minutes?: number
  interval_minutes?: number
  scheduled_times?: string[]
  max_sends_per_group_per_day?: number
  max_sends_per_account_per_day?: number
}

export interface AccountAdBinding {
  id: number
  account_id: number
  ad_campaign_id: number
  creative_id?: number
  enabled: boolean
  priority: number
}

export const automationApi = {
  getAccountOperationConfig: (accountId: number) => {
    return apiClient.get<{ data: AccountOperationConfig }>(`/automation/accounts/${accountId}/operation-config`)
  },

  updateAccountOperationConfig: (accountId: number, data: Partial<AccountOperationConfig>) => {
    return apiClient.put<{ data: AccountOperationConfig }>(`/automation/accounts/${accountId}/operation-config`, data)
  },

  replenishKeywords: (data: { min_per_type?: Record<string, number>; generate_counts?: Record<string, number>; auto_approve?: boolean }) => {
    return apiClient.post<{ data: AutomationRunResult }>('/automation/keywords/replenish', data)
  },

  runAutoJoin: (data: { max_accounts?: number; keywords_per_account?: number; max_groups_per_keyword?: number; dry_run?: boolean }) => {
    return apiClient.post<{ data: AutomationRunResult }>('/automation/auto-join/run', data)
  },

  runAds: (data: { max_deliveries?: number; dry_run?: boolean }) => {
    return apiClient.post<{ data: AutomationRunResult }>('/automation/ads/run', data)
  },

  getAutoJoinAttempts: (params?: { account_id?: number; status?: string; limit?: number }) => {
    return apiClient.get<{ data: any[] }>('/automation/auto-join/attempts', { params })
  },

  getCreatives: (params?: { enabled?: boolean; page?: number; page_size?: number }) => {
    return apiClient.get<{ data: AdCreative[]; total: number }>('/automation/ads/creatives', { params })
  },

  createCreative: (data: Omit<AdCreative, 'id' | 'created_at' | 'updated_at'>) => {
    return apiClient.post<{ data: AdCreative }>('/automation/ads/creatives', data)
  },

  getCampaigns: (params?: { enabled?: boolean; page?: number; page_size?: number }) => {
    return apiClient.get<{ data: AdCampaign[]; total: number }>('/automation/ads/campaigns', { params })
  },

  createCampaign: (data: AdCampaignCreatePayload) => {
    return apiClient.post<{ data: AdCampaign }>('/automation/ads/campaigns', data)
  },

  getBindings: (params?: { account_id?: number; campaign_id?: number }) => {
    return apiClient.get<{ data: AccountAdBinding[] }>('/automation/ads/bindings', { params })
  },

  createBinding: (data: Omit<AccountAdBinding, 'id'>) => {
    return apiClient.post<{ data: AccountAdBinding }>('/automation/ads/bindings', data)
  },

  getDeliveryLogs: (params?: { account_id?: number; campaign_id?: number; status?: string; limit?: number }) => {
    return apiClient.get<{ data: any[] }>('/automation/ads/delivery-logs', { params })
  },
}
