import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'

import type {
  AccountPersonaPreviewResult,
  AccountPersonaPreviewTarget,
} from '@/api/accountPersonas'
import PersonaPreviewPanel from './PersonaPreviewPanel.vue'

const target: AccountPersonaPreviewTarget = {
  owned_group_asset_id: 25,
  group_name: '测试自建群',
  policy_id: 8,
  policy_revision: 6,
  available_categories: ['community', 'promotion'],
  governance_status: 'degraded',
  runtime_send_eligible: false,
  blocking_reasons: ['GOVERNANCE_NOT_MANAGED'],
}

const result: AccountPersonaPreviewResult = {
  preview_id: 'preview-1',
  account_id: 17,
  owned_group_asset_id: 25,
  content_category: 'promotion',
  persona_source: 'draft',
  persona_revision: null,
  base_account_revision: 3,
  persona_hash: 'a'.repeat(64),
  prompt_template_version: 'owned-group-persona-v1',
  prompt_hash: 'b'.repeat(64),
  governance_rules_hash: 'c'.repeat(64),
  sample_text: '正文\n了解详情\nhttps://example.com/promo',
  promotion_composition: {
    body_text: '正文',
    cta_text: '了解详情',
    destination_url: 'https://example.com/promo',
    final_text: '正文\n了解详情\nhttps://example.com/promo',
  },
  effective_constraints: {
    language: 'zh_cn',
    max_chars: 80,
    allowed_topics: ['节点稳定性'],
    forbidden_topic_count: 4,
    promotion_url_locked: true,
  },
  governance: { allowed: true, reason_code: null },
  feature: { effective_enabled: false, runtime_will_apply: false },
  warnings: ['PERSONA_FEATURE_DISABLED_FOR_RUNTIME'],
  usage: { provider: 'openai', model: 'configured-model', input_tokens: 210, output_tokens: 28 },
}

const stubs = {
  'el-alert': { props: ['title'], template: '<div>{{ title }}</div>' },
  'el-button': { template: '<button><slot /></button>' },
  'el-checkbox': { template: '<label><slot /></label>' },
  'el-descriptions': { template: '<div><slot /></div>' },
  'el-descriptions-item': { template: '<div><slot /></div>' },
  'el-empty': { props: ['description'], template: '<div>{{ description }}</div>' },
  'el-form': { template: '<form><slot /></form>' },
  'el-form-item': { template: '<label><slot /></label>' },
  'el-input': {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<input :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
  },
  'el-option': { template: '<span><slot /></span>' },
  'el-radio-button': { template: '<button><slot /></button>' },
  'el-radio-group': { template: '<div><slot /></div>' },
  'el-select': { template: '<div><slot /></div>' },
  'el-tag': { template: '<span><slot /></span>' },
}

describe('PersonaPreviewPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('does not send sample_context until the administrator explicitly enables it', async () => {
    const wrapper = mount(PersonaPreviewPanel, {
      props: { targets: [target], result: null },
      global: { stubs },
    })
    await wrapper.find('input').setValue('节点稳定性')
    await wrapper.get('[data-testid="persona-preview-button"]').trigger('click')

    const calls = wrapper.emitted('preview')
    expect(calls).toHaveLength(1)
    expect(calls?.[0]?.[0]).toEqual({
      owned_group_asset_id: 25,
      content_category: 'community',
      trigger_type: 'manual',
      topic: '节点稳定性',
    })
  })

  it('blocks duplicate clicks while preview is loading', async () => {
    const wrapper = mount(PersonaPreviewPanel, {
      props: { targets: [target], result: null, loading: true },
      global: { stubs },
    })
    await wrapper.get('[data-testid="persona-preview-button"]').trigger('click')
    expect(wrapper.emitted('preview')).toBeUndefined()
  })

  it('renders promotion composition as separate body, CTA and read-only URL sections', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText },
    })
    const wrapper = mount(PersonaPreviewPanel, {
      props: { targets: [target], result, runtimeWillApply: false },
      global: { stubs },
    })

    expect(wrapper.text()).toContain('AI 推广正文')
    expect(wrapper.text()).toContain('确定性 CTA')
    expect(wrapper.text()).toContain('只读 URL')
    expect(wrapper.text()).toContain('Persona 当前不会用于实际发送')
    const copyButton = wrapper.findAll('button').find((button) => button.text().includes('只复制样例'))
    expect(copyButton).toBeDefined()
    await copyButton?.trigger('click')
    expect(writeText).toHaveBeenCalledWith(result.sample_text)
  })
})
