import apiClient from './client'
import type { AccountPersonaSummary } from './accountPersonas'

export type AccountStatus = 'offline' | 'online' | 'working' | 'idle' | 'error' | 'banned'
export type AccountType = 'promoter' | 'guardian_bot'
export type AccountAssetTier = 'unknown' | 'month_1' | 'month_3_6' | 'year_1' | 'year_2' | 'year_3_plus'
export type AccountWarmupStage = 'observe' | 'seed' | 'soft' | 'ramp' | 'normal' | 'cooldown'
export type ProxyMode = 'dynamic' | 'static' | 'none'
export type AccountRiskLevel = 'normal' | 'watch' | 'limited' | 'frozen' | 'quarantined'
export type AccountOperationMode = 'growth' | 'ad_only'
export type AccountListPersonaSummary = Omit<AccountPersonaSummary, 'effective_enabled'> &
  Partial<Pick<AccountPersonaSummary, 'effective_enabled'>>

export interface Account {
  id: number
  phone?: string
  identifier: string
  display_name?: string
  profile_bio?: string
  profile_bio_synced_at?: string
  account_type: AccountType
  operation_mode: AccountOperationMode
  asset_tier: AccountAssetTier
  registered_at?: string
  asset_verified_at?: string
  asset_note?: string
  managed_started_at?: string
  warmup_stage: AccountWarmupStage
  warmup_stage_updated_at?: string
  warmup_hold_until?: string
  warmup_note?: string
  status: AccountStatus
  country_code: string
  country_name?: string
  api_config_name: string
  fingerprint_id?: string
  session_name: string
  proxy_mode: ProxyMode
  static_proxy_id?: number
  static_proxy_address?: string
  is_active: boolean
  connection_count: number
  error_count: number
  last_active_at?: string
  last_connected_at?: string
  created_at: string
  updated_at: string
  persona?: AccountListPersonaSummary
}

export interface AccountListParams {
  cursor?: string
  limit?: number
  status_filter?: AccountStatus | ''
  country_code?: string
  api_config_name?: string
  account_type?: AccountType
  asset_tier?: AccountAssetTier | ''
  search?: string
}

export interface AccountFormData {
  phone?: string
  identifier?: string
  display_name?: string
  profile_bio?: string
  account_type?: AccountType
  operation_mode?: AccountOperationMode
  asset_tier?: AccountAssetTier
  registered_at?: string
  asset_note?: string
  managed_started_at?: string
  warmup_hold_until?: string
  warmup_note?: string
  api_config_name?: string
  country_code?: string
  country_name?: string
  session_name?: string
  proxy_mode?: ProxyMode
  static_proxy_id?: number
}

export interface AccountUpdateData {
  display_name?: string
  profile_bio?: string
  asset_tier?: AccountAssetTier
  registered_at?: string
  asset_note?: string
  managed_started_at?: string
  warmup_hold_until?: string
  warmup_note?: string
  country_code?: string
  fingerprint_id?: string
  is_active?: boolean
  proxy_mode?: ProxyMode
  static_proxy_id?: number
}


export interface AccountRiskEvent {
  id: number
  account_id?: number
  action?: string
  status: string
  reason?: string
  target_type?: string
  target_id?: string
  fingerprint_id?: string
  proxy_mode?: string
  proxy_id?: number
  proxy_country?: string
  details?: string
  created_at: string
}

export interface AccountEnvironmentEvent {
  id: number
  account_id?: number
  event_type: string
  status: string
  reason?: string
  proxy_mode?: string
  proxy_id?: number
  proxy_country?: string
  fingerprint_id?: string
  device_model?: string
  system_version?: string
  app_version?: string
  details?: string
  created_at: string
}

export interface AccountRiskSummary {
  account_id: number
  risk_score: number
  risk_level?: AccountRiskLevel
  risk_pause_until?: string
  risk_recovery_until?: string
  risk_reason?: string
  last_risk_event_at?: string
  last_risk_decay_at?: string
  asset_tier?: AccountAssetTier
  registered_at?: string
  asset_verified_at?: string
  asset_note?: string
  managed_started_at?: string
  warmup_stage?: AccountWarmupStage
  warmup_stage_updated_at?: string
  warmup_hold_until?: string
  warmup_note?: string
  blocked_count: number
  failure_count: number
  today_usage?: AccountRiskDailyStat[]
  fingerprint_id?: string
  device_model?: string
  system_version?: string
  app_version?: string
  latest_risk_event?: AccountRiskEvent
  latest_environment_event?: AccountEnvironmentEvent
}

export interface AccountRiskDailyStat {
  id: number
  account_id?: number
  stat_date: string
  action: string
  status: string
  target_type?: string
  count: number
  last_reason?: string
  first_seen_at: string
  last_seen_at: string
}

export interface AccountRiskEventsPayload {
  risk_events: AccountRiskEvent[]
  environment_events: AccountEnvironmentEvent[]
}

