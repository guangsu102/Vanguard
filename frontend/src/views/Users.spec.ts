import { describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import Users from './Users.vue'

vi.mock('@/stores/user', () => ({
  useUserStore: () => ({
    list: [],
    total: 0,
    page: 1,
    pageSize: 20,
    fetchList: vi.fn().mockResolvedValue([]),
    setPage: vi.fn(),
    setPageSize: vi.fn(),
  }),
}))

vi.mock('@/components/TableCard.vue', () => ({
  default: { name: 'TableCard', template: '<div><slot /></div>' },
}))
vi.mock('@/components/SearchBar.vue', () => ({
  default: { name: 'SearchBar', template: '<div />' },
}))
vi.mock('@/components/StatusTag.vue', () => ({
  default: { name: 'StatusTag', template: '<span>status</span>' },
}))

describe('Users view', () => {
  it('renders users page title', () => {
    const wrapper = mount(Users, {
      global: {
        stubs: {
          'el-button': { template: '<button><slot /></button>' },
          'el-icon': { template: '<span><slot /></span>' },
          'el-tag': { template: '<span><slot /></span>' },
          'el-table': { template: '<table><slot /></table>' },
          'el-table-column': { template: '<div />' },
        },
      },
    })

    expect(wrapper.text()).toContain('用户管理')
  })
})
