import apiClient from './client'

export type GroupStatus = 'active' | 'inactive' | 'banned' | 'left'
export type GroupLevel = 'A' | 'B' | 'C' | 'unrated'
export type DiscoverySource = 'manual' | 'keyword_search' | 'related_group' | 'import'

export interface GroupMetrics {
  adsSent: number
  groupReplies: number
  privateMessages: number
  repliedUsers: number
  registeredUsers: number
  paidUsers: number
  conversionRate: number
}

export interface Group {
  id: number
  chatId: string
  title?: string
  username?: string
  memberCount: number
  status: GroupStatus
  discoverySource: DiscoverySource | string
  sourceKeyword?: string
  level: GroupLevel
  levelScore: number
  ruleScore: number
  adminScore: number
  historyScore: number
  convertScore: number
  activityScore: number
  accountCount: number
  primaryAccountPhone?: string
  metrics: GroupMetrics
  lastMessageAt?: string
  createdAt: string
  updatedAt: string
}

export interface GroupMember {
  id: number
  accountId: number
  accountPhone?: string
  status: string
  joinMethod: string
  sourceKeyword?: string
  joinedAt?: string
  leftAt?: string
  note?: string
}

export interface GroupListParams {
  page?: number
  pageSize?: number
  status?: GroupStatus | ''
  level?: GroupLevel | ''
  keyword?: string
  sourceKeyword?: string
}

export interface GroupListResponse {
  list: Group[]
  total: number
  page: number
  pageSize: number
}

export interface GroupFormData {
  chatId: string
  title?: string
  username?: string
  memberCount?: number
  status?: GroupStatus
  discoverySource?: DiscoverySource | string
  sourceKeyword?: string
  accountId?: number
  joinMethod?: string
  level?: GroupLevel
}

const defaultMetrics: GroupMetrics = {
  adsSent: 0,
  groupReplies: 0,
  privateMessages: 0,
  repliedUsers: 0,
  registeredUsers: 0,
  paidUsers: 0,
  conversionRate: 0,
}

const toCamelGroup = (raw: any): Group => {
  const metrics = raw?.metrics || {}

  return {
    id: raw.id,
    chatId: String(raw.group_id ?? raw.chatId ?? ''),
    title: raw.title || '',
    username: raw.username || '',
    memberCount: Number(raw.member_count ?? raw.memberCount ?? 0),
    status: raw.status || 'active',
    discoverySource: raw.discovery_source || raw.discoverySource || 'manual',
    sourceKeyword: raw.source_keyword || raw.sourceKeyword || '',
    level: raw.level || 'unrated',
    levelScore: Number(raw.level_score ?? raw.levelScore ?? 0),
    ruleScore: Number(raw.rule_score ?? raw.ruleScore ?? 0),
    adminScore: Number(raw.admin_score ?? raw.adminScore ?? 0),
    historyScore: Number(raw.history_score ?? raw.historyScore ?? 0),
    convertScore: Number(raw.convert_score ?? raw.convertScore ?? 0),
    activityScore: Number(raw.activity_score ?? raw.activityScore ?? 0),
    accountCount: Number(raw.account_count ?? raw.accountCount ?? 0),
    primaryAccountPhone: raw.primary_account_phone || raw.primaryAccountPhone || '',
    metrics: {
      adsSent: Number(metrics.ads_sent ?? metrics.adsSent ?? defaultMetrics.adsSent),
      groupReplies: Number(metrics.group_replies ?? metrics.groupReplies ?? defaultMetrics.groupReplies),
      privateMessages: Number(metrics.private_messages ?? metrics.privateMessages ?? defaultMetrics.privateMessages),
      repliedUsers: Number(metrics.replied_users ?? metrics.repliedUsers ?? defaultMetrics.repliedUsers),
      registeredUsers: Number(metrics.registered_users ?? metrics.registeredUsers ?? defaultMetrics.registeredUsers),
      paidUsers: Number(metrics.paid_users ?? metrics.paidUsers ?? defaultMetrics.paidUsers),
      conversionRate: Number(metrics.conversion_rate ?? metrics.conversionRate ?? defaultMetrics.conversionRate),
    },
    lastMessageAt: raw.last_message_at || raw.lastMessageAt || '',
    createdAt: raw.created_at || raw.createdAt || '',
    updatedAt: raw.updated_at || raw.updatedAt || '',
  }
}

