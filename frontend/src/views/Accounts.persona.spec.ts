import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import type { Account } from '@/api/accounts'
import type { AccountPersonaDetail } from '@/api/accountPersonas'
import Accounts from './Accounts.vue'

const mocks = vi.hoisted(() => ({
  auth: { role: 'admin' },
  fetchList: vi.fn().mockResolvedValue([]),
  getAdDynamicStatus: vi.fn().mockResolvedValue({ data: { data: [] } }),
  getPersona: vi.fn(),
  getAccountById: vi.fn(),
  push: vi.fn(),
  route: { query: {} as Record<string, string | undefined> },
  setAccountTypeFilter: vi.fn(),
  updatePersonaSummary: vi.fn(),
}))

const makeAccount = (
  id: number,
  persona: Account['persona'],
  operationMode: Account['operation_mode'] = 'growth',
): Account => ({
  id,
  identifier: `promoter-${id}`,
  display_name: `推广账号 ${id}`,
  account_type: 'promoter',
  operation_mode: operationMode,
  asset_tier: 'unknown',
  warmup_stage: 'normal',
  status: 'online',
  country_code: 'CN',
  api_config_name: 'default',
  session_name: `session-${id}`,
  proxy_mode: 'dynamic',
  is_active: true,
  connection_count: 1,
  error_count: 0,
  created_at: '2026-09-10T00:00:00Z',
  updated_at: '2026-09-10T00:00:00Z',
  persona,
})

const accounts: Account[] = [
  makeAccount(1, {
    account_id: 1,
    configured: true,
    name: '稳健顾问',
    revision: 7,
    applicable: true,
  }),
  makeAccount(2, {
    account_id: 2,
    configured: true,
    name: '轻松朋友',
    revision: 4,
    applicable: true,
    effective_enabled: false,
  }),
  makeAccount(3, {
    account_id: 3,
    configured: false,
    name: null,
    revision: 0,
    applicable: false,
    effective_enabled: false,
  }, 'ad_only'),
]

vi.mock('vue-router', () => ({
  useRoute: () => mocks.route,
  useRouter: () => ({ push: mocks.push }),
}))

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({ userInfo: mocks.auth }),
}))

vi.mock('@/stores/account', () => ({
  useAccountStore: () => ({
    list: accounts,
    total: accounts.length,
    page: 1,
    pageSize: 20,
    fetchList: mocks.fetchList,
    setAccountTypeFilter: mocks.setAccountTypeFilter,
    setPage: vi.fn(),
    setPageSize: vi.fn(),
    update: vi.fn(),
    updateProxyPolicy: vi.fn(),
    remove: vi.fn(),
    enable: vi.fn(),
    disable: vi.fn(),
    syncProfileBio: vi.fn(),
    updatePersonaSummary: mocks.updatePersonaSummary,
  }),
}))

vi.mock('@/api/accountPersonas', () => ({
  accountPersonasApi: { get: mocks.getPersona },
}))

vi.mock('@/api/accounts', () => ({
  accountsApi: {
    getById: mocks.getAccountById,
    getRiskSummary: vi.fn(),
    getRiskEvents: vi.fn(),
    manualAdjustRisk: vi.fn(),
    manualBan: vi.fn(),
  },
}))

vi.mock('@/api/proxies', () => ({
  proxiesApi: { list: vi.fn().mockResolvedValue({ data: { data: { list: [] } } }) },
}))

vi.mock('@/api/automation', () => ({
  automationApi: { getAdDynamicStatus: mocks.getAdDynamicStatus },
}))

vi.mock('@/components/TableCard.vue', () => ({
  default: {
    name: 'TableCard',
    props: ['data'],
    template: `
      <div>
        <div v-for="row in data" :key="row.id" class="account-row">
          <slot name="persona" :row="row" />
          <slot name="actions" :row="row" />
        </div>
      </div>
    `,
  },
}))

vi.mock('@/components/accounts/AccountPersonaDrawer.vue', () => ({
  default: {
    name: 'AccountPersonaDrawer',
    props: ['visible', 'account', 'canEdit'],
    emits: ['updated', 'update:visible'],
    template: '<div v-if="visible" data-testid="persona-drawer">{{ account?.id }}</div>',
  },
}))

vi.mock('@/components/SearchBar.vue', () => ({
  default: { name: 'SearchBar', template: '<div />' },
}))
vi.mock('@/components/FormDrawer.vue', () => ({
  default: { name: 'FormDrawer', template: '<div />' },
}))
vi.mock('@/components/StatusTag.vue', () => ({
  default: { name: 'StatusTag', template: '<span />' },
}))
vi.mock('@/components/AccountLoginDialog.vue', () => ({
  default: { name: 'AccountLoginDialog', template: '<div />' },
}))
vi.mock('@/components/AccountOperationalStatusPanel.vue', () => ({
  default: { name: 'AccountOperationalStatusPanel', template: '<div />' },
}))
vi.mock('@/components/AccountDeliveryBlockDrawer.vue', () => ({
  default: { name: 'AccountDeliveryBlockDrawer', template: '<div />' },
}))
vi.mock('@/components/ClientListPagination.vue', () => ({
  default: { name: 'ClientListPagination', template: '<div />' },
}))

