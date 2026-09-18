import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { nextTick } from 'vue'

const api = vi.hoisted(() => ({
  listAccounts: vi.fn(),
  createOperation: vi.fn(),
  getLatestOperation: vi.fn(),
  getOperation: vi.fn(),
  cancelOperation: vi.fn(),
  createKey: vi.fn(() => 'profile-update-key'),
}))
const ui = vi.hoisted(() => ({
  confirm: vi.fn(),
  success: vi.fn(),
  warning: vi.fn(),
}))

vi.mock('@/api/accounts', () => ({
  accountsApi: {
    list: api.listAccounts,
  },
}))
vi.mock('@/api/accountProfileUpdates', () => ({
  accountProfileUpdatesApi: {
    createOperation: api.createOperation,
    getLatestOperation: api.getLatestOperation,
    getOperation: api.getOperation,
    cancelOperation: api.cancelOperation,
  },
  createAccountProfileUpdateIdempotencyKey: api.createKey,
}))
vi.mock('element-plus', () => ({
  ElMessage: {
    success: ui.success,
    warning: ui.warning,
  },
  ElMessageBox: {
    confirm: ui.confirm,
  },
}))

import type { Account } from '@/api/accounts'
import type { AccountProfileUpdateOperation } from '@/api/accountProfileUpdates'
import AccountProfileUpdateDialog from './AccountProfileUpdateDialog.vue'

const makeAccount = (id: number, overrides: Partial<Account> = {}): Account => ({
  id,
  identifier: `account-${id}`,
  display_name: `账号 ${id}`,
  phone: `1550000${id}`,
  account_type: 'promoter',
  operation_mode: 'ad_only',
  asset_tier: 'unknown',
  warmup_stage: 'normal',
  status: 'online',
  spam_check_status: 'unknown',
  risk_score: 0,
  risk_level: 'normal',
  country_code: 'US',
  api_config_name: 'default',
  session_name: `session-${id}`,
  proxy_mode: 'dynamic',
  is_active: true,
  connection_count: 0,
  error_count: 0,
  created_at: '2026-09-15T00:00:00Z',
  updated_at: '2026-09-15T00:00:00Z',
  ...overrides,
})

const queuedOperation: AccountProfileUpdateOperation = {
  id: 52,
  status: 'queued',
  profile_bio: '广告账号简介',
  total_accounts: 1,
  processed_accounts: 0,
  succeeded_accounts: 0,
  failed_accounts: 0,
  cancelled_accounts: 0,
  skipped_accounts: 0,
  max_attempts: 3,
  last_error: null,
  created_at: '2026-09-15T00:10:00Z',
  items: [{
    id: 71,
    account_id: 1,
    status: 'pending',
    attempts: 0,
    reason_code: null,
    error_message: null,
  }],
}

const stubs = {
  'el-dialog': { template: '<div><slot /><slot name="footer" /></div>' },
  'el-alert': { props: ['title', 'description'], template: '<div>{{ title }}{{ description }}</div>' },
  'el-button': { props: ['disabled'], template: '<button :disabled="disabled"><slot /></button>' },
  'el-tag': { template: '<span><slot /></span>' },
  'el-progress': { template: '<div />' },
  'el-table': { template: '<table><slot /></table>' },
  'el-table-column': { template: '<td />' },
  'el-input': { template: '<input />' },
  'el-scrollbar': { template: '<div><slot /></div>' },
  'el-checkbox-group': { template: '<div><slot /></div>' },
  'el-checkbox': { props: ['disabled'], template: '<label :data-disabled="disabled"><slot /></label>' },
  'el-empty': { template: '<div />' },
}

interface DialogViewModel {
  profileUpdateAccounts: Account[]
  profileUpdateEligibleAccounts: Account[]
  profileUpdateEligibilityReason: (account: Account) => string
  profileUpdateSelectedIds: number[]
  profileUpdateBio: string
  profileUpdateOperation: AccountProfileUpdateOperation | null
  submitProfileUpdateOperation: () => Promise<void>
}

const mountDialog = () => mount(AccountProfileUpdateDialog, {
  props: { visible: true },
  global: {
    stubs,
    directives: { loading: { mounted: () => undefined } },
  },
})

describe('AccountProfileUpdateDialog', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    ui.confirm.mockResolvedValue('confirm')
    api.getLatestOperation.mockResolvedValue(null)
    api.listAccounts.mockResolvedValue({
      list: [
        makeAccount(1),
        makeAccount(2, { operation_mode: 'growth' }),
        makeAccount(3, { is_active: false }),
        makeAccount(4, { status: 'working' }),
      ],
      total: 4,
      nextCursor: null,
      hasMore: false,
    })
  })

  it('only exposes active ad_only promoter accounts with a usable session', async () => {
    const wrapper = mountDialog()
    await flushPromises()

    const vm = wrapper.vm as unknown as DialogViewModel
    expect(api.listAccounts).toHaveBeenCalledWith({ account_type: 'promoter', limit: 2000 })
    expect(vm.profileUpdateEligibleAccounts.map((account) => account.id)).toEqual([1])
    expect(vm.profileUpdateEligibilityReason(vm.profileUpdateAccounts[1]!)).toBe('不是广告专用账号')
    expect(vm.profileUpdateEligibilityReason(vm.profileUpdateAccounts[2]!)).toBe('账号未启用')
    expect(vm.profileUpdateEligibilityReason(vm.profileUpdateAccounts[3]!)).toBe('当前连接状态不可更新')

    wrapper.unmount()
  })

  it('confirms and submits an idempotent queued update instead of direct concurrent work', async () => {
    api.createOperation.mockResolvedValue(queuedOperation)
    const wrapper = mountDialog()
    await flushPromises()
    const vm = wrapper.vm as unknown as DialogViewModel
    vm.profileUpdateSelectedIds = [1]
    vm.profileUpdateBio = '广告账号简介'
    await nextTick()

    await vm.submitProfileUpdateOperation()

    expect(ui.confirm).toHaveBeenCalledWith(
      expect.stringContaining('全局单线程'),
      '确认排队批量设置简介',
      expect.objectContaining({ confirmButtonText: '确认排队' }),
    )
    expect(api.createOperation).toHaveBeenCalledWith(
      { account_ids: [1], profile_bio: '广告账号简介' },
      'profile-update-key',
    )
    expect(vm.profileUpdateOperation).toEqual(queuedOperation)
    expect(ui.success).toHaveBeenCalledWith('广告账号简介已排入单线程队列')

    wrapper.unmount()
  })
})
