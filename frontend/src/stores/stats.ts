import { defineStore } from 'pinia'
import { ref } from 'vue'
import { statsApi, type DashboardStats, type StatsOverview, type TrendData, type FunnelData, type SourceDistribution } from '@/api/stats'

export const useStatsStore = defineStore('stats', () => {
  const dashboardStats = ref<DashboardStats | null>(null)
  const overview = ref<StatsOverview | null>(null)
  const trendData = ref<TrendData[]>([])
  const funnelData = ref<FunnelData[]>([])
  const sourceData = ref<SourceDistribution[]>([])
  const loading = ref(false)

  const dateRange = ref<[string, string] | null>(null)

  const fetchDashboard = async () => {
    loading.value = true
    try {
      const res = await statsApi.dashboard()
      dashboardStats.value = res.data.data
      return dashboardStats.value
    } finally {
      loading.value = false
    }
  }

  const fetchOverview = async (params?: { startDate?: string; endDate?: string }) => {
    loading.value = true
    try {
      const res = await statsApi.overview(params)
      overview.value = res.data.data
      return overview.value
    } finally {
      loading.value = false
    }
  }

  const fetchTrend = async (params?: { startDate?: string; endDate?: string }) => {
    loading.value = true
    try {
      const res = await statsApi.trend(params)
      trendData.value = res.data.data
      return trendData.value
    } finally {
      loading.value = false
    }
  }

  const fetchFunnel = async (params?: { startDate?: string; endDate?: string }) => {
    loading.value = true
    try {
      const res = await statsApi.funnel(params)
      funnelData.value = res.data.data
      return funnelData.value
    } finally {
      loading.value = false
    }
  }

  const fetchSources = async (params?: { startDate?: string; endDate?: string }) => {
    loading.value = true
    try {
      const res = await statsApi.sources(params)
      sourceData.value = res.data.data
      return sourceData.value
    } finally {
      loading.value = false
    }
  }

  const setDateRange = (range: [string, string] | null) => {
    dateRange.value = range
  }

  return {
    dashboardStats,
    overview,
    trendData,
    funnelData,
    sourceData,
    loading,
    dateRange,
    fetchDashboard,
    fetchOverview,
    fetchTrend,
    fetchFunnel,
    fetchSources,
    setDateRange,
  }
})
