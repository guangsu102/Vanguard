import apiClient from './client'

export type CampaignType = 'discount'
export type CampaignScope = 'global' | 'managed_group'
export const campaignTriggerTimingValues = [
  'after_register',
  'immediate',
  'delayed',
  'scheduled',
  'manual',
  'periodic',
] as const
export type CampaignTriggerTiming = typeof campaignTriggerTimingValues[number]
export const campaignDistributionModeValues = [
  'welcome',
  'delayed',
  'scheduled',
  'manual',
  'periodic',
] as const
export type CampaignDistributionMode = typeof campaignDistributionModeValues[number]
export type ManagedGroupTriggerEvent =
  | 'user_joined'
  | 'verification_passed'
  | 'new_member_delay'
  | 'scheduled'
  | 'manual_broadcast'
  | 'periodic'
export type ManagedGroupTriggerTiming = Exclude<CampaignTriggerTiming, 'after_register'>

export interface Campaign {
  id: number
  name: string
  campaign_type: CampaignType
  campaign_scope: CampaignScope
  trigger_timing: CampaignTriggerTiming
  trigger_event?: string
  validity_hours: number
  target_group_ids?: number[]
  bot_account_id?: number
  distribution_mode?: CampaignDistributionMode
  reward_policy_json?: Record<string, any>
  broadcast_policy_json?: Record<string, any>
  eligibility_policy_json?: Record<string, any>
  enabled: boolean
  created_at: string
  updated_at: string
}

export interface CampaignListParams {
  cursor?: string
  limit?: number
  campaign_type?: CampaignType | ''
  campaign_scope?: CampaignScope | ''
  enabled?: boolean
  search?: string
}

export interface CampaignFormData {
  name: string
  campaign_type: CampaignType
  campaign_scope: CampaignScope
  trigger_timing: CampaignTriggerTiming
  trigger_event?: string
  validity_hours: number
  target_group_ids?: number[]
  bot_account_id?: number
  distribution_mode?: CampaignDistributionMode
  reward_policy_json?: Record<string, any>
  broadcast_policy_json?: Record<string, any>
  eligibility_policy_json?: Record<string, any>
  enabled?: boolean
}

export interface CampaignStats {
  campaign_id: number
  campaign_name: string
  campaign_type: string
  enabled: boolean
  total_tracked: number
  registered: number
  converted: number
  trial_granted: number
  coupon_granted: number
  conversion_rate: number
  by_source: Record<string, { total: number; registered: number; converted: number }>
}

export interface CampaignListPayload {
  list: Campaign[]
  total: number
  nextCursor?: string | null
  hasMore: boolean
}

const isCampaignTriggerTiming = (value: unknown): value is CampaignTriggerTiming => {
  return typeof value === 'string' && campaignTriggerTimingValues.includes(value as CampaignTriggerTiming)
}

const isCampaignDistributionMode = (value: unknown): value is CampaignDistributionMode => {
  return typeof value === 'string' && campaignDistributionModeValues.includes(value as CampaignDistributionMode)
}

const normalizeCampaign = (item: any): Campaign => ({
  id: Number(item.id),
  name: item.name || '',
  campaign_type: 'discount',
  campaign_scope: item.campaign_scope || 'global',
  trigger_timing: isCampaignTriggerTiming(item.trigger_timing) ? item.trigger_timing : 'after_register',
  trigger_event: item.trigger_event || undefined,
  validity_hours: Number(item.validity_hours ?? 168),
  target_group_ids: Array.isArray(item.target_group_ids) ? item.target_group_ids : undefined,
  bot_account_id: item.bot_account_id ?? undefined,
  distribution_mode: isCampaignDistributionMode(item.distribution_mode) ? item.distribution_mode : undefined,
  reward_policy_json: item.reward_policy_json || undefined,
  broadcast_policy_json: item.broadcast_policy_json || undefined,
  eligibility_policy_json: item.eligibility_policy_json || undefined,
  enabled: Boolean(item.enabled),
  created_at: item.created_at || '',
  updated_at: item.updated_at || '',
})

export const campaignsApi = {
  list: async (params?: CampaignListParams): Promise<CampaignListPayload> => {
    const response = await apiClient.get('/campaigns', { params })
    return {
      list: Array.isArray(response.data?.data) ? response.data.data.map(normalizeCampaign) : [],
      total: Number(response.data?.total ?? 0),
      nextCursor: response.data?.next_cursor ?? null,
      hasMore: Boolean(response.data?.has_more),
    }
  },

  create: async (data: CampaignFormData): Promise<Campaign> => {
    const response = await apiClient.post('/campaigns', data)
    return normalizeCampaign(response.data)
  },

  getById: async (id: number): Promise<Campaign> => {
    const response = await apiClient.get(`/campaigns/${id}`)
    return normalizeCampaign(response.data)
  },

  update: async (id: number, data: Partial<CampaignFormData>): Promise<Campaign> => {
    const response = await apiClient.put(`/campaigns/${id}`, data)
    return normalizeCampaign(response.data)
  },

  delete: (id: number) => {
    return apiClient.delete(`/campaigns/${id}`)
  },

  toggle: async (id: number): Promise<{ campaign_id: number; enabled: boolean }> => {
    const response = await apiClient.post(`/campaigns/${id}/toggle`)
    return response.data.data
  },

  trigger: async (
    id: number,
    payload?: { user_id?: number }
  ): Promise<{
    campaign_id: number
    queued?: boolean
    status?: string
    task_name?: string
    task_id?: string
    triggered?: boolean
    delivered?: boolean
    reward_granted?: boolean
    reason?: string
  }> => {
    const params = payload?.user_id ? { user_id: payload.user_id } : undefined
    const response = await apiClient.post(`/campaigns/${id}/trigger`, null, { params })
    return response.data.data
  },

  getStats: async (id: number): Promise<CampaignStats> => {
    const response = await apiClient.get(`/campaigns/${id}/stats`)
    return response.data.data
  },
}
