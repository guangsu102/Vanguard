import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import Rules from './Rules.vue'

const moderationMocks = vi.hoisted(() => ({
  getStats: vi.fn().mockResolvedValue({
    data: {
      data: {
        total: 1,
        pending: 1,
        approved: 0,
        rejected: 0,
        by_category: {},
      },
    },
  }),
  listSuggestions: vi.fn().mockResolvedValue({
    data: {
      data: [
        {
          id: 1,
          keyword: 'Spam Check',
          category: 'sensitive',
          confidence: 0.9,
          source_sample: 'spam sample',
          status: 'pending',
          created_at: '2026-05-20T10:00:00Z',
        },
      ],
      total: 1,
    },
  }),
  listViolations: vi.fn().mockResolvedValue({
    data: {
      data: [
        {
          id: 1,
          user_id: 1,
          group_id: 10,
          rule_type: 'sensitive',
          rule_pattern: 'spam',
          content: 'spam sample',
          action_taken: 'mute',
          created_at: '2026-05-20T10:00:00Z',
        },
      ],
      total: 1,
    },
  }),
}))

vi.mock('@/api/moderation', () => ({
  moderationApi: {
    getStats: moderationMocks.getStats,
    listSuggestions: moderationMocks.listSuggestions,
    listViolations: moderationMocks.listViolations,
    approveSuggestion: vi.fn().mockResolvedValue({}),
    rejectSuggestion: vi.fn().mockResolvedValue({}),
    batchReview: vi.fn().mockResolvedValue({}),
    generateSuggestions: vi.fn().mockResolvedValue({}),
  },
}))

vi.mock('@/components/TableCard.vue', () => ({
  default: {
    name: 'TableCard',
    props: ['data'],
    template: '<div><slot /><div v-for="row in data" :key="row.id">{{ row.keyword || row.content }}</div></div>',
  },
}))
vi.mock('@/components/SearchBar.vue', () => ({
  default: { name: 'SearchBar', template: '<div />' },
}))

const stubs = {
  'el-alert': { template: '<div><slot /></div>' },
  'el-button': { template: '<button><slot /></button>' },
  'el-dialog': { template: '<div><slot /></div>' },
  'el-form': { template: '<form><slot /></form>' },
  'el-form-item': { template: '<div><slot /></div>' },
  'el-icon': { template: '<span><slot /></span>' },
  'el-input': { template: '<input />' },
  'el-input-number': { template: '<input />' },
  'el-option': { template: '<option />' },
  'el-select': { template: '<select><slot /></select>' },
  'el-tag': { template: '<span><slot /></span>' },
}

describe('Rules view', () => {
  beforeEach(() => {
    moderationMocks.getStats.mockClear()
    moderationMocks.listSuggestions.mockClear()
    moderationMocks.listViolations.mockClear()
  })

  it('renders moderation suggestions', async () => {
    const wrapper = mount(Rules, {
      global: { stubs },
    })

    await flushPromises()

    expect(wrapper.text()).toContain('审核规则')
    expect(wrapper.text()).toContain('Spam Check')
  })

  it('loads moderation data on mount', async () => {
    mount(Rules, {
      global: { stubs },
    })

    await flushPromises()

    expect(moderationMocks.getStats).toHaveBeenCalledTimes(1)
    expect(moderationMocks.listSuggestions).toHaveBeenCalledTimes(1)
    expect(moderationMocks.listViolations).toHaveBeenCalledTimes(1)
  })
})
