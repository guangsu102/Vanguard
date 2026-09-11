<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import type { Account } from '@/api/accounts'
import {
  cloneAccountPersona,
  createNeutralAccountPersona,
  getAccountPersonaError,
  getAccountPersonaErrorMessage,
  getAccountPersonaFieldErrors,
  normalizeAccountPersona,
  validateAccountPersona,
  type AccountPersona,
  type AccountPersonaApiError,
  type AccountPersonaDetail,
  type AccountPersonaFieldErrors,
  type AccountPersonaPreviewInput,
} from '@/api/accountPersonas'
import { useAccountPersonaStore } from '@/stores/accountPersona'
import PersonaForm from './PersonaForm.vue'
import PersonaPreviewPanel from './PersonaPreviewPanel.vue'

const props = withDefaults(
  defineProps<{
    visible: boolean
    account: Account | null
    canEdit?: boolean
  }>(),
  { canEdit: false },
)

const emit = defineEmits<{
  (event: 'update:visible', value: boolean): void
  (event: 'updated', value: AccountPersonaDetail): void
}>()

const personaStore = useAccountPersonaStore()
const draftPersona = ref<AccountPersona>(createNeutralAccountPersona())
const loadedPersona = ref<AccountPersona>(createNeutralAccountPersona())
const loadedRevision = ref(0)
const fieldErrors = ref<AccountPersonaFieldErrors>({})
const loadError = ref<AccountPersonaApiError | null>(null)
const conflictError = ref<AccountPersonaApiError | null>(null)
const previewPanelRef = ref<InstanceType<typeof PersonaPreviewPanel> | null>(null)
let loadGeneration = 0

const detail = computed(() => personaStore.detail)
const accountLabel = computed(
  () => props.account?.display_name || props.account?.identifier || '推广账号',
)
const title = computed(() => `AI 性格 · ${accountLabel.value}`)
const isApplicable = computed(() => detail.value?.persona_applicable === true)
const isConfigInvalid = computed(() => loadError.value?.code === 'PERSONA_CONFIG_INVALID')
const isDirty = computed(
  () =>
    JSON.stringify(normalizeAccountPersona(draftPersona.value)) !==
    JSON.stringify(normalizeAccountPersona(loadedPersona.value)),
)
const canSave = computed(
  () =>
    props.canEdit &&
    isApplicable.value &&
    isDirty.value &&
    !personaStore.loading &&
    !personaStore.saving,
)
const canReset = computed(
  () =>
    props.canEdit &&
    (detail.value?.configured === true || isConfigInvalid.value) &&
    !personaStore.resetting,
)
const canPreview = computed(
  () =>
    props.canEdit &&
    isApplicable.value &&
    (detail.value?.preview_targets.length ?? 0) > 0 &&
    !personaStore.previewing,
)

const clearLocalState = () => {
  draftPersona.value = createNeutralAccountPersona()
  loadedPersona.value = createNeutralAccountPersona()
  loadedRevision.value = 0
  fieldErrors.value = {}
  loadError.value = null
  conflictError.value = null
  previewPanelRef.value?.resetForm()
}

const applyDetail = (value: AccountPersonaDetail) => {
  const persona = value.persona
    ? cloneAccountPersona(value.persona)
    : createNeutralAccountPersona()
  draftPersona.value = persona
  loadedPersona.value = cloneAccountPersona(persona)
  loadedRevision.value = value.revision
  fieldErrors.value = {}
  loadError.value = null
  conflictError.value = null
}

const revisionFromError = (error: AccountPersonaApiError): number | null => {
  const value = error.details.current_revision ?? error.details.revision
  return typeof value === 'number' && Number.isInteger(value) && value >= 0 ? value : null
}

const loadPersona = async (account: Account) => {
  const generation = ++loadGeneration
  clearLocalState()
  personaStore.selectAccount(account.id)
  try {
    const value = await personaStore.fetchPersona(account.id, account.persona?.revision)
    if (generation !== loadGeneration || props.account?.id !== account.id || !value) return
    applyDetail(value)
  } catch (caught) {
    if (generation !== loadGeneration || props.account?.id !== account.id) return
    const error = getAccountPersonaError(caught)
    loadError.value = error
    const currentRevision = revisionFromError(error)
    if (currentRevision !== null) loadedRevision.value = currentRevision
  }
}

