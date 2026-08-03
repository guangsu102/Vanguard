import apiClient from './client'

export type ProxyProtocol = 'http' | 'https' | 'socks5'
export type ProxyStatus = 'active' | 'inactive' | 'error'
export type ProxyType = 'residential' | 'datacenter' | 'mobile'

export interface Proxy {
  id: number
  address: string
  port: number
  protocol: ProxyProtocol
  username?: string
  password?: string
  proxy_type: ProxyType
  country: string
  countryName?: string
  latency?: number
  status: ProxyStatus
  bindAccountId?: number
  bindAccountPhone?: string
  bindAccountCount: number
  bindAccounts: Array<{
    id: number
    phone?: string
    identifier: string
    status: string
  }>
  remainingBindSlots: number
  lastCheckedAt?: string
  createdAt: string
  updatedAt: string
}

export interface ProxyListParams {
  page?: number
  pageSize?: number
  protocol?: ProxyProtocol
  status?: ProxyStatus
  keyword?: string
}

export interface ProxyListResponse {
  list: Proxy[]
  total: number
  page: number
  pageSize: number
}

export interface ProxyFormData {
  address: string
  port: number
  protocol: ProxyProtocol
  username?: string
  password?: string
  proxy_type?: ProxyType
  country?: string
  country_name?: string
}

export const proxiesApi = {
  list: (params?: ProxyListParams) => {
    return apiClient.get<{ data: ProxyListResponse }>('/proxies', { params })
  },

  create: (data: ProxyFormData) => {
    return apiClient.post<{ data: Proxy }>('/proxies', data)
  },

  getById: (id: number) => {
    return apiClient.get<{ data: Proxy }>(`/proxies/${id}`)
  },

  update: (id: number, data: Partial<ProxyFormData>) => {
    return apiClient.put<{ data: Proxy }>(`/proxies/${id}`, data)
  },

  delete: (id: number) => {
    return apiClient.delete(`/proxies/${id}`)
  },

  test: (id: number) => {
    return apiClient.post<{ data: { latency: number } }>(`/proxies/${id}/test`)
  },

  import: (data: FormData) => {
    return apiClient.post<{ data: { success: number; failed: number } }>('/proxies/import', data, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
  },

  refreshStatus: () => {
    return apiClient.post('/proxies/refresh-status')
  },

  export: () => {
    return apiClient.get('/proxies/export', { responseType: 'blob' })
  },

  batchValidate: (proxyIds?: number[]) => {
    return apiClient.post<{
      data: { total_proxies: number; queued: boolean; status: string; task_name: string; task_id: string }
    }>('/proxies/batch-validate', proxyIds)
  },
}
