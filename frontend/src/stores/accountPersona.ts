import { defineStore } from 'pinia'
import { ref } from 'vue'

import {
  accountPersonaCacheKey,
  accountPersonasApi,
  getAccountPersonaError,
  type AccountPersona,
  type AccountPersonaApiError,
  type AccountPersonaAuditPage,
  type AccountPersonaAuditParams,
  type AccountPersonaDetail,
  type AccountPersonaPreviewInput,
  type AccountPersonaPreviewResult,
} from '@/api/accountPersonas'

export const useAccountPersonaStore = defineStore('accountPersona', () => {
  const activeAccountId = ref<number | null>(null)
  const detail = ref<AccountPersonaDetail | null>(null)
  const previewResult = ref<AccountPersonaPreviewResult | null>(null)
  const auditPage = ref<AccountPersonaAuditPage | null>(null)
  const error = ref<AccountPersonaApiError | null>(null)
  const previewError = ref<AccountPersonaApiError | null>(null)
  const loading = ref(false)
  const saving = ref(false)
  const resetting = ref(false)
  const previewing = ref(false)
  const auditLoading = ref(false)
  const detailCache = new Map<string, AccountPersonaDetail>()
  let requestGeneration = 0

  const ownsRequest = (accountId: number, generation: number) =>
    activeAccountId.value === accountId && requestGeneration === generation

  const clearActiveState = () => {
    detail.value = null
    previewResult.value = null
    auditPage.value = null
    error.value = null
    previewError.value = null
    loading.value = false
    saving.value = false
    resetting.value = false
    previewing.value = false
    auditLoading.value = false
  }

  const invalidateAccount = (accountId: number) => {
    const prefix = `${accountId}:`
    for (const key of [...detailCache.keys()]) {
      if (key.startsWith(prefix)) detailCache.delete(key)
    }
  }

  const selectAccount = (accountId: number) => {
    if (activeAccountId.value !== accountId) {
      requestGeneration += 1
      detailCache.clear()
      activeAccountId.value = accountId
      clearActiveState()
    }
    return requestGeneration
  }

  const closeAccount = () => {
    requestGeneration += 1
    activeAccountId.value = null
    detailCache.clear()
    clearActiveState()
  }

  const cacheDetail = (value: AccountPersonaDetail) => {
    invalidateAccount(value.account_id)
    detailCache.set(accountPersonaCacheKey(value.account_id, value.revision), value)
  }

  const applyDetail = (value: AccountPersonaDetail) => {
    cacheDetail(value)
    detail.value = value
    previewResult.value = null
    error.value = null
  }

  const fetchPersona = async (
    accountId: number,
    revisionHint?: number,
  ): Promise<AccountPersonaDetail | null> => {
    const generation = selectAccount(accountId)
    if (revisionHint !== undefined) {
      const cached = detailCache.get(accountPersonaCacheKey(accountId, revisionHint))
      if (cached) {
        detail.value = cached
        return cached
      }
      invalidateAccount(accountId)
      if (detail.value?.account_id === accountId && detail.value.revision !== revisionHint) {
        detail.value = null
        previewResult.value = null
        auditPage.value = null
      }
    }
    loading.value = true
    error.value = null
    try {
      const value = await accountPersonasApi.get(accountId)
      if (!ownsRequest(accountId, generation)) return null
      applyDetail(value)
      return value
    } catch (caught) {
      if (!ownsRequest(accountId, generation)) return null
      error.value = getAccountPersonaError(caught)
      throw caught
    } finally {
      if (ownsRequest(accountId, generation)) loading.value = false
    }
  }

  const replacePersona = async (
    accountId: number,
    expectedRevision: number,
    persona: AccountPersona,
  ): Promise<AccountPersonaDetail> => {
    const generation = selectAccount(accountId)
    saving.value = true
    error.value = null
    try {
      const value = await accountPersonasApi.replace(accountId, {
        expected_revision: expectedRevision,
        persona,
      })
      if (ownsRequest(accountId, generation)) applyDetail(value)
      return value
    } catch (caught) {
      if (ownsRequest(accountId, generation)) error.value = getAccountPersonaError(caught)
      throw caught
    } finally {
      if (ownsRequest(accountId, generation)) saving.value = false
    }
  }

  const resetPersona = async (
    accountId: number,
    expectedRevision: number,
  ): Promise<AccountPersonaDetail> => {
    const generation = selectAccount(accountId)
    resetting.value = true
    error.value = null
    try {
      const value = await accountPersonasApi.reset(accountId, {
        expected_revision: expectedRevision,
      })
      if (ownsRequest(accountId, generation)) applyDetail(value)
      return value
    } catch (caught) {
      if (ownsRequest(accountId, generation)) error.value = getAccountPersonaError(caught)
      throw caught
    } finally {
      if (ownsRequest(accountId, generation)) resetting.value = false
    }
  }

  const previewPersona = async (
    accountId: number,
    input: AccountPersonaPreviewInput,
    requestId: string,
  ): Promise<AccountPersonaPreviewResult> => {
    const generation = selectAccount(accountId)
    previewing.value = true
    previewResult.value = null
    previewError.value = null
    try {
      const value = await accountPersonasApi.preview(accountId, input, requestId)
      if (ownsRequest(accountId, generation)) previewResult.value = value
      return value
    } catch (caught) {
      if (ownsRequest(accountId, generation)) {
        previewResult.value = null
        previewError.value = getAccountPersonaError(caught, 'Persona 预览失败')
      }
      throw caught
    } finally {
      if (ownsRequest(accountId, generation)) previewing.value = false
    }
  }

  const fetchAuditEvents = async (
    accountId: number,
    params: AccountPersonaAuditParams = {},
  ): Promise<AccountPersonaAuditPage | null> => {
    const generation = selectAccount(accountId)
    auditLoading.value = true
    try {
      const value = await accountPersonasApi.listAuditEvents(accountId, params)
      if (!ownsRequest(accountId, generation)) return null
      auditPage.value = value
      return value
    } finally {
      if (ownsRequest(accountId, generation)) auditLoading.value = false
    }
  }

  const hasCachedRevision = (accountId: number, revision: number) =>
    detailCache.has(accountPersonaCacheKey(accountId, revision))

  return {
    activeAccountId,
    detail,
    previewResult,
    auditPage,
    error,
    previewError,
    loading,
    saving,
    resetting,
    previewing,
    auditLoading,
    selectAccount,
    closeAccount,
    invalidateAccount,
    fetchPersona,
    replacePersona,
    resetPersona,
    previewPersona,
    fetchAuditEvents,
    hasCachedRevision,
  }
})
