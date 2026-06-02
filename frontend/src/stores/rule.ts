import { defineStore } from 'pinia'
import { ref } from 'vue'
import { rulesApi, type Rule, type RuleListParams, type RuleFormData } from '@/api/rules'
import { normalizeListPayload } from '@/utils/pagination'

export const useRuleStore = defineStore('rule', () => {
  const list = ref<Rule[]>([])
  const total = ref(0)
  const loading = ref(false)
  const currentRule = ref<Rule | null>(null)

  const page = ref(1)
  const pageSize = ref(20)
  const params = ref<RuleListParams>({})

  const fetchList = async (newParams?: RuleListParams) => {
    loading.value = true
    try {
      if (newParams) {
        params.value = newParams
      }
      const res = await rulesApi.list({
        page: page.value,
        pageSize: pageSize.value,
        ...params.value,
      })
      const payload = normalizeListPayload<Rule>(res.data)
      list.value = payload.list
      total.value = payload.total
      return list.value
    } finally {
      loading.value = false
    }
  }

  const create = async (data: RuleFormData) => {
    loading.value = true
    try {
      const res = await rulesApi.create(data)
      await fetchList()
      return res.data.data
    } finally {
      loading.value = false
    }
  }

  const update = async (id: number, data: Partial<RuleFormData>) => {
    loading.value = true
    try {
      const res = await rulesApi.update(id, data)
      const index = list.value.findIndex((item) => item.id === id)
      if (index !== -1) {
        list.value[index] = res.data.data
      }
      return res.data.data
    } finally {
      loading.value = false
    }
  }

  const remove = async (id: number) => {
    loading.value = true
    try {
      await rulesApi.delete(id)
      list.value = list.value.filter((item) => item.id !== id)
      total.value--
    } finally {
      loading.value = false
    }
  }

  const getById = async (id: number) => {
    const res = await rulesApi.getById(id)
    currentRule.value = res.data.data
    return currentRule.value
  }

  const enable = async (id: number) => {
    await rulesApi.enable(id)
    const rule = list.value.find((item) => item.id === id)
    if (rule) {
      rule.status = 'active'
    }
  }

  const disable = async (id: number) => {
    await rulesApi.disable(id)
    const rule = list.value.find((item) => item.id === id)
    if (rule) {
      rule.status = 'inactive'
    }
  }

  const test = async (id: number, data: { message?: string; userId?: number }) => {
    const res = await rulesApi.test(id, data)
    return res.data.data
  }

  const updatePriority = async (rules: Array<{ id: number; priority: number }>) => {
    await rulesApi.updatePriority({ rules })
    await fetchList()
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
    currentRule,
    page,
    pageSize,
    fetchList,
    create,
    update,
    remove,
    getById,
    enable,
    disable,
    test,
    updatePriority,
    setPage,
    setPageSize,
  }
})
