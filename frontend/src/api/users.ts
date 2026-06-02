import apiClient from './client'

export type UserStatus = 'active' | 'muted' | 'banned' | 'inactive'

export interface User {
  id: number
  tgUserId: string
  username?: string
  displayName?: string
  avatar?: string
  status: UserStatus
  sourceGroupId?: number
  sourceGroupName?: string
  registeredAt: string
  lastActiveAt?: string
  createdAt: string
}

export interface UserActivity {
  id: number
  type: string
  description: string
  timestamp: string
  metadata?: Record<string, any>
}

export interface UserListParams {
  page?: number
  pageSize?: number
  status?: UserStatus
  sourceGroupId?: number
  registeredFrom?: string
  registeredTo?: string
  keyword?: string
}

export interface UserListResponse {
  list: User[]
  total: number
  page: number
  pageSize: number
}

export const usersApi = {
  list: (params?: UserListParams) => {
    return apiClient.get<{ data: UserListResponse }>('/users', { params })
  },

  getById: (id: number) => {
    return apiClient.get<{ data: User }>(`/users/${id}`)
  },

  update: (id: number, data: { displayName?: string; status?: UserStatus }) => {
    return apiClient.put<{ data: User }>(`/users/${id}`, data)
  },

  mute: (id: number, data?: { duration?: number; reason?: string }) => {
    return apiClient.post(`/users/${id}/mute`, data)
  },

  unmute: (id: number) => {
    return apiClient.post(`/users/${id}/unmute`)
  },

  blacklist: (id: number, data?: { reason?: string }) => {
    return apiClient.post(`/users/${id}/blacklist`, data)
  },

  removeBlacklist: (id: number) => {
    return apiClient.post(`/users/${id}/remove-blacklist`)
  },

  getActivities: (id: number, params?: { page?: number; pageSize?: number }) => {
    return apiClient.get<{ data: { list: UserActivity[]; total: number } }>(`/users/${id}/activities`, { params })
  },

  export: (params?: { status?: UserStatus }) => {
    return apiClient.get('/users/export', { params, responseType: 'blob' })
  },
}
