import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { ElMessageBox } from 'element-plus'
import { accountsApi, type Account } from '@/api/accounts'
import type { SpamCheckOperation } from '@/api/accountSpamChecks'
import Accounts from './Accounts.vue'

interface AccountsViewModel {
  columns: Array<{ slot?: string }>
  deliveryBlockDrawerVisible: boolean
  deliveryStatusLabel: (account: { id: number }) => string
  deliveryStatusType: (account: { id: number }) => string
  handleDisable: (account: { id: number }) => Promise<void>
  handleEnable: (account: { id: number }) => Promise<void>
  openDeliveryBlockDrawer: (account: { id: number; identifier: string }) => void
  selectedDeliveryStatus: { account_id: number } | null
  spamCheckAccounts: Account[]
  spamCheckEligibleAccounts: Account[]
  spamCheckSelectedIds: number[]
  spamCheckOperation: SpamCheckOperation | null
  openSpamCheckDialog: () => Promise<void>
  submitSpamCheckOperation: () => Promise<void>
  spamCheckResultText: (status: Account['spam_check_status']) => string
  accountRestrictionSourceText: (account: Account) => string
}

const {
  fetchList,
  update,
  remove,
  enable,
  disable,
  setPage,
  setPageSize,
  setAccountTypeFilter,
  updatePersonaSummary,
  push,
  getAdDynamicStatus,
  createSpamCheckOperation,
  getLatestSpamCheckOperation,
  getSpamCheckOperation,
  cancelSpamCheckOperation,
  createSpamCheckKey,
} = vi.hoisted(() => ({
  fetchList: vi.fn().mockResolvedValue([]),
  update: vi.fn().mockResolvedValue({}),
  remove: vi.fn().mockResolvedValue({}),
  enable: vi.fn().mockResolvedValue({}),
  disable: vi.fn().mockResolvedValue({}),
  setPage: vi.fn(),
  setPageSize: vi.fn(),
  setAccountTypeFilter: vi.fn(),
  updatePersonaSummary: vi.fn(),
  push: vi.fn(),
  createSpamCheckOperation: vi.fn(),
  getLatestSpamCheckOperation: vi.fn(),
  getSpamCheckOperation: vi.fn(),
  cancelSpamCheckOperation: vi.fn(),
  createSpamCheckKey: vi.fn(() => 'spam-check-key'),
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

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({ userInfo: { role: 'admin' } }),
}))

vi.mock('@/api/proxies', () => ({
  proxiesApi: {
    list: vi.fn().mockResolvedValue({ data: { data: { list: [] } } }),
  },
}))

vi.mock('@/api/automation', () => ({
  automationApi: {
    getAdDynamicStatus,
  createSpamCheckOperation,
  getLatestSpamCheckOperation,
  getSpamCheckOperation,
  cancelSpamCheckOperation,
  createSpamCheckKey,
  },
}))

