import apiClient from './client'

export interface DashboardStats {
  totalAccounts: number
  onlineAccounts: number
  totalGroups: number
  totalUsers: number
  activeUsers: number
  dailyRegistered: number
  dailyConverted: number
  dailyMessages: number
  dailyViolations: number
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

export interface P4PacingAccount {
  account_id: number
  gate_reason: string | null
  score: number
  budget_headroom: number
  read_recovery_factor: number
  risk_component: number
  cooldown_readiness: number
  survival_health: number
  recent_success_rate: number
  fair_share: number
  actual_share: number
  fairness_gap: number
  last_scheduled_at: number
  rank: number
  risk_level: string
}

export interface P4Summary {
  flags: {
    ad_pacing_enabled: boolean
    ad_pacing_shadow: boolean
    ad_pacing_active: boolean
    admin_metrics_enabled: boolean
    history_governance_enabled: boolean
    history_governance_dry_run: boolean
    canary_account_ids: number[]
    survival_backlog_limit: number
    unresolved_backlog_limit: number
    retention_days: { hot: number; audit: number; recent: number }
  }
  generated_at: string
  pacing: {
    last_decision: {
      id: number
      stage: string
      mode: string
      applied: boolean
      window_started_at: string
      candidate_count: number
      gated_count: number
      scheduled_count: number
      selected_account_id: number | null
      gate_reasons: Record<string, number>
    } | null
    accounts: P4PacingAccount[]
    daily_stats: Array<{
      stat_date: string
      account_id: number
      decision_count: number
      scheduled_count: number
      gated_count: number
      avg_score: number
      fair_share_avg: number
      actual_share_avg: number
      ad_success_count: number
    }>
  }
  ads_queue: Record<string, unknown>
  outbox: {
    states: Record<string, number>
    pending: number
    oldest_age_seconds: number
  }
  survival: { backlog: number; check_due: number; unresolved_sends: number }
  reconciliation: {
    states: Array<{ kind: string; state: string; count: number }>
    oldest_age_seconds: Record<string, number>
    overdue: number
  }
  receipts: { latency_p95_seconds: number | null }
  history: {
    candidates: Record<string, number>
    latest_batches: Array<{
      id: number
      mode: string
      status: string
      dry_run: boolean
      archived_count: number
      deleted_count: number
      started_at: string | null
      finished_at: string | null
      error: string | null
    }>
    archive_count: number
    oldest_archive_age_seconds: number
  }
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

  p4Summary: () => {
    return apiClient.get<{ data: P4Summary }>('/stats/p4/summary')
  },

  export: (params?: StatsParams & { type: string }) => {
    return apiClient.get('/stats/export', { params: { ...toQueryParams(params), type: params?.type }, responseType: 'blob' })
  },
}