export interface AccountListPayload {
  list: Account[]
  total: number
  nextCursor?: string | null
  hasMore: boolean
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  Boolean(value) && typeof value === 'object' && !Array.isArray(value)

const optionalString = (value: unknown): string | undefined =>
  typeof value === 'string' && value.length > 0 ? value : undefined

const normalizePersonaSummary = (
  value: Record<string, unknown>,
  accountId: number,
): AccountListPersonaSummary | undefined => {
  const flatFields = [
    'persona_configured',
    'persona_name',
    'persona_revision',
    'persona_applicable',
  ] as const
  if (!flatFields.some((field) => Object.prototype.hasOwnProperty.call(value, field))) {
    return undefined
  }
  return {
    account_id: accountId,
    configured: value.persona_configured === true,
    name: typeof value.persona_name === 'string' ? value.persona_name : null,
    revision: Number(value.persona_revision ?? 0),
    applicable: value.persona_applicable === true,
  }
}

const normalizeAccount = (item: unknown): Account => {
  const value = isRecord(item) ? item : {}
  const id = Number(value.id ?? 0)
  const phone = optionalString(value.phone)
  return {
    id,
    phone,
    identifier: optionalString(value.identifier) ?? phone ?? `account-${id}`,
    display_name: optionalString(value.display_name),
    profile_bio: optionalString(value.profile_bio),
    profile_bio_synced_at: optionalString(value.profile_bio_synced_at),
    account_type: value.account_type === 'guardian_bot' ? 'guardian_bot' : 'promoter',
    operation_mode: value.operation_mode === 'ad_only' ? 'ad_only' : 'growth',
    asset_tier: typeof value.asset_tier === 'string'
      ? value.asset_tier as AccountAssetTier
      : 'unknown',
    registered_at: optionalString(value.registered_at),
    asset_verified_at: optionalString(value.asset_verified_at),
    asset_note: optionalString(value.asset_note),
    managed_started_at: optionalString(value.managed_started_at),
    warmup_stage: typeof value.warmup_stage === 'string'
      ? value.warmup_stage as AccountWarmupStage
      : 'observe',
    warmup_stage_updated_at: optionalString(value.warmup_stage_updated_at),
    warmup_hold_until: optionalString(value.warmup_hold_until),
    warmup_note: optionalString(value.warmup_note),
    status: typeof value.status === 'string' ? value.status as AccountStatus : 'offline',
    country_code: optionalString(value.country_code) ?? 'US',
    country_name: optionalString(value.country_name),
    api_config_name: optionalString(value.api_config_name) ?? 'default',
    fingerprint_id: optionalString(value.fingerprint_id),
    session_name: optionalString(value.session_name) ?? '',
    proxy_mode: typeof value.proxy_mode === 'string' ? value.proxy_mode as ProxyMode : 'dynamic',
    static_proxy_id: value.static_proxy_id ? Number(value.static_proxy_id) : undefined,
    static_proxy_address: optionalString(value.static_proxy_address),
    is_active: Boolean(value.is_active),
    connection_count: Number(value.connection_count ?? 0),
    error_count: Number(value.error_count ?? 0),
    last_active_at: optionalString(value.last_active_at),
    last_connected_at: optionalString(value.last_connected_at),
    created_at: optionalString(value.created_at) ?? '',
    updated_at: optionalString(value.updated_at) ?? '',
    persona: normalizePersonaSummary(value, id),
  }
}

export const accountsApi = {
  list: async (params?: AccountListParams): Promise<AccountListPayload> => {
    const response = await apiClient.get('/accounts', { params })
    return {
      list: Array.isArray(response.data?.data) ? response.data.data.map(normalizeAccount) : [],
      total: Number(response.data?.total ?? 0),
      nextCursor: response.data?.next_cursor ?? null,
      hasMore: Boolean(response.data?.has_more),
    }
  },

  create: async (data: AccountFormData): Promise<Account> => {
    const response = await apiClient.post('/accounts', data)
    return normalizeAccount(response.data)
  },

  getById: async (id: number, signal?: AbortSignal): Promise<Account> => {
    const response = signal
      ? await apiClient.get(`/accounts/${id}`, { signal })
      : await apiClient.get(`/accounts/${id}`)
    return normalizeAccount(response.data)
  },

  update: async (id: number, data: AccountUpdateData): Promise<Account> => {
    const response = await apiClient.put(`/accounts/${id}`, data)
    return normalizeAccount(response.data)
  },

  delete: (id: number) => {
    return apiClient.delete(`/accounts/${id}`)
  },

  connect: async (id: number): Promise<{ account_id: number; status: AccountStatus }> => {
    const response = await apiClient.post(`/accounts/${id}/connect`)
    return response.data.data
  },

  disconnect: async (id: number): Promise<{ account_id: number; status: AccountStatus }> => {
    const response = await apiClient.post(`/accounts/${id}/disconnect`)
    return response.data.data
  },

  syncProfileBio: async (id: number, profileBio?: string): Promise<Account> => {
    const payload = profileBio === undefined ? undefined : { profile_bio: profileBio }
    const response = await apiClient.post(`/accounts/${id}/profile-bio/sync`, payload)
    return normalizeAccount(response.data)
  },

  getRiskSummary: async (id: number): Promise<AccountRiskSummary> => {
    const response = await apiClient.get(`/accounts/${id}/risk-summary`)
    return response.data.data
  },

  getRiskEvents: async (id: number, params?: { limit?: number }): Promise<AccountRiskEventsPayload> => {
    const response = await apiClient.get(`/accounts/${id}/risk-events`, { params })
    return response.data.data
  },

  manualAdjustRisk: async (
    id: number,
    data: { score_delta?: number; set_score?: number; target_level?: AccountRiskLevel; clear_pause?: boolean; reason?: string },
  ): Promise<AccountRiskSummary> => {
    const response = await apiClient.post(`/accounts/${id}/risk/manual-adjust`, data)
    return response.data.data
  },
  manualBan: async (id: number, reason = 'manual_ban'): Promise<Account> => {
    const response = await apiClient.post(`/accounts/${id}/manual-ban`, { reason })
    return normalizeAccount(response.data)
  },
  updateProxyPolicy: async (
    id: number,
    data: { proxy_mode: ProxyMode; static_proxy_id?: number },
  ): Promise<{ account_id: number; proxy_mode: ProxyMode; static_proxy_id?: number }> => {
    const response = await apiClient.put(`/accounts/${id}/proxy-policy`, data)
    return response.data.data
  },
}
