import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { campaignsApi, type Campaign, type CampaignFormData, type CampaignListParams, type CampaignStats } from '@/api/campaigns'

export const useCampaignStore = defineStore('campaign', () => {
  const list = ref<Campaign[]>([])
  const total = ref(0)
  const loading = ref(false)
  const currentCampaign = ref<Campaign | null>(null)
  const currentStats = ref<CampaignStats | null>(null)
  const nextCursor = ref<string | null>(null)

  const page = ref(1)
  const pageSize = ref(20)
  const params = ref<CampaignListParams>({})

  const hasMore = computed(() => Boolean(nextCursor.value))

  const fetchList = async (newParams?: CampaignListParams) => {
    loading.value = true
    try {
      if (newParams) {
        params.value = { ...newParams }
      }
      const effectiveLimit = page.value * pageSize.value
      const payload = await campaignsApi.list({
        limit: effectiveLimit,
        ...params.value,
      })
      const startIndex = Math.max(0, (page.value - 1) * pageSize.value)
      list.value = payload.list.slice(startIndex, startIndex + pageSize.value)
      total.value = payload.total
      nextCursor.value = payload.nextCursor ?? null
      return list.value
    } finally {
      loading.value = false
    }
  }

  const create = async (data: CampaignFormData) => {
    loading.value = true
    try {
      const created = await campaignsApi.create(data)
      await fetchList()
      return created
    } finally {
      loading.value = false
    }
  }

  const update = async (id: number, data: Partial<CampaignFormData>) => {
    loading.value = true
    try {
      const updated = await campaignsApi.update(id, data)
      const index = list.value.findIndex((item) => item.id === id)
      if (index !== -1) {
        list.value[index] = updated
      }
      if (currentCampaign.value?.id === id) {
        currentCampaign.value = updated
      }
      return updated
    } finally {
      loading.value = false
    }
  }

  const remove = async (id: number) => {
    loading.value = true
    try {
      await campaignsApi.delete(id)
      list.value = list.value.filter((item) => item.id !== id)
      total.value = Math.max(0, total.value - 1)
    } finally {
      loading.value = false
    }
  }

  const getById = async (id: number) => {
    const campaign = await campaignsApi.getById(id)
    currentCampaign.value = campaign
    return currentCampaign.value
  }

  const toggle = async (id: number) => {
    const result = await campaignsApi.toggle(id)
    const campaign = list.value.find((item) => item.id === id)
    if (campaign) {
      campaign.enabled = result.enabled
    }
    if (currentCampaign.value?.id === id) {
      currentCampaign.value.enabled = result.enabled
    }
    return result
  }

  const fetchStats = async (id: number) => {
    loading.value = true
    try {
      const stats = await campaignsApi.getStats(id)
      currentStats.value = stats
      return currentStats.value
    } finally {
      loading.value = false
    }
  }

  const trigger = async (id: number) => {
    return campaignsApi.trigger(id)
  }

  const setPage = (newPage: number) => {
    page.value = newPage
  }

  const setPageSize = (newPageSize: number) => {
    pageSize.value = newPageSize
    page.value = 1
  }

  return {
    list,
    total,
    loading,
    currentCampaign,
    currentStats,
    nextCursor,
    page,
    pageSize,
    hasMore,
    fetchList,
    create,
    update,
    remove,
    getById,
    toggle,
    trigger,
    fetchStats,
    setPage,
    setPageSize,
  }
})
