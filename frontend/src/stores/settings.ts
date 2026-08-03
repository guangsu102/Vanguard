import { defineStore } from 'pinia'
import { ref } from 'vue'
import {
  settingsApi,
  type SettingsFormData,
  type SystemInfo,
  type OperationLog,
} from '@/api/settings'

export interface AllSettings extends SettingsFormData {
  system?: SystemInfo
}

export const useSettingsStore = defineStore('settings', () => {
  const settings = ref<AllSettings>({})
  const systemInfo = ref<SystemInfo | null>(null)
  const logs = ref<OperationLog[]>([])
  const logTotal = ref(0)
  const loading = ref(false)

  const page = ref(1)
  const pageSize = ref(20)

  const fetchSettings = async () => {
    loading.value = true
    try {
      const res = await settingsApi.get()
      settings.value = res.data.data
      return settings.value
    } finally {
      loading.value = false
    }
  }

  const updateSettings = async (data: SettingsFormData) => {
    loading.value = true
    try {
      await settingsApi.update(data)
      await fetchSettings()
    } finally {
      loading.value = false
    }
  }

  const fetchSystemInfo = async () => {
    const res = await settingsApi.getSystemInfo()
    systemInfo.value = res.data.data
    return systemInfo.value
  }

  const fetchLogs = async (params?: {
    page?: number
    pageSize?: number
    user?: string
    action?: string
    startDate?: string
    endDate?: string
  }) => {
    loading.value = true
    try {
      const res = await settingsApi.getLogs({ page: page.value, pageSize: pageSize.value, ...params })
      logs.value = res.data.data.list
      logTotal.value = res.data.data.total
      return logs.value
    } finally {
      loading.value = false
    }
  }

  const clearLogs = async () => {
    loading.value = true
    try {
      await settingsApi.clearLogs()
      logs.value = []
      logTotal.value = 0
    } finally {
      loading.value = false
    }
  }

  const backupDatabase = async () => {
    loading.value = true
    try {
      const res = await settingsApi.backupDatabase()
      return res.data.data
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
    settings,
    systemInfo,
    logs,
    logTotal,
    loading,
    page,
    pageSize,
    fetchSettings,
    updateSettings,
    fetchSystemInfo,
    fetchLogs,
    clearLogs,
    backupDatabase,
    setPage,
    setPageSize,
  }
})
