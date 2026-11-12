import apiClient from './client'
import type { AxiosResponse } from 'axios'

/** Lifecycle states persisted on the backend user record. */
export type UserState = 'new' | 'pending' | 'active' | 'silent' | 'churned' | 'blocked'

export interface User {
  id: number
  telegramId: string
  xboardUserId?: number | null
  username?: string | null
  state: UserState
  warningCount: number
  mutedUntil?: string | null
  trialStartedAt?: string | null
  trialExpiresAt?: string | null
  createdAt: string
  updatedAt: string
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
  state?: UserState | ''
  keyword?: string
  registeredFrom?: string
  registeredTo?: string
}

/** Raw user payload returned by the backend (snake_case). */
interface ServerUser {
  id: number
  telegram_id: number
  xboard_user_id?: number | null
  username?: string | null
  state: UserState
  warning_count: number
  muted_until?: string | null
  trial_started_at?: string | null
  trial_expires_at?: string | null
  created_at: string
  updated_at: string
}

/** Raw violation payload returned by the backend (snake_case). */
interface ServerViolation {
  id: number
  user_id: number
  group_id: number
  rule_type: string
  rule_pattern?: string | null
  content?: string | null
  action_taken: string
  action_duration?: number | null
  created_at: string
}

const toUser = (raw: ServerUser): User => ({
  id: raw.id,
  telegramId: raw.telegram_id != null ? String(raw.telegram_id) : '',
  xboardUserId: raw.xboard_user_id,
  username: raw.username,
  state: raw.state,
  warningCount: raw.warning_count ?? 0,
  mutedUntil: raw.muted_until,
  trialStartedAt: raw.trial_started_at,
  trialExpiresAt: raw.trial_expires_at,
  createdAt: raw.created_at,
  updatedAt: raw.updated_at,
})

const toActivity = (raw: ServerViolation): UserActivity => ({
  id: raw.id,
  type: raw.rule_type,
  description: raw.content || raw.action_taken,
  timestamp: raw.created_at,
  metadata: {
    groupId: raw.group_id,
    rulePattern: raw.rule_pattern,
    actionTaken: raw.action_taken,
    actionDuration: raw.action_duration,
  },
})

/** A user is considered muted when muted_until is set and in the future. */
export const isUserMuted = (user: Pick<User, 'mutedUntil'>): boolean => {
  if (!user.mutedUntil) return false
  const until = new Date(user.mutedUntil).getTime()
  return Number.isFinite(until) && until > Date.now()
}

export const isUserBlocked = (user: Pick<User, 'state'>): boolean => user.state === 'blocked'

export const usersApi = {
  list: async (params?: UserListParams) => {
    const query: Record<string, unknown> = {
      page: params?.page,
      page_size: params?.pageSize,
    }
    if (params?.state) query.state_filter = params.state
    if (params?.keyword) query.search = params.keyword
    if (params?.registeredFrom) query.registered_from = params.registeredFrom
    if (params?.registeredTo) query.registered_to = params.registeredTo

    const res = await apiClient.get<{ data?: ServerUser[]; total?: number }>('/users', { params: query })
    const rawList = Array.isArray(res.data?.data) ? res.data.data : []
    const total = Number(res.data?.total ?? rawList.length)
    // Normalize into the { data: { list, total } } shape consumed by stores.
    res.data = { code: 0, message: 'success', data: { list: rawList.map(toUser), total } } as any
    return res as unknown as AxiosResponse<{ data: { list: User[]; total: number } }>
  },

  getById: async (id: number) => {
    const res = await apiClient.get<ServerUser>(`/users/${id}`)
    const raw = ((res.data as unknown as { data?: ServerUser })?.data ?? res.data) as ServerUser
    // Backend returns the user object directly; wrap it for the store contract.
    res.data = { code: 0, message: 'success', data: toUser(raw) } as any
    return res as unknown as AxiosResponse<{ data: User }>
  },

  update: (id: number, data: { state?: UserState }) => {
    return apiClient.put(`/users/${id}/state`, { state: data.state })
  },

  mute: (id: number, data?: { durationHours?: number; reason?: string }) => {
    return apiClient.post(`/users/${id}/mute`, {
      duration_hours: data?.durationHours ?? 24,
      reason: data?.reason,
    })
  },

  unmute: (id: number) => {
    return apiClient.post(`/users/${id}/unmute`)
  },

  blacklist: (id: number) => {
    return apiClient.post(`/users/${id}/block`)
  },

  removeBlacklist: (id: number) => {
    return apiClient.post(`/users/${id}/unblock`)
  },

  getActivities: async (id: number, params?: { page?: number; pageSize?: number }) => {
    const res = await apiClient.get<{ data?: ServerViolation[]; total?: number }>(`/users/${id}/violations`, {
      params: { limit: params?.pageSize ?? 20 },
    })
    const rawList = Array.isArray(res.data?.data) ? res.data.data : []
    const total = Number(res.data?.total ?? rawList.length)
    res.data = { code: 0, message: 'success', data: { list: rawList.map(toActivity), total } } as any
    return res as unknown as AxiosResponse<{ data: { list: UserActivity[]; total: number } }>
  },
}
