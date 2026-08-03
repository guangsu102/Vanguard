import apiClient from './client'

export type CampaignType = 'discount'
export type CampaignScope = 'global' | 'managed_group'
export type CouponProvider = 'xboard' | 'sub2api'
export type Sub2APICouponType = 'balance' | 'concurrency' | 'subscription' | 'invitation'
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
export type CampaignTargetUserState =
  | 'new'
  | 'pending'
  | 'active'
  | 'silent'
  | 'churned'
  | 'blocked'

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
  broadcast_message?: string
  delay_minutes?: number
  schedule_times?: string[]
  interval_minutes?: number
  verified_only: boolean
  once_per_user: boolean
  min_join_minutes?: number
  target_user_states?: CampaignTargetUserState[]
  target_limit?: number
  min_account_age_minutes?: number
  coupon_provider?: CouponProvider
  coupon_amount?: number
  coupon_quantity?: number
  coupon_type?: Sub2APICouponType
  coupon_batch_key?: string
  sub2api_group_id?: number
  sub2api_validity_days?: number
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
  broadcast_message?: string
  delay_minutes?: number
  schedule_times?: string[]
  interval_minutes?: number
  verified_only?: boolean
  once_per_user?: boolean
  min_join_minutes?: number
  target_user_states?: CampaignTargetUserState[]
  target_limit?: number
  min_account_age_minutes?: number
  coupon_provider?: CouponProvider
  coupon_amount?: number
  coupon_quantity?: number
  coupon_type?: Sub2APICouponType
  coupon_batch_key?: string
  sub2api_group_id?: number
  sub2api_validity_days?: number
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

const campaignTargetUserStateValues: CampaignTargetUserState[] = [
  'new',
  'pending',
  'active',
  'silent',
  'churned',
  'blocked',
]

const normalizeTargetUserStates = (item: any): CampaignTargetUserState[] | undefined => {
  const eligibilityPolicy = item.eligibility_policy_json || {}
  const rawStates = Array.isArray(item.target_user_states)
    ? item.target_user_states
    : Array.isArray(eligibilityPolicy.target_user_states)
      ? eligibilityPolicy.target_user_states
      : undefined

  if (!rawStates) return undefined
  const states = rawStates.filter((value: unknown): value is CampaignTargetUserState => {
    return typeof value === 'string' && campaignTargetUserStateValues.includes(value as CampaignTargetUserState)
  })
  return states.length ? states : undefined
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
  broadcast_message: item.broadcast_message || undefined,
  delay_minutes: typeof item.delay_minutes === 'number' ? item.delay_minutes : undefined,
  schedule_times: Array.isArray(item.schedule_times) ? item.schedule_times : undefined,
  interval_minutes: typeof item.interval_minutes === 'number' ? item.interval_minutes : undefined,
  verified_only: Boolean(item.verified_only),
  once_per_user: Boolean(item.once_per_user),
  min_join_minutes: typeof item.min_join_minutes === 'number' ? item.min_join_minutes : undefined,
  target_user_states: normalizeTargetUserStates(item),
  target_limit: typeof item.target_limit === 'number'
    ? item.target_limit
    : typeof item.eligibility_policy_json?.target_limit === 'number'
      ? item.eligibility_policy_json.target_limit
      : undefined,
  min_account_age_minutes: typeof item.min_account_age_minutes === 'number'
    ? item.min_account_age_minutes
    : typeof item.eligibility_policy_json?.min_account_age_minutes === 'number'
      ? item.eligibility_policy_json.min_account_age_minutes
      : undefined,
  coupon_provider: item.coupon_provider || item.reward_policy_json?.coupon_provider || 'xboard',
  coupon_amount: typeof item.coupon_amount === 'number'
    ? item.coupon_amount
    : typeof item.reward_policy_json?.coupon_amount === 'number'
      ? item.reward_policy_json.coupon_amount
      : undefined,
  coupon_quantity: typeof item.coupon_quantity === 'number'
    ? item.coupon_quantity
    : typeof item.reward_policy_json?.coupon_quantity === 'number'
      ? item.reward_policy_json.coupon_quantity
      : 1,
  coupon_type: item.coupon_type || item.reward_policy_json?.coupon_type || 'balance',
  coupon_batch_key: item.coupon_batch_key || item.reward_policy_json?.coupon_batch_key || undefined,
  sub2api_group_id: typeof item.sub2api_group_id === 'number'
    ? item.sub2api_group_id
    : typeof item.reward_policy_json?.sub2api_group_id === 'number'
      ? item.reward_policy_json.sub2api_group_id
      : undefined,
  sub2api_validity_days: typeof item.sub2api_validity_days === 'number'
    ? item.sub2api_validity_days
    : typeof item.reward_policy_json?.sub2api_validity_days === 'number'
      ? item.reward_policy_json.sub2api_validity_days
      : undefined,
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
