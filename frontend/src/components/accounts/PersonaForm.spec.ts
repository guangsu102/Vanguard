import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'

import { validateAccountPersona, type AccountPersona } from '@/api/accountPersonas'
import PersonaForm from './PersonaForm.vue'

const persona: AccountPersona = {
  schema_version: 1,
  name: '技术型群友',
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
}

const stubs = {
  'el-form': { template: '<form><slot /></form>' },
  'el-form-item': { template: '<label><slot name="label" /><slot /></label>' },
  'el-input': {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<input :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
  },
  'el-select': { template: '<div><slot /></div>' },
  'el-option': { template: '<span />' },
  'el-radio-group': { template: '<div><slot /></div>' },
  'el-radio-button': { template: '<button><slot /></button>' },
  'el-tag': { template: '<span><slot /></span>' },
  'el-alert': { props: ['title'], template: '<div>{{ title }}</div>' },
}

describe('PersonaForm', () => {
  it('renders countable tag fields and emits a full immutable Persona update', async () => {
    const wrapper = mount(PersonaForm, {
      props: { modelValue: persona },
      global: { stubs },
    })

    expect(wrapper.text()).toContain('兴趣 0/10')
    expect(wrapper.text()).toContain('附加禁区 0/20')
    expect(wrapper.text()).toContain('低优先级风格备注，不会覆盖安全规则')

    await wrapper.find('input').setValue('新的群友性格')
    const updates = wrapper.emitted('update:modelValue')
    expect(updates).toHaveLength(1)
    expect(updates?.[0]?.[0]).toMatchObject({
      name: '新的群友性格',
      schema_version: 1,
      reply_length: 'short',
    })
    expect(persona.name).toBe('技术型群友')
  })

  it('surfaces the exact list and string boundaries used by the form', () => {
    const errors = validateAccountPersona({
      ...persona,
      name: '',
      tone: 'x'.repeat(121),
      interests: Array.from({ length: 11 }, (_, index) => `兴趣${index}`),
      catchphrases: ['x'.repeat(61)],
    })

    expect(errors.name).toContain('1–50')
    expect(errors.tone).toContain('1–120')
    expect(errors.interests).toContain('最多 10 项')
    expect(errors.catchphrases).toContain('1–60')
  })
})