const toCamelMember = (raw: any): GroupMember => ({
  id: raw.id,
  accountId: raw.account_id ?? raw.accountId,
  accountPhone: raw.account_phone || raw.accountPhone || '',
  status: raw.status,
  joinMethod: raw.join_method || raw.joinMethod || '',
  sourceKeyword: raw.source_keyword || raw.sourceKeyword || '',
  joinedAt: raw.joined_at || raw.joinedAt || '',
  leftAt: raw.left_at || raw.leftAt || '',
  note: raw.note || '',
})

const toServerGroup = (data: GroupFormData | Partial<GroupFormData>) => ({
  group_id: data.chatId ? Number(data.chatId) : undefined,
  title: data.title,
  username: data.username,
  member_count: data.memberCount,
  status: data.status,
  discovery_source: data.discoverySource,
  source_keyword: data.sourceKeyword,
  account_id: data.accountId,
  join_method: data.joinMethod,
  level: data.level,
})

const clean = (value: Record<string, any>) => {
  Object.keys(value).forEach((key) => {
    if (value[key] === undefined || value[key] === '' || value[key] === null) {
      delete value[key]
    }
  })
  return value
}

const mapListResponse = (res: any) => {
  if (Array.isArray(res.data?.data)) {
    res.data.data = res.data.data.map(toCamelGroup)
  }
  return res
}

const mapGroupResponse = (res: any) => {
  if (res.data?.data) {
    res.data.data = toCamelGroup(res.data.data)
  } else if (res.data?.id) {
    res.data = { code: 0, message: 'success', data: toCamelGroup(res.data) }
  }
  return res
}

const mapMemberListResponse = (res: any) => {
  if (Array.isArray(res.data?.data)) {
    res.data.data = res.data.data.map(toCamelMember)
  }
  return res
}

export const groupsApi = {
  list: async (params?: GroupListParams) => {
    const query = clean({
      ...params,
      page_size: params?.pageSize,
      source_keyword: params?.sourceKeyword,
    })
    delete query.pageSize
    delete query.sourceKeyword
    const res = await apiClient.get<{ data: Group[]; total: number }>('/groups', { params: query })
    return mapListResponse(res)
  },

  create: async (data: GroupFormData) => {
    const res = await apiClient.post<{ data: Group }>('/groups', clean(toServerGroup(data)))
    return mapGroupResponse(res)
  },

  getById: async (id: number) => {
    const res = await apiClient.get<{ data: Group }>(`/groups/${id}`)
    return mapGroupResponse(res)
  },

  update: async (id: number, data: Partial<GroupFormData>) => {
    const payload = clean(toServerGroup(data))
    delete payload.group_id
    delete payload.account_id
    delete payload.join_method
    const res = await apiClient.put<{ data: Group }>(`/groups/${id}`, payload)
    return mapGroupResponse(res)
  },

  delete: (id: number) => {
    return apiClient.delete(`/groups/${id}`)
  },

  getMembers: async (id: number, _params?: { page?: number; pageSize?: number }) => {
    const res = await apiClient.get<{ data: GroupMember[]; total: number }>(`/groups/${id}/memberships`)
    return mapMemberListResponse(res)
  },

  createMembership: async (id: number, data: { accountId: number; joinMethod?: string; sourceKeyword?: string; note?: string }) => {
    const res = await apiClient.post<{ data: GroupMember }>(`/groups/${id}/memberships`, clean({
      account_id: data.accountId,
      join_method: data.joinMethod,
      source_keyword: data.sourceKeyword,
      note: data.note,
    }))
    if (res.data?.data) {
      res.data.data = toCamelMember(res.data.data) as any
    } else if ((res.data as any)?.id) {
      res.data = { code: 0, message: 'success', data: toCamelMember(res.data) } as any
    }
    return res
  },

  syncMetrics: async (id: number) => {
    const res = await apiClient.post<{ data: Group }>(`/groups/${id}/sync-metrics`)
    return mapGroupResponse(res)
  },

  getTopGroups: async (params?: { limit?: number; sortBy?: 'memberCount' | 'activity' }) => {
    const res = await apiClient.get<{ data: Group[] }>('/groups/top', { params })
    return mapListResponse(res)
  },
}