const globalConfig = {
  stubs: {
    'el-alert': { template: '<div />' },
    'el-card': { template: '<section><slot name="header"/><slot /></section>' },
    'el-button': { props: ['disabled'], template: '<button :disabled="disabled"><slot /></button>' },
    'el-icon': { template: '<span><slot /></span>' },
    'el-tag': { template: '<span><slot /></span>' },
    'el-tabs': { template: '<div><slot /></div>' },
    'el-tab-pane': { template: '<div />' },
    'el-drawer': { template: '<div />' },
    'el-table': { template: '<table />' },
    'el-table-column': { template: '<td />' },
    'el-descriptions': { template: '<div />' },
    'el-descriptions-item': { template: '<div />' },
    'el-empty': { template: '<div />' },
  },
  directives: { loading: { mounted: () => undefined } },
}

const mountView = () => mount(Accounts, { global: globalConfig })

describe('Accounts Persona integration', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.auth.role = 'admin'
    mocks.route.query = {}
    mocks.getAccountById.mockResolvedValue(makeAccount(99, { account_id: 99, configured: false, name: null, revision: 0, applicable: true }))
  })

  it('renders list summaries without issuing per-row full Persona requests', async () => {
    const wrapper = mountView()
    await flushPromises()
    const rows = wrapper.findAll('.account-row')

    expect(rows[0]?.text()).toContain('已配置 · v7')
    expect(rows[0]?.text()).not.toContain('已暂停应用')
    expect(rows[1]?.text()).toContain('已暂停应用')
    expect(rows[2]?.text()).toContain('当前不适用')
    expect(mocks.getPersona).not.toHaveBeenCalled()
    expect(mocks.fetchList).toHaveBeenCalledWith({ account_type: 'promoter' })
  })

  it('opens the selected account drawer for an administrator', async () => {
    const wrapper = mountView()
    const personaButton = wrapper.findAll('button').find((button) => button.text().includes('AI 性格'))

    expect(personaButton).toBeDefined()
    await personaButton?.trigger('click')

    const drawer = wrapper.findComponent({ name: 'AccountPersonaDrawer' })
    expect(drawer.props('visible')).toBe(true)
    expect((drawer.props('account') as Account).id).toBe(1)
    expect(drawer.props('canEdit')).toBe(true)
  })

  it('opens the requested Persona drawer from the account route query', async () => {
    mocks.route.query = { persona_account_id: '2', tab: 'list' }
    const wrapper = mountView()
    await flushPromises()

    const drawer = wrapper.findComponent({ name: 'AccountPersonaDrawer' })
    expect(drawer.props('visible')).toBe(true)
    expect((drawer.props('account') as Account).id).toBe(2)
    expect((wrapper.vm as any).activeAccountTab).toBe('list')
  })

  it('hides the full Persona entry from non-admin users', () => {
    mocks.auth.role = 'auditor'
    const wrapper = mountView()

    expect(wrapper.findAll('button').some((button) => button.text().includes('AI 性格'))).toBe(false)
    expect(mocks.getPersona).not.toHaveBeenCalled()
  })

  it('writes an authoritative drawer response back into the list summary', () => {
    const wrapper = mountView()
    const drawer = wrapper.findComponent({ name: 'AccountPersonaDrawer' })
    const authoritative = {
      account_id: 1,
      configured: true,
      revision: 8,
      persona: {
        name: '更新后的 Persona',
      },
    } as AccountPersonaDetail

    drawer.vm.$emit('updated', authoritative)

    expect(mocks.updatePersonaSummary).toHaveBeenCalledWith(authoritative)
  })

  it('keeps an off-page account location in a local card without polluting pagination', async () => {
    mocks.route.query = { account_id: '99', assetId: '18', tab: 'list' }
    const wrapper = mountView()
    await flushPromises()

    expect(mocks.getAccountById).toHaveBeenCalledWith(99, expect.any(AbortSignal))
    expect((wrapper.vm as any).focusedAccount.id).toBe(99)
    expect(accounts.map((item) => item.id)).toEqual([1, 2, 3])
    expect((wrapper.vm as any).returnAssetId).toBe(18)
    expect((wrapper.vm as any).personaDrawerVisible).toBe(false)
    wrapper.unmount()
  })
})
