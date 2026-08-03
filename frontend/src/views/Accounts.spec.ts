import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import Accounts from './Accounts.vue'

const fetchList = vi.fn().mockResolvedValue([])
const update = vi.fn().mockResolvedValue({})
const remove = vi.fn().mockResolvedValue({})
const enable = vi.fn().mockResolvedValue({})
const disable = vi.fn().mockResolvedValue({})
const setPage = vi.fn()
const setPageSize = vi.fn()
const setAccountTypeFilter = vi.fn()
const push = vi.fn()

vi.mock('vue-router', () => ({
  useRouter: () => ({
    push,
  }),
}))

vi.mock('@/api/proxies', () => ({
  proxiesApi: {
    list: vi.fn().mockResolvedValue({ data: { data: { list: [] } } }),
  },
}))

vi.mock('@/stores/account', () => ({
  useAccountStore: () => ({
    list: [
      {
        id: 1,
        account_type: 'promoter',
        identifier: 'alice-promoter',
        display_name: 'Alice',
        phone: '13800000000',
        session_name: 'alice-session',
        status: 'online',
        is_active: true,
        country_code: 'US',
        country_name: 'United States',
        api_config_name: 'default',
        connection_count: 1,
        error_count: 0,
        last_active_at: '2026-05-24T10:00:00Z',
        created_at: '2026-05-20T10:00:00Z',
      },
      {
        id: 2,
        account_type: 'guardian_bot',
        identifier: 'guardian-bot',
        display_name: 'Guardian Bot',
        phone: '19900000000',
        session_name: 'guardian-session',
        status: 'online',
        is_active: true,
        country_code: 'US',
        api_config_name: 'default',
        connection_count: 1,
        error_count: 0,
        last_active_at: '2026-05-24T11:00:00Z',
        created_at: '2026-05-20T11:00:00Z',
      },
    ],
    total: 1,
    page: 1,
    pageSize: 20,
    fetchList,
    update,
    remove,
    enable,
    disable,
    setPage,
    setPageSize,
    setAccountTypeFilter,
  }),
}))

vi.mock('@/components/TableCard.vue', () => ({
  default: {
    name: 'TableCard',
    props: ['data'],
    template: '<div><slot /><div v-for="row in data" :key="row.id">{{ row.display_name }} {{ row.phone }}</div></div>',
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
vi.mock('@/components/AccountLoginDialog.vue', () => ({
  default: { name: 'AccountLoginDialog', template: '<div />' },
}))

const globalStubs = {
  'el-button': { template: '<button><slot /></button>' },
  'el-icon': { template: '<span><slot /></span>' },
  'el-tag': { template: '<span><slot /></span>' },
  'el-alert': { template: '<div><slot /></div>' },
  'el-drawer': { template: '<div />' },
  'el-table': { template: '<table />' },
  'el-table-column': { template: '<td />' },
  'el-descriptions': { template: '<div />' },
  'el-descriptions-item': { template: '<div />' },
}

const globalConfig = {
  stubs: globalStubs,
  directives: {
    loading: { mounted: () => undefined },
  },
}

describe('Accounts view', () => {
  beforeEach(() => {
    fetchList.mockClear()
    setAccountTypeFilter.mockClear()
    enable.mockClear()
    disable.mockClear()
    push.mockClear()
  })

  it('renders page and account row', () => {
    const wrapper = mount(Accounts, {
      global: globalConfig,
    })

    expect(wrapper.text()).toContain('推广账号')
    expect(wrapper.text()).toContain('13800000000')
    expect(wrapper.text()).not.toContain('Guardian Bot')
  })

  it('loads accounts on mount', () => {
    mount(Accounts, { global: globalConfig })
    expect(fetchList).toHaveBeenCalledTimes(1)
    expect(setAccountTypeFilter).toHaveBeenCalledWith('promoter')
    expect(fetchList).toHaveBeenCalledWith({ account_type: 'promoter' })
  })

  it('calls enable and disable actions', async () => {
    const wrapper = mount(Accounts, {
      global: globalConfig,
    })

    const vm = wrapper.vm as any
    await vm.handleEnable({ id: 1 })
    await vm.handleDisable({ id: 1 })

    expect(enable).toHaveBeenCalledWith(1)
    expect(disable).toHaveBeenCalledWith(1)
  })
})
