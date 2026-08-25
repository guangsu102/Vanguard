import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import Accounts from './Accounts.vue'

const {
  fetchList,
  update,
  remove,
  enable,
  disable,
  setPage,
  setPageSize,
  setAccountTypeFilter,
  push,
  getAdDynamicStatus,
} = vi.hoisted(() => ({
  fetchList: vi.fn().mockResolvedValue([]),
  update: vi.fn().mockResolvedValue({}),
  remove: vi.fn().mockResolvedValue({}),
  enable: vi.fn().mockResolvedValue({}),
  disable: vi.fn().mockResolvedValue({}),
  setPage: vi.fn(),
  setPageSize: vi.fn(),
  setAccountTypeFilter: vi.fn(),
  push: vi.fn(),
  getAdDynamicStatus: vi.fn().mockResolvedValue({
    data: {
      data: [
        {
          account_id: 1,
          account_label: 'Alice',
          risk_level: 'limited',
          risk_score: 42,
          health_score: 71,
          ad_eligible_groups: 3,
          growth_health_allowed: false,
          recent_errors: [],
          delivery_diagnostic: {
            ad_delivery_allowed: false,
            probe_execution_allowed: true,
            primary_block_label: '广告频控阻塞',
            primary_block_severity: 'warning',
            block_reasons: [],
            blocked_group_samples: [],
          },
        },
      ],
    },
  }),
}))

vi.mock('vue-router', () => ({
  useRouter: () => ({
    push,
  }),
  useRoute: () => ({
    query: {},
  }),
}))

vi.mock('@/api/proxies', () => ({
  proxiesApi: {
    list: vi.fn().mockResolvedValue({ data: { data: { list: [] } } }),
  },
}))

vi.mock('@/api/automation', () => ({
  automationApi: {
    getAdDynamicStatus,
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
  'el-tabs': { template: '<div><slot /></div>' },
  'el-tab-pane': { template: '<div><slot /></div>' },
  'el-empty': { template: '<div />' },
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
    getAdDynamicStatus.mockClear()
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

  it('shows an obvious delivery status and opens account details', async () => {
    const wrapper = mount(Accounts, {
      global: globalConfig,
    })
    await flushPromises()

    const vm = wrapper.vm as any
    const account = { id: 1, identifier: 'alice-promoter' }

    expect(getAdDynamicStatus).toHaveBeenCalledTimes(1)
    expect(vm.columns.some((column: any) => column.slot === 'deliveryStatus')).toBe(true)
    expect(vm.deliveryStatusLabel(account)).toBe('广告频控阻塞')
    expect(vm.deliveryStatusType(account)).toBe('warning')

    vm.openDeliveryBlockDrawer(account)
    expect(vm.deliveryBlockDrawerVisible).toBe(true)
    expect(vm.selectedDeliveryStatus.account_id).toBe(1)
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
