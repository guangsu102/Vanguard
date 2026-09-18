import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

const api = vi.hoisted(() => ({
  get: vi.fn(),
  replace: vi.fn(),
  reset: vi.fn(),
  preview: vi.fn(),
  listAuditEvents: vi.fn(),
}))
const ui = vi.hoisted(() => ({
  confirm: vi.fn(),
  success: vi.fn(),
  error: vi.fn(),
}))

vi.mock('@/api/accountPersonas', async (importOriginal) => {
  const original = await importOriginal<typeof import('@/api/accountPersonas')>()
  return { ...original, accountPersonasApi: api }
})
vi.mock('element-plus', () => ({
  ElMessage: { success: ui.success, error: ui.error },
  ElMessageBox: { confirm: ui.confirm },
}))

import type { Account } from '@/api/accounts'
import type {
  AccountPersona,
  AccountPersonaDetail,
  AccountPersonaFieldErrors,
  AccountPersonaPreviewInput,
} from '@/api/accountPersonas'
import { useAccountPersonaStore } from '@/stores/accountPersona'
import AccountPersonaDrawer from './AccountPersonaDrawer.vue'

const persona = (name: string): AccountPersona => ({
  schema_version: 1,
  name,
  tone: '自然、克制、简短',
  interests: [],
  expertise: [],
  reply_length: 'short',
  preferred_topics: [],
  forbidden_topics: [],
  ad_style: 'neutral',
  catchphrases: [],
  language_style: 'zh_cn',
  system_prompt: '',
})

const account = (id: number): Account => ({
  id,
  identifier: `account-${id}`,
  display_name: `账号 ${id}`,
  account_type: 'promoter',
  operation_mode: 'growth',
  asset_tier: 'unknown',
  warmup_stage: 'normal',
  status: 'online',
  spam_check_status: 'unknown',
  risk_score: 0,
  risk_level: 'normal',
  country_code: 'CN',
  api_config_name: 'default',
  session_name: `session-${id}`,
  proxy_mode: 'dynamic',
  is_active: true,
  connection_count: 1,
  error_count: 0,
  created_at: '2026-09-10T00:00:00Z',
  updated_at: '2026-09-10T00:00:00Z',
  persona: {
    account_id: id,
    configured: true,
    name: `Persona ${id}`,
    revision: 3,
    applicable: true,
    effective_enabled: true,
  },
})

const detail = (id: number, revision = 3): AccountPersonaDetail => ({
  account_id: id,
  account_type: 'promoter',
  operation_mode: 'growth',
  persona_applicable: true,
  blocking_reason: null,
  configured: true,
  revision,
  persona_hash: 'a'.repeat(64),
  persona: persona(`Persona ${id}`),
  updated_at: '2026-09-10T10:00:00Z',
  updated_by: { id: 2, username: 'admin' },
  feature: {
    static_enabled: true,
    runtime_enabled: true,
    effective_enabled: true,
    runtime_will_apply: true,
  },
  limits: { max_total_bytes: 16384, max_system_prompt_chars: 1000 },
  preview_targets: [
    {
      owned_group_asset_id: 25,
      group_name: '测试群',
      policy_id: 8,
      policy_revision: 6,
      available_categories: ['community'],
      governance_status: 'managed',
      runtime_send_eligible: true,
      blocking_reasons: [],
    },
  ],
})

const stubs = {
  PersonaForm: {
    props: ['modelValue'],
    template: '<div data-testid="form">{{ modelValue.name }}</div>',
  },
  PersonaPreviewPanel: {
    methods: { requestPreview: () => undefined, resetForm: () => undefined },
    template: '<div data-testid="preview" />',
  },
  'el-alert': { props: ['title'], template: '<div>{{ title }}</div>' },
  'el-button': { template: '<button><slot /></button>' },
  'el-descriptions': { template: '<div><slot /></div>' },
  'el-descriptions-item': { template: '<div><slot /></div>' },
  'el-drawer': { template: '<div><slot /><slot name="footer" /></div>' },
  'el-tag': { template: '<span><slot /></span>' },
}

interface DrawerViewModel {
  conflictError: { code: string } | null
  draftPersona: AccountPersona
  fieldErrors: AccountPersonaFieldErrors
  loadedRevision: number
  previewPersona: (input: AccountPersonaPreviewInput) => Promise<void>
  resetPersona: () => Promise<void>
  savePersona: () => Promise<void>
}

const mountDrawer = (selected: Account, canEdit = true) =>
  mount(AccountPersonaDrawer, {
    props: { visible: true, account: selected, canEdit },
    global: {
      plugins: [createPinia()],
      stubs,
      directives: { loading: { mounted: () => undefined } },
    },
  })

