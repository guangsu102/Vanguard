import { defineStore } from 'pinia'
import { ref } from 'vue'
import { usersApi, type User, type UserListParams, type UserActivity } from '@/api/users'
import { normalizeListPayload } from '@/utils/pagination'

export const useUserStore = defineStore('user', () => {
  const list = ref<User[]>([])
  const total = ref(0)
  const loading = ref(false)
  const currentUser = ref<User | null>(null)
  const activities = ref<UserActivity[]>([])
  const activityTotal = ref(0)

  const page = ref(1)
  const pageSize = ref(20)
  const params = ref<UserListParams>({})

  const fetchList = async (newParams?: UserListParams) => {
    loading.value = true
    try {
      if (newParams) {
        params.value = newParams
      }
      const res = await usersApi.list({
        page: page.value,
        pageSize: pageSize.value,
        ...params.value,
      })
      const payload = normalizeListPayload<User>(res.data)
      list.value = payload.list
      total.value = payload.total
      return list.value
    } finally {
      loading.value = false
    }
  }

  const getById = async (id: number) => {
    const res = await usersApi.getById(id)
    currentUser.value = res.data.data
    return currentUser.value
  }

  const update = async (id: number, data: { displayName?: string; status?: string }) => {
    loading.value = true
    try {
      const res = await usersApi.update(id, data as any)
      const index = list.value.findIndex((item) => item.id === id)
      if (index !== -1) {
        list.value[index] = res.data.data
      }
      return res.data.data
    } finally {
      loading.value = false
    }
  }

  const mute = async (id: number, data?: { duration?: number; reason?: string }) => {
    await usersApi.mute(id, data)
    const user = list.value.find((item) => item.id === id)
    if (user) {
      user.status = 'muted'
    }
  }

  const unmute = async (id: number) => {
    await usersApi.unmute(id)
    const user = list.value.find((item) => item.id === id)
    if (user) {
      user.status = 'active'
    }
  }

  const blacklist = async (id: number, data?: { reason?: string }) => {
    await usersApi.blacklist(id, data)
    const user = list.value.find((item) => item.id === id)
    if (user) {
      user.status = 'banned'
    }
  }

  const removeBlacklist = async (id: number) => {
    await usersApi.removeBlacklist(id)
    const user = list.value.find((item) => item.id === id)
    if (user) {
      user.status = 'active'
    }
  }

  const fetchActivities = async (id: number, params?: { page?: number; pageSize?: number }) => {
    loading.value = true
    try {
      const res = await usersApi.getActivities(id, params)
      const payload = normalizeListPayload<UserActivity>(res.data)
      activities.value = payload.list
      activityTotal.value = payload.total
      return activities.value
    } finally {
      loading.value = false
    }
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
    currentUser,
    activities,
    activityTotal,
    page,
    pageSize,
    fetchList,
    getById,
    update,
    mute,
    unmute,
    blacklist,
    removeBlacklist,
    fetchActivities,
    setPage,
    setPageSize,
  }
})
