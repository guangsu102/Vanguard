import { describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import Rules from './Rules.vue'

const fetchList = vi.fn().mockResolvedValue([])
const update = vi.fn().mockResolvedValue({})
const create = vi.fn().mockResolvedValue({})
const remove = vi.fn().mockResolvedValue({})
const enable = vi.fn().mockResolvedValue({})
const disable = vi.fn().mockResolvedValue({})
const testRule = vi.fn().mockResolvedValue({ matched: true, matchedConditions: ['关键词'], executedActions: ['警告'] })
const setPage = vi.fn()
const setPageSize = vi.fn()

vi.mock('@/stores/rule', () => ({
  useRuleStore: () => ({
    list: [
      {
        id: 1,
        name: 'Spam Check',
        type: 'spam_protection',
        description: 'block spam',
        conditions: [],
        actions: [],
        priority: 100,
        status: 'active',
        createdAt: '2026-05-20T10:00:00Z',
      },
    ],
    total: 1,
    page: 1,
    pageSize: 20,
    fetchList,
    setPage,
    setPageSize,
    update,
    create,
    remove,
    enable,
    disable,
    test: testRule,
  }),
}))

vi.mock('@/components/TableCard.vue', () => ({
  default: { name: 'TableCard', template: '<div><slot /></div>' },
}))
vi.mock('@/components/SearchBar.vue', () => ({
  default: { name: 'SearchBar', template: '<div />' },
}))
vi.mock('@/components/FormDrawer.vue', () => ({
  default: { name: 'FormDrawer', template: '<div><slot /></div>' },
}))

describe('Rules view', () => {
  it('renders rules page title and row', () => {
    const wrapper = mount(Rules, {
      global: {
        stubs: {
          'el-button': { template: '<button><slot /></button>' },
          'el-icon': { template: '<span><slot /></span>' },
          'el-tag': { template: '<span><slot /></span>' },
          'el-switch': { template: '<button />' },
          'el-drawer': { template: '<div><slot /></div>' },
          'el-form': { template: '<form><slot /></form>' },
          'el-form-item': { template: '<div><slot /></div>' },
          'el-input': { template: '<input />' },
          'el-input-number': { template: '<input />' },
          'el-select': { template: '<select><slot /></select>' },
          'el-option': { template: '<option />' },
          'el-checkbox': { template: '<input type="checkbox" />' },
          'el-alert': { template: '<div><slot /></div>' },
          'el-tooltip': { template: '<div><slot /></div>' },
        },
      },
    })

    expect(wrapper.text()).toContain('审核规则')
    expect(wrapper.text()).toContain('Spam Check')
  })

  it('loads rules on mount', () => {
    mount(Rules, {
      global: {
        stubs: {
          'el-button': { template: '<button><slot /></button>' },
          'el-icon': { template: '<span><slot /></span>' },
          'el-tag': { template: '<span><slot /></span>' },
          'el-switch': { template: '<button />' },
          'el-drawer': { template: '<div><slot /></div>' },
          'el-form': { template: '<form><slot /></form>' },
          'el-form-item': { template: '<div><slot /></div>' },
          'el-input': { template: '<input />' },
          'el-input-number': { template: '<input />' },
          'el-select': { template: '<select><slot /></select>' },
          'el-option': { template: '<option />' },
          'el-checkbox': { template: '<input type="checkbox" />' },
          'el-alert': { template: '<div><slot /></div>' },
          'el-tooltip': { template: '<div><slot /></div>' },
        },
      },
    })

    expect(fetchList).toHaveBeenCalledTimes(1)
  })
})
