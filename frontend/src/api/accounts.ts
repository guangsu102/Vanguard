import apiClient from './client'

export type AccountStatus = 'offline' | 'online' | 'working' | 'idle' | 'error' | 'banned'
export type AccountType = 'promoter' | 'guardian_bot'

export interface Account {
  id: number
  phone?: string
  identifier: string
  display_name?: string
  account_type: AccountType
  status: AccountStatus
  country_code: string
  country_name?: string
  api_config_name: string
  fingerprint_id?: string
  session_name: string
  is_active: boolean
  connection_count: number
  error_count: number
  last_active_at?: string
  last_connected_at?: string
  created_at: string
  updated_at: string
}

export interface AccountListParams {
  cursor?: string
  limit?: number
  status_filter?: AccountStatus | ''
  country_code?: string
  api_config_name?: string
  account_type?: AccountType
  search?: string
}

export interface AccountFormData {
  phone?: string
  identifier?: string
  display_name?: string
  account_type?: AccountType
  api_config_name?: string
  country_code?: string
  country_name?: string
  session_name?: string
}

export interface AccountUpdateData {
  display_name?: string
  country_code?: string
  fingerprint_id?: string
  is_active?: boolean
}

export interface AccountListPayload {
  list: Account[]
  total: number
  nextCursor?: string | null
  hasMore: boolean
}

const normalizeAccount = (item: any): Account => ({
  id: Number(item.id),
  phone: item.phone || undefined,
  identifier: item.identifier || item.phone || `account-${item.id}`,
  display_name: item.display_name || undefined,
  account_type: item.account_type || 'promoter',
  status: item.status || 'offline',
  country_code: item.country_code || 'US',
  country_name: item.country_name || undefined,
  api_config_name: item.api_config_name || 'default',
  fingerprint_id: item.fingerprint_id || undefined,
  session_name: item.session_name || '',
  is_active: Boolean(item.is_active),
  connection_count: Number(item.connection_count ?? 0),
  error_count: Number(item.error_count ?? 0),
  last_active_at: item.last_active_at || undefined,
  last_connected_at: item.last_connected_at || undefined,
  created_at: item.created_at || '',
  updated_at: item.updated_at || '',
})

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

  getById: async (id: number): Promise<Account> => {
    const response = await apiClient.get(`/accounts/${id}`)
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
}
