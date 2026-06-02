import { defineStore } from 'pinia'
import { ref } from 'vue'
import { groupsApi, type Group, type GroupListParams, type GroupFormData, type GroupMember } from '@/api/groups'
import { normalizeListPayload } from '@/utils/pagination'

export const useGroupStore = defineStore('group', () => {
  const list = ref<Group[]>([])
  const total = ref(0)
  const loading = ref(false)
  const currentGroup = ref<Group | null>(null)
  const members = ref<GroupMember[]>([])
  const memberTotal = ref(0)

  const page = ref(1)
  const pageSize = ref(20)
  const params = ref<GroupListParams>({})

  const fetchList = async (newParams?: GroupListParams) => {
    loading.value = true
    try {
      if (newParams) {
        params.value = newParams
      }
      const res = await groupsApi.list({
        page: page.value,
        pageSize: pageSize.value,
        ...params.value,
      })
      const payload = normalizeListPayload<Group>(res.data)
      list.value = payload.list
      total.value = payload.total
      return list.value
    } finally {
      loading.value = false
    }
  }

  const create = async (data: GroupFormData) => {
    loading.value = true
    try {
      const res = await groupsApi.create(data)
      await fetchList()
      return res.data.data
    } finally {
      loading.value = false
    }
  }

  const update = async (id: number, data: Partial<GroupFormData>) => {
    loading.value = true
    try {
      const res = await groupsApi.update(id, data)
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
      await groupsApi.delete(id)
      list.value = list.value.filter((item) => item.id !== id)
      total.value--
    } finally {
      loading.value = false
    }
  }

  const getById = async (id: number) => {
    const res = await groupsApi.getById(id)
    currentGroup.value = res.data.data
    return currentGroup.value
  }

  const fetchMembers = async (id: number, params?: { page?: number; pageSize?: number }) => {
    loading.value = true
    try {
      const res = await groupsApi.getMembers(id, params)
      const payload = normalizeListPayload<GroupMember>(res.data)
      members.value = payload.list
      memberTotal.value = payload.total
      return members.value
    } finally {
      loading.value = false
    }
  }

  const syncMetrics = async (id: number) => {
    const res = await groupsApi.syncMetrics(id)
    const group = list.value.find((item) => item.id === id)
    if (group) {
      Object.assign(group, res.data.data)
    }
    return res.data.data
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
    currentGroup,
    members,
    memberTotal,
    page,
    pageSize,
    fetchList,
    create,
    update,
    remove,
    getById,
    fetchMembers,
    syncMetrics,
    setPage,
    setPageSize,
  }
})
