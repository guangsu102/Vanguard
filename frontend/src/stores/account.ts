import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import {
  accountsApi,
  type Account,
  type AccountFormData,
  type AccountListParams,
  type AccountType,
  type AccountUpdateData,
} from '@/api/accounts'

export const useAccountStore = defineStore('account', () => {
  const list = ref<Account[]>([])
  const total = ref(0)
  const loading = ref(false)
  const currentAccount = ref<Account | null>(null)
  const nextCursor = ref<string | null>(null)

  const page = ref(1)
  const pageSize = ref(20)
  const params = ref<AccountListParams>({})

  const hasMore = computed(() => Boolean(nextCursor.value))

  const fetchList = async (newParams?: AccountListParams) => {
    loading.value = true
    try {
      if (newParams) {
        params.value = { ...newParams }
      }
      const effectiveLimit = page.value * pageSize.value
      const payload = await accountsApi.list({
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

  const create = async (data: AccountFormData) => {
    loading.value = true
    try {
      const created = await accountsApi.create(data)
      await fetchList()
      return created
    } finally {
      loading.value = false
    }
  }

  const update = async (id: number, data: AccountUpdateData) => {
    loading.value = true
    try {
      const updated = await accountsApi.update(id, data)
      const index = list.value.findIndex((item) => item.id === id)
      if (index !== -1) {
        list.value[index] = updated
      }
      if (currentAccount.value?.id === id) {
        currentAccount.value = updated
      }
      return updated
    } finally {
      loading.value = false
    }
  }

  const remove = async (id: number) => {
    loading.value = true
    try {
      await accountsApi.delete(id)
      list.value = list.value.filter((item) => item.id !== id)
      total.value = Math.max(0, total.value - 1)
    } finally {
      loading.value = false
    }
  }

  const getById = async (id: number) => {
    const account = await accountsApi.getById(id)
    currentAccount.value = account
    return currentAccount.value
  }

  const enable = async (id: number) => {
    await accountsApi.connect(id)
    const account = list.value.find((item) => item.id === id)
    if (account) {
      account.status = 'online'
      account.is_active = true
    }
  }

  const disable = async (id: number) => {
    await accountsApi.disconnect(id)
    const account = list.value.find((item) => item.id === id)
    if (account) {
      account.status = 'offline'
      account.is_active = false
    }
  }

  const syncProfileBio = async (id: number, profileBio?: string) => {
    const updated = await accountsApi.syncProfileBio(id, profileBio)
    const index = list.value.findIndex((item) => item.id === id)
    if (index !== -1) {
      list.value[index] = updated
    }
    if (currentAccount.value?.id === id) {
      currentAccount.value = updated
    }
    return updated
  }

  const updateProxyPolicy = async (
    id: number,
    data: { proxy_mode: 'dynamic' | 'static' | 'none'; static_proxy_id?: number },
  ) => {
    const result = await accountsApi.updateProxyPolicy(id, data)
    const account = await getById(id)
    const index = list.value.findIndex((item) => item.id === id)
    if (index !== -1 && account) {
      list.value[index] = account
    }
    return result
  }

  const setPage = (newPage: number) => {
    page.value = newPage
  }

  const setPageSize = (newPageSize: number) => {
    pageSize.value = newPageSize
    page.value = 1
  }

  const setAccountTypeFilter = (accountType: AccountType) => {
    params.value = {
      ...params.value,
      account_type: accountType,
    }
  }

  return {
    list,
    total,
    loading,
    currentAccount,
    nextCursor,
    page,
    pageSize,
    hasMore,
    fetchList,
    create,
    update,
    remove,
    getById,
    enable,
    disable,
    syncProfileBio,
    updateProxyPolicy,
    setPage,
    setPageSize,
    setAccountTypeFilter,
  }
})