describe('AccountPersonaDrawer', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    ui.confirm.mockResolvedValue('confirm')
    api.get.mockImplementation((accountId: number) => Promise.resolve(detail(accountId)))
  })

  it('does not request full Persona for a non-admin read-only mount', async () => {
    const wrapper = mountDrawer(account(17), false)
    await flushPromises()

    expect(api.get).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('完整 Persona 仅管理员可操作')
  })

  it('clears draft, preview and validation state when switching accounts', async () => {
    const wrapper = mountDrawer(account(17))
    await flushPromises()
    const store = useAccountPersonaStore()
    const vm = wrapper.vm as unknown as DrawerViewModel
    vm.draftPersona = persona('账号 17 本地草稿')
    vm.fieldErrors = { tone: '旧账号错误' }
    store.previewResult = {
      preview_id: 'old-preview',
      account_id: 17,
      owned_group_asset_id: 25,
      content_category: 'community',
      persona_source: 'draft',
      persona_revision: null,
      base_account_revision: 3,
      persona_hash: 'a'.repeat(64),
      prompt_template_version: 'owned-group-persona-v1',
      prompt_hash: 'b'.repeat(64),
      governance_rules_hash: 'c'.repeat(64),
      sample_text: '旧账号样例',
      promotion_composition: null,
      effective_constraints: {
        language: 'zh_cn',
        max_chars: 80,
        allowed_topics: [],
        forbidden_topic_count: 0,
        promotion_url_locked: true,
      },
      governance: { allowed: true, reason_code: null },
      feature: { effective_enabled: true, runtime_will_apply: true },
      warnings: [],
      usage: { provider: 'openai', model: 'model', input_tokens: 1, output_tokens: 1 },
    }

    await wrapper.setProps({ account: account(18) })
    await flushPromises()

    expect(vm.draftPersona.name).toBe('Persona 18')
    expect(vm.fieldErrors).toEqual({})
    expect(store.previewResult).toBeNull()
    expect(store.activeAccountId).toBe(18)
    expect(store.hasCachedRevision(17, 3)).toBe(false)
  })

  it('keeps the local draft and authoritative detail on a 409 conflict', async () => {
    api.replace.mockRejectedValue({
      response: {
        status: 409,
        data: {
          error: {
            code: 'PERSONA_REVISION_CONFLICT',
            details: { current_revision: 4 },
            retryable: false,
          },
        },
      },
    })
    ui.confirm.mockRejectedValueOnce('close')
    const wrapper = mountDrawer(account(17))
    await flushPromises()
    const vm = wrapper.vm as unknown as DrawerViewModel
    vm.draftPersona = persona('本地未保存草稿')
    await nextTick()

    await vm.savePersona()

    expect(api.replace).toHaveBeenCalledWith(
      17,
      expect.objectContaining({ expected_revision: 3, persona: expect.objectContaining({ name: '本地未保存草稿' }) }),
    )
    expect(vm.draftPersona.name).toBe('本地未保存草稿')
    expect(useAccountPersonaStore().detail?.persona?.name).toBe('Persona 17')
    expect(vm.conflictError?.code).toBe('PERSONA_REVISION_CONFLICT')
  })

  it('requires confirmation and sends loadedRevision when restoring neutral defaults', async () => {
    api.reset.mockResolvedValue({
      ...detail(17, 4),
      configured: false,
      persona: null,
      persona_hash: null,
    })
    const wrapper = mountDrawer(account(17))
    await flushPromises()
    const vm = wrapper.vm as unknown as DrawerViewModel

    await vm.resetPersona()

    expect(ui.confirm).toHaveBeenCalledWith(
      expect.stringContaining('只影响之后创建的 AI 任务'),
      '恢复中性默认',
      expect.objectContaining({ confirmButtonText: '确认恢复' }),
    )
    expect(api.reset).toHaveBeenCalledWith(17, { expected_revision: 3 })
    expect(vm.loadedRevision).toBe(4)
  })

  it('keeps a dirty draft and clears the old preview when preview fails', async () => {
    api.preview.mockRejectedValue({
      response: { status: 429, data: { error: { code: 'PERSONA_PREVIEW_RATE_LIMITED' } } },
    })
    const wrapper = mountDrawer(account(17))
    await flushPromises()
    const vm = wrapper.vm as unknown as DrawerViewModel
    vm.draftPersona = persona('预览草稿')
    await nextTick()

    await vm.previewPersona({
      owned_group_asset_id: 25,
      content_category: 'community',
      trigger_type: 'manual',
      topic: '节点稳定性',
    })

    expect(api.preview).toHaveBeenCalledWith(
      17,
      expect.objectContaining({ draft_persona: expect.objectContaining({ name: '预览草稿' }) }),
      expect.stringMatching(/^[a-zA-Z0-9-]+$/),
    )
    expect(vm.draftPersona.name).toBe('预览草稿')
    expect(useAccountPersonaStore().previewResult).toBeNull()
  })
})
