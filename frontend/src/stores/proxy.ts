import { defineStore } from 'pinia'
import { ref } from 'vue'
import {
  proxiesApi,
  type Proxy,
  type ProxyListParams,
  type ProxyFormData,
  type ProxyUpdateData,
} from '@/api/proxies'
import { DEFAULT_PAGE_SIZE, normalizeListPayload, normalizePageSize } from '@/utils/pagination'

export const useProxyStore = defineStore('proxy', () => {
  const list = ref<Proxy[]>([])
  const total = ref(0)
  const loading = ref(false)
  const currentProxy = ref<Proxy | null>(null)

  const page = ref(1)
  const pageSize = ref(DEFAULT_PAGE_SIZE)
  const params = ref<ProxyListParams>({})

  const fetchList = async (newParams?: ProxyListParams) => {
    loading.value = true
    try {
      if (newParams) {
        params.value = newParams
      }
      const res = await proxiesApi.list({
        page: page.value,
        pageSize: pageSize.value,
        ...params.value,
      })
      const payload = normalizeListPayload<Proxy>(res.data)
      list.value = payload.list
      total.value = payload.total
      return list.value
    } finally {
      loading.value = false
    }
  }

  const create = async (data: ProxyFormData) => {
    loading.value = true
    try {
      const res = await proxiesApi.create(data)
      await fetchList()
      return res.data.data
    } finally {
      loading.value = false
    }
  }

  const update = async (id: number, data: ProxyUpdateData) => {
    loading.value = true
    try {
      const res = await proxiesApi.update(id, data)
      const index = list.value.findIndex((item) => item.id === id)
      if (index !== -1) {
        list.value[index] = res.data.data
      }
      return res.data.data
    } finally {
      loading.value = false
    }
  }

  const setEnabled = async (id: number, enabled: boolean) => {
    return update(id, { status: enabled ? 'active' : 'inactive' })
  }

  const remove = async (id: number) => {
    loading.value = true
    try {
      await proxiesApi.delete(id)
      list.value = list.value.filter((item) => item.id !== id)
      total.value--
    } finally {
      loading.value = false
    }
  }

  const getById = async (id: number) => {
    const res = await proxiesApi.getById(id)
    currentProxy.value = res.data.data
    return currentProxy.value
  }

  const testLatency = async (id: number) => {
    try {
      const res = await proxiesApi.test(id)
      const proxy = list.value.find((item) => item.id === id)
      if (proxy) {
        proxy.latency = res.data.data.latency
      }
      return res.data.data
    } finally {
      // Both success and failure update health counters on the backend. Reload
      // so the row immediately reflects active/error while keeping manual stop.
      await fetchList().catch(() => undefined)
    }
  }

  const refreshStatus = async () => {
    loading.value = true
    try {
      await proxiesApi.refreshStatus()
      await fetchList()
    } finally {
      loading.value = false
    }
  }

  const setPage = (newPage: number) => {
    page.value = newPage
  }

  const setPageSize = (newPageSize: number) => {
    pageSize.value = normalizePageSize(newPageSize)
    page.value = 1
  }

  return {
    list,
    total,
    loading,
    currentProxy,
    page,
    pageSize,
    fetchList,
    create,
    update,
    setEnabled,
    remove,
    getById,
    testLatency,
    refreshStatus,
    setPage,
    setPageSize,
  }
})