vi.mock('@/api/accountSpamChecks', () => ({
  accountSpamChecksApi: {
    createOperation: createSpamCheckOperation,
    getLatestOperation: getLatestSpamCheckOperation,
    getOperation: getSpamCheckOperation,
    cancelOperation: cancelSpamCheckOperation,
  },
  createSpamCheckIdempotencyKey: createSpamCheckKey,
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
        spam_check_status: 'clear',
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
        spam_check_status: 'clear',
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
    updatePersonaSummary,
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
vi.mock('@/components/accounts/AccountPersonaDrawer.vue', () => ({
  default: { name: 'AccountPersonaDrawer', template: '<div />' },
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
  'el-dialog': { template: '<div><slot /><slot name="footer" /></div>' },
  'el-input': { template: '<input />' },
  'el-scrollbar': { template: '<div><slot /></div>' },
  'el-checkbox-group': { template: '<div><slot /></div>' },
  'el-checkbox': { template: '<label><slot /></label>' },
  'el-progress': { template: '<div />' },
}

const listAccounts = vi.spyOn(accountsApi, 'list')
const confirmAction = vi.spyOn(ElMessageBox, 'confirm')

const makeAccount = (id: number, overrides: Partial<Account> = {}): Account => ({
  id,
  identifier: 'promoter-' + id,
  display_name: 'Promoter ' + id,
  phone: '1550000' + id,
  account_type: 'promoter',
  operation_mode: 'growth',
  asset_tier: 'unknown',
  warmup_stage: 'normal',
  status: 'online',
  spam_check_status: 'unknown',
  risk_score: 0,
  risk_level: 'normal',
  country_code: 'US',
  api_config_name: 'default',
  session_name: 'session-' + id,
  proxy_mode: 'dynamic',
  is_active: true,
  connection_count: 0,
  error_count: 0,
  created_at: '2026-09-13T00:00:00Z',
  updated_at: '2026-09-13T00:00:00Z',
  ...overrides,
})

const runningSpamOperation: SpamCheckOperation = {
  id: 51,
  status: 'running',
  total_accounts: 2,
  processed_accounts: 0,
  clear_accounts: 0,
  restricted_accounts: 0,
  failed_accounts: 0,
  cancelled_accounts: 0,
  last_error: null,
  created_at: '2026-09-13T01:00:00Z',
  items: [],
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
    createSpamCheckOperation.mockReset()
    getLatestSpamCheckOperation.mockReset().mockResolvedValue(null)
    getSpamCheckOperation.mockReset()
    cancelSpamCheckOperation.mockReset()
    createSpamCheckKey.mockClear()
    listAccounts.mockReset().mockResolvedValue({
      list: [makeAccount(11)],
      total: 1,
      nextCursor: null,
      hasMore: false,
    })
    confirmAction.mockReset().mockResolvedValue('confirm' as never)
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

    const vm = wrapper.vm as unknown as AccountsViewModel
    const account = { id: 1, identifier: 'alice-promoter' }

    expect(getAdDynamicStatus).toHaveBeenCalledTimes(1)
    expect(vm.columns.some((column) => column.slot === 'deliveryStatus')).toBe(true)
    expect(vm.deliveryStatusLabel(account)).toBe('广告频控阻塞')
    expect(vm.deliveryStatusType(account)).toBe('warning')

    vm.openDeliveryBlockDrawer(account)
    expect(vm.deliveryBlockDrawerVisible).toBe(true)
    expect(vm.selectedDeliveryStatus?.account_id).toBe(1)
  })

  it('calls enable and disable actions', async () => {
    const wrapper = mount(Accounts, {
      global: globalConfig,
    })

    const vm = wrapper.vm as unknown as AccountsViewModel
    await vm.handleEnable({ id: 1 })
    await vm.handleDisable({ id: 1 })

    expect(enable).toHaveBeenCalledWith(1)
    expect(disable).toHaveBeenCalledWith(1)
  })
  it('loads all promoter pages and only exposes detectable accounts', async () => {
    listAccounts
      .mockResolvedValueOnce({
        list: [
          makeAccount(11),
          makeAccount(12, { status: 'working' }),
        ],
        total: 3,
        nextCursor: 'next-page',
        hasMore: true,
      })
      .mockResolvedValueOnce({
        list: [makeAccount(13, { status: 'offline' })],
        total: 3,
        nextCursor: null,
        hasMore: false,
      })

    const wrapper = mount(Accounts, { global: globalConfig })
    const vm = wrapper.vm as unknown as AccountsViewModel
    await vm.openSpamCheckDialog()

    expect(listAccounts).toHaveBeenNthCalledWith(1, {
      account_type: 'promoter',
      limit: 2000,
    })
    expect(listAccounts).toHaveBeenNthCalledWith(2, {
      account_type: 'promoter',
      limit: 2000,
      cursor: 'next-page',
    })
    expect(vm.spamCheckAccounts.map((account) => account.id)).toEqual([11, 12, 13])
    expect(vm.spamCheckEligibleAccounts.map((account) => account.id)).toEqual([11, 13])
    wrapper.unmount()
  })

  it('submits selected accounts only after confirming a real SpamBot action', async () => {
    createSpamCheckOperation.mockResolvedValue(runningSpamOperation)
    const wrapper = mount(Accounts, { global: globalConfig })
    const vm = wrapper.vm as unknown as AccountsViewModel
    await vm.openSpamCheckDialog()
    vm.spamCheckSelectedIds = [11]

    await vm.submitSpamCheckOperation()

    expect(confirmAction).toHaveBeenCalledWith(
      expect.stringContaining('真实 Telegram'),
      '确认执行 SpamBot 检测',
      expect.objectContaining({ confirmButtonText: '确认真实执行' }),
    )
    expect(createSpamCheckOperation).toHaveBeenCalledWith(
      { account_ids: [11] },
      'spam-check-key',
    )
    wrapper.unmount()
  })

  it('polls after three seconds and separates SpamBot clear from an RPC restriction', async () => {
    vi.useFakeTimers()
    getLatestSpamCheckOperation.mockResolvedValue(runningSpamOperation)
    getSpamCheckOperation.mockResolvedValue({
      ...runningSpamOperation,
      status: 'succeeded',
      processed_accounts: 2,
      clear_accounts: 1,
      restricted_accounts: 1,
    })
    const wrapper = mount(Accounts, { global: globalConfig })
    const vm = wrapper.vm as unknown as AccountsViewModel

    await vm.openSpamCheckDialog()
    await vi.advanceTimersByTimeAsync(3000)

    expect(getSpamCheckOperation).toHaveBeenCalledWith(51)
    expect(vm.spamCheckOperation?.status).toBe('succeeded')
    expect(vm.spamCheckResultText('clear')).toContain('SpamBot')
    expect(vm.accountRestrictionSourceText(makeAccount(14, {
      status: 'restricted',
      restriction_source: 'telegram_rpc',
    }))).toBe('Telegram 操作限制')

    wrapper.unmount()
    vi.useRealTimers()
  })
})