watch(
  [() => props.visible, () => props.account?.id, () => props.canEdit],
  ([visible, accountId, canEdit], previous) => {
    const previousAccountId = previous?.[1]
    if (!visible || accountId === undefined || !props.account || !canEdit) {
      loadGeneration += 1
      personaStore.closeAccount()
      clearLocalState()
      return
    }
    if (accountId !== previousAccountId || !previous?.[0] || !previous?.[2]) {
      void loadPersona(props.account)
    }
  },
  { immediate: true },
)

const closeDrawer = () => {
  loadGeneration += 1
  personaStore.closeAccount()
  clearLocalState()
  emit('update:visible', false)
}

const validateDraft = (): AccountPersona | null => {
  const normalized = normalizeAccountPersona(draftPersona.value)
  fieldErrors.value = validateAccountPersona(
    normalized,
    detail.value?.limits.max_total_bytes,
    detail.value?.limits.max_system_prompt_chars,
  )
  return Object.keys(fieldErrors.value).length === 0 ? normalized : null
}

const copyDraft = async () => {
  try {
    if (!navigator.clipboard?.writeText) throw new Error('clipboard_unavailable')
    await navigator.clipboard.writeText(JSON.stringify(draftPersona.value, null, 2))
    ElMessage.success('本地草稿已复制')
  } catch {
    ElMessage.error('复制失败，请保持抽屉开启并手动复制')
  }
}

const reloadLatest = async () => {
  const account = props.account
  if (!account) return
  personaStore.invalidateAccount(account.id)
  const value = await personaStore.fetchPersona(account.id)
  if (value && props.account?.id === account.id) applyDetail(value)
}

const handleConflict = async (error: AccountPersonaApiError) => {
  conflictError.value = error
  try {
    await ElMessageBox.confirm(
      '服务端 Persona 已更新。本地草稿仍保留，请选择重新加载或复制草稿。',
      'Persona 版本冲突',
      {
        type: 'warning',
        confirmButtonText: '重新加载',
        cancelButtonText: '复制草稿',
        distinguishCancelAndClose: true,
        closeOnClickModal: false,
      },
    )
    await reloadLatest()
  } catch (action) {
    if (action === 'cancel') await copyDraft()
  }
}

const savePersona = async () => {
  const account = props.account
  if (!account || !canSave.value) return
  const normalized = validateDraft()
  if (!normalized) return
  try {
    const value = await personaStore.replacePersona(account.id, loadedRevision.value, normalized)
    if (props.account?.id !== account.id) return
    applyDetail(value)
    emit('updated', value)
    ElMessage.success('Persona 已保存')
  } catch (caught) {
    const error = getAccountPersonaError(caught)
    if (error.code === 'PERSONA_REVISION_CONFLICT') {
      await handleConflict(error)
      return
    }
    fieldErrors.value = getAccountPersonaFieldErrors(error)
    ElMessage.error(getAccountPersonaErrorMessage(error))
  }
}

const resetPersona = async () => {
  const account = props.account
  if (!account || !canReset.value) return
  try {
    await ElMessageBox.confirm(
      '恢复中性默认只影响之后创建的 AI 任务，已经创建的任务不会改变。',
      '恢复中性默认',
      {
        type: 'warning',
        confirmButtonText: '确认恢复',
        cancelButtonText: '取消',
      },
    )
  } catch {
    return
  }
  try {
    const value = await personaStore.resetPersona(account.id, loadedRevision.value)
    if (props.account?.id !== account.id) return
    applyDetail(value)
    emit('updated', value)
    ElMessage.success('已恢复中性默认')
  } catch (caught) {
    const error = getAccountPersonaError(caught)
    if (error.code === 'PERSONA_REVISION_CONFLICT') {
      await handleConflict(error)
      return
    }
    ElMessage.error(getAccountPersonaErrorMessage(error))
  }
}

