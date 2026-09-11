import { defineStore } from 'pinia'
import { ref } from 'vue'
import {
  getSettingsApiError,
  settingsApi,
  type OwnedGroupAiPersonaFeatureSettings,
  type SettingsReadData,
  type SettingsUpdatePayload,
  type SystemInfo,
  type OperationLog,
} from '@/api/settings'
import { DEFAULT_PAGE_SIZE, normalizePageSize } from '@/utils/pagination'

export const useSettingsStore = defineStore('settings', () => {
  const settings = ref<SettingsReadData | null>(null)
  const systemInfo = ref<SystemInfo | null>(null)
  const logs = ref<OperationLog[]>([])
  const logTotal = ref(0)
  const loading = ref(false)
  const ownedGroupAiPersonaPendingIntent = ref<boolean | null>(null)

  const page = ref(1)
  const pageSize = ref(DEFAULT_PAGE_SIZE)

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

  const updateSettings = async (data: SettingsUpdatePayload) => {
    loading.value = true
    try {
      const payload: SettingsUpdatePayload = {}
      if (data.notification !== undefined) payload.notification = data.notification
      if (data.xboard !== undefined) payload.xboard = data.xboard
      if (data.aiReply !== undefined) payload.aiReply = data.aiReply
      if (data.groupAiInteraction !== undefined) {
        payload.groupAiInteraction = data.groupAiInteraction
      }
      if (data.ownedGroupMessaging !== undefined) {
        payload.ownedGroupMessaging = data.ownedGroupMessaging
      }
      if (data.keywordPrivateReply !== undefined) {
        payload.keywordPrivateReply = data.keywordPrivateReply
      }
      if (data.privateMessaging !== undefined) payload.privateMessaging = data.privateMessaging
      await settingsApi.update(payload)
      await fetchSettings()
    } finally {
      loading.value = false
    }
  }

  const updateOwnedGroupAiPersonaFeature = async (
    enabled: boolean,
  ): Promise<OwnedGroupAiPersonaFeatureSettings> => {
    const current = settings.value?.ownedGroupAiPersona
    if (!current) throw new Error('Persona feature settings have not been loaded')

    ownedGroupAiPersonaPendingIntent.value = enabled
    loading.value = true
    try {
      const response = await settingsApi.updateOwnedGroupAiPersonaFeature({
        expectedRevision: current.revision,
        enabled,
      })
      const saved = response.data.data
      if (settings.value) {
        settings.value = {
          ...settings.value,
          ownedGroupAiPersona: saved,
        }
      }
      ownedGroupAiPersonaPendingIntent.value = null
      return saved
    } catch (error) {
      if (getSettingsApiError(error).code === 'PERSONA_FEATURE_REVISION_CONFLICT') {
        try {
          await fetchSettings()
        } catch {
          // Preserve the original conflict and the user's intended value.
        }
      }
      throw error
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
    pageSize.value = normalizePageSize(newPageSize)
    page.value = 1
  }

  return {
    settings,
    systemInfo,
    logs,
    logTotal,
    loading,
    ownedGroupAiPersonaPendingIntent,
    page,
    pageSize,
    fetchSettings,
    updateSettings,
    updateOwnedGroupAiPersonaFeature,
    fetchSystemInfo,
    fetchLogs,
    clearLogs,
    backupDatabase,
    setPage,
    setPageSize,
  }
})
