import apiClient from './client'

export interface DashboardStats {
  totalAccounts: number
  onlineAccounts: number
  totalGroups: number
  totalUsers: number
  dailyRegistered: number
  conversionRate: number
  weeklyTrend: Array<{
    date: string
    registered: number
    converted: number
    active: number
  }>
  accountDistribution: Array<{
    status: string
    count: number
  }>
  topGroups: Array<{
    id: number
    title: string
    memberCount: number
  }>
}

export interface TrendData {
  date: string
  registered: number
  converted: number
  active: number
}

export interface FunnelData {
  stage: string
  count: number
  rate: number
}

export interface SourceDistribution {
  source: string
  count: number
  percentage: number
}

export interface GroupStats {
  id: number
  title: string
  memberCount: number
  dailyActive: number
  weeklyGrowth: number
}

export interface KeywordStats {
  keyword: string
  type: string
  hitCount: number
  lastHitAt: string
}

export interface StatsOverview {
  totalUsers: number
  totalGroups: number
  totalCampaigns: number
  totalKeywords: number
  todayRegistered: number
  todayActive: number
  weeklyGrowth: number
  monthlyGrowth: number
}

export interface StatsParams {
  startDate?: string
  endDate?: string
}

const toQueryParams = (params?: StatsParams) => ({
  start_date: params?.startDate,
  end_date: params?.endDate,
})

export const statsApi = {
  dashboard: () => {
    return apiClient.get<{ data: DashboardStats }>('/stats/dashboard')
  },

  overview: (params?: StatsParams) => {
    return apiClient.get<{ data: StatsOverview }>('/stats/overview', { params: toQueryParams(params) })
  },

  trend: (params?: StatsParams) => {
    return apiClient.get<{ data: TrendData[] }>('/stats/trend', { params: toQueryParams(params) })
  },

  funnel: (params?: StatsParams) => {
    return apiClient.get<{ data: FunnelData[] }>('/stats/funnel', { params: toQueryParams(params) })
  },

  sources: (params?: StatsParams) => {
    return apiClient.get<{ data: SourceDistribution[] }>('/stats/sources', { params: toQueryParams(params) })
  },

  groupStats: (params?: { limit?: number; sortBy?: string }) => {
    return apiClient.get<{ data: GroupStats[] }>('/stats/groups', { params })
  },

  keywordStats: (params?: { limit?: number; type?: string }) => {
    return apiClient.get<{ data: KeywordStats[] }>('/stats/keywords', { params })
  },

  export: (params?: StatsParams & { type: string }) => {
    return apiClient.get('/stats/export', { params: { ...toQueryParams(params), type: params?.type }, responseType: 'blob' })
  },
}
