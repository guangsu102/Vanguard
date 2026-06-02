import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import Groups from './Groups.vue'

const fetchList = vi.fn().mockResolvedValue([])
const fetchMembers = vi.fn().mockResolvedValue([])
const create = vi.fn().mockResolvedValue({})
const update = vi.fn().mockResolvedValue({})
const remove = vi.fn().mockResolvedValue({})
const syncMetrics = vi.fn().mockResolvedValue({})
const setPage = vi.fn()
const setPageSize = vi.fn()
const fetchAccounts = vi.fn().mockResolvedValue([])

vi.mock('@/stores/group', () => ({
  useGroupStore: () => ({
    list: [
      {
        id: 1,
        chatId: '-1001',
        title: 'Test Group',
        username: 'testgroup',
        memberCount: 123,
        discoverySource: 'keyword_search',
        sourceKeyword: 'vpn',
        accountCount: 1,
        primaryAccountPhone: '13800000000',
        level: 'A',
        status: 'active',
        metrics: {
          adsSent: 3,
          privateMessages: 4,
          repliedUsers: 2,
          registeredUsers: 1,
          paidUsers: 1,
          conversionRate: 25,
        },
        lastMessageAt: '2026-05-24T10:00:00Z',
        createdAt: '2026-05-20T10:00:00Z',
      },
    ],
    total: 1,
    page: 1,
    pageSize: 20,
    members: [],
    memberTotal: 0,
    fetchList,
    fetchMembers,
    create,
    update,
    remove,
    syncMetrics,
    setPage,
    setPageSize,
  }),
}))

vi.mock('@/stores/account', () => ({
  useAccountStore: () => ({
    list: [
      {
        id: 1,
        identifier: 'alice-promoter',
        display_name: 'Alice',
        status: 'online',
      },
    ],
    fetchList: fetchAccounts,
  }),
}))

vi.mock('@/components/TableCard.vue', () => ({
  default: {
    name: 'TableCard',
    props: ['data'],
    template: '<div><slot /><div v-for="row in data" :key="row.id">{{ row.title }} {{ row.chatId }}</div></div>',
  },
}))
vi.mock('@/components/SearchBar.vue', () => ({
  default: { name: 'SearchBar', template: '<div />' },
}))
vi.mock('@/components/FormDrawer.vue', () => ({
  default: { name: 'FormDrawer', template: '<div />' },
}))
vi.mock('@/components/StatusTag.vue', () => ({
  default: { name: 'StatusTag', template: '<span>status</span>' },
}))

const globalStubs = {
  'el-button': { template: '<button><slot /></button>' },
  'el-icon': { template: '<span><slot /></span>' },
  'el-tag': { template: '<span><slot /></span>' },
  'el-alert': { template: '<div><slot /></div>' },
  'el-drawer': { template: '<div><slot /></div>' },
  'el-table': { template: '<table><slot /></table>' },
  'el-table-column': { template: '<div />' },
  'el-empty': { template: '<div />' },
}

describe('Groups view', () => {
  beforeEach(() => {
    fetchList.mockClear()
    fetchAccounts.mockClear()
  })

  it('renders groups page title and list data', () => {
    const wrapper = mount(Groups, {
      global: { stubs: globalStubs },
    })

    expect(wrapper.text()).toContain('群池管理')
    expect(wrapper.text()).toContain('添加群池条目')
    expect(wrapper.text()).toContain('Test Group')
  })

  it('loads groups on mount', () => {
    mount(Groups, {
      global: { stubs: globalStubs },
    })

    expect(fetchList).toHaveBeenCalledTimes(1)
    expect(fetchAccounts).toHaveBeenCalledWith({
      account_type: 'promoter',
      limit: 100,
    })
  })

  it('calls store actions for add and sync', async () => {
    const wrapper = mount(Groups, {
      global: { stubs: globalStubs },
    })

    const vm = wrapper.vm as any
    vm.openAddDrawer()
    await vm.handleSyncMetrics({ id: 1 })

    expect(syncMetrics).toHaveBeenCalledWith(1)
  })
})