const createRequestId = (): string => {
  if (typeof globalThis.crypto?.randomUUID === 'function') return globalThis.crypto.randomUUID()
  return `persona-preview-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

const previewPersona = async (input: AccountPersonaPreviewInput) => {
  const account = props.account
  if (!account || !canPreview.value) return
  const normalized = validateDraft()
  if (!normalized) return
  try {
    await personaStore.previewPersona(
      account.id,
      {
        ...input,
        ...(isDirty.value ? { draft_persona: normalized } : {}),
      },
      createRequestId(),
    )
  } catch (caught) {
    const error = getAccountPersonaError(caught, 'Persona 预览失败')
    ElMessage.error(getAccountPersonaErrorMessage(error))
  }
}

defineExpose({
  canPreview,
  canSave,
  clearLocalState,
  conflictError,
  draftPersona,
  fieldErrors,
  isDirty,
  loadPersona,
  loadedRevision,
  previewPersona,
  reloadLatest,
  resetPersona,
  savePersona,
})
</script>

<template>
  <el-drawer
    :model-value="visible"
    :title="title"
    size="720px"
    destroy-on-close
    :close-on-click-modal="false"
    class="account-persona-drawer"
    @update:model-value="$event ? undefined : closeDrawer()"
    @closed="clearLocalState"
  >
    <div v-if="account" v-loading="personaStore.loading" class="drawer-body">
      <el-descriptions :column="2" border size="small">
        <el-descriptions-item label="账号">{{ accountLabel }}</el-descriptions-item>
        <el-descriptions-item label="Account ID">{{ account.id }}</el-descriptions-item>
        <el-descriptions-item label="类型">{{ detail?.account_type || account.account_type }}</el-descriptions-item>
        <el-descriptions-item label="账号职责">
          {{ (detail?.operation_mode || account.operation_mode) === 'growth' ? 'Growth 增长' : 'Ad-only 专用' }}
        </el-descriptions-item>
        <el-descriptions-item label="配置版本">v{{ detail?.revision ?? loadedRevision }}</el-descriptions-item>
        <el-descriptions-item label="运行时应用">
          <el-tag :type="detail?.feature.runtime_will_apply ? 'success' : 'warning'" effect="plain">
            {{ detail?.feature.runtime_will_apply ? '会应用' : '已暂停应用' }}
          </el-tag>
        </el-descriptions-item>
      </el-descriptions>

      <el-alert
        v-if="!canEdit"
        title="完整 Persona 仅管理员可操作"
        type="info"
        :closable="false"
        show-icon
      />
      <el-alert
        v-else-if="detail && !detail.persona_applicable"
        :title="`当前账号不适用 Persona：${detail.blocking_reason || '账号类型或职责不支持'}`"
        type="warning"
        :closable="false"
        show-icon
      />
      <el-alert
        v-if="detail && !detail.feature.runtime_will_apply"
        title="Persona 功能当前关闭：配置仍可保存，预览结果不会用于实际发送。"
        type="warning"
        :closable="false"
        show-icon
      />
      <el-alert
        v-if="conflictError"
        title="检测到版本冲突，本地草稿仍保留且尚未覆盖服务端。"
        type="warning"
        :closable="false"
        show-icon
      />
      <el-alert
        v-if="loadError"
        :title="getAccountPersonaErrorMessage(loadError)"
        :type="isConfigInvalid ? 'error' : 'warning'"
        :closable="false"
        show-icon
      />

      <template v-if="detail && !isConfigInvalid">
        <PersonaForm
          v-model="draftPersona"
          :disabled="!canEdit || !detail.persona_applicable"
          :field-errors="fieldErrors"
          :max-system-prompt-chars="detail.limits.max_system_prompt_chars"
        />
        <PersonaPreviewPanel
          ref="previewPanelRef"
          :targets="detail.preview_targets"
          :result="personaStore.previewResult"
          :loading="personaStore.previewing"
          :disabled="!canEdit || !detail.persona_applicable"
          :runtime-will-apply="detail.feature.runtime_will_apply"
          @preview="previewPersona"
        />
      </template>
    </div>

    <template #footer>
      <div class="drawer-footer">
        <el-button @click="closeDrawer">取消</el-button>
        <div class="primary-actions">
          <el-button
            v-if="canReset"
            type="danger"
            plain
            :loading="personaStore.resetting"
            @click="resetPersona"
          >恢复中性默认</el-button>
          <el-button
            v-if="canEdit"
            :disabled="!canPreview"
            :loading="personaStore.previewing"
            @click="previewPanelRef?.requestPreview()"
          >预览</el-button>
          <el-button
            v-if="canEdit && !isConfigInvalid"
            type="primary"
            :disabled="!canSave"
            :loading="personaStore.saving"
            @click="savePersona"
          >保存</el-button>
        </div>
      </div>
    </template>
  </el-drawer>
</template>

<style scoped>
.drawer-body {
  display: flex;
  min-height: 240px;
  flex-direction: column;
  gap: 16px;
}

.drawer-footer,
.primary-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.primary-actions {
  justify-content: flex-end;
}
</style>
