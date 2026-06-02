import apiClient from './client'

export interface BroadcastRecord {
  id: number
  content: string
  broadcast_type: string
  target_group_count: number
  success_count: number
  failed_count: number
  status: 'pending' | 'queued' | 'sending' | 'completed' | 'failed'
  target_groups?: number[]
  created_at: string
  completed_at?: string
}

export interface BroadcastCreateParams {
  content: string
  target_groups: number[]
  broadcast_type?: string
}

export interface BroadcastListParams {
  limit?: number
  offset?: number
  broadcast_type?: string
}

export const broadcastsApi = {
  create: (data: BroadcastCreateParams) => {
    return apiClient.post<{ data: BroadcastRecord }>('/broadcasts', data)
  },

  list: (params?: BroadcastListParams) => {
    return apiClient.get<{
      data: BroadcastRecord[]
      total: number
    }>('/broadcasts', { params })
  },

  get: (id: number) => {
    return apiClient.get<{ data: BroadcastRecord }>(`/broadcasts/${id}`)
  },

  execute: (id: number) => {
    return apiClient.post<{
      data: { id: number; status: string; queued: boolean; task_name: string; task_id: string }
    }>(`/broadcasts/${id}/execute`)
  },
}
