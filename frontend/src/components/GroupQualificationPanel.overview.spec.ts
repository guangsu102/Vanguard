import { describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import GroupQualificationPanel from './GroupQualificationPanel.vue'

const requests = vi.hoisted(() => ({ list: vi.fn(), runtime: vi.fn() }))
vi.mock('@/api/groupReviews', () => ({ listGroupReviews: requests.list, groupReviewRuntime: requests.runtime }))

const box = { template: '<div><slot /></div>' }
const stubs = {
  ElCard: box, ElDescriptions: box,
  ElDescriptionsItem: { props: ['label'], template: '<div class="metric">{{ label }} <slot /></div>' },
  ElAlert: { props: ['title'], template: '<div>{{ title }}</div>' },
  ElSelect: box, ElOption: box, ElButton: box,
  ElTable: { name: 'ElTable', props: ['data'], template: '<div><slot /></div>' },
  ElTableColumn: { template: '<span />' }, ElDrawer: { template: '<span />' }, ElTag: box,
}

async function show(readiness: Record<string, unknown>, extra: Record<string, unknown> = {}) {
  requests.list.mockReset().mockResolvedValue({ data: { data: [] } })
  requests.runtime.mockReset().mockResolvedValue({ data: { data: {
    business: { available: true, readiness, ...extra },
    measurement: { available: false }, metrics: { enabled: false },
  } } })
  const wrapper = mount(GroupQualificationPanel, { global: { stubs, directives: { loading: () => undefined } } })
  await flushPromises()
  return wrapper
}

describe('local growth readiness and independent maintenance', () => {
  it('keeps a known zero visible and marks bounded counts without requesting execution', async () => {
    const wrapper = await show({ available: true, ad_targets: 0, join_candidates: 0,
      join_candidates_preview_pending: 0, join_candidates_pending_identity: 0, sample_complete: false,
      maintenance: { available: true, items: [] } })
    expect(wrapper.findAll('.metric').find(row => row.text().includes('当前可执行广告目标'))?.text()).toBe('当前可执行广告目标 0')
    expect(wrapper.text()).toContain('数量不是全量')
    expect(wrapper.text()).toContain('不触发 Telegram 取证或预留额度')
    expect(requests.list).toHaveBeenCalledTimes(1)
    expect(requests.runtime).toHaveBeenCalledTimes(1)
    expect(requests.list).toHaveBeenCalledWith(undefined)
    expect(requests.runtime).toHaveBeenCalledWith(undefined)
  })

  it('does not turn an unavailable inventory into zero', async () => {
    const wrapper = await show({ available: false, ad_targets: null, join_candidates: null,
      maintenance: { available: false, items: [] } })
    expect(wrapper.findAll('.metric').find(row => row.text().includes('当前可执行广告目标'))?.text()).toBe('当前可执行广告目标 未知')
    expect(wrapper.findAll('.metric').find(row => row.text().includes('缓存已就绪加群候选'))?.text()).toContain('未知')
    expect(wrapper.text()).toContain('经营维护状态暂不可用')
  })

  it('renders maintenance as a separate bounded list', async () => {
    const maintenance = { available: true, truncated: true,
      items: [{ id: 9, group_id: 40, state: 'WAIT_EVENT', reason: 'business_evidence_missing' }] }
    const wrapper = await show({ available: true, ad_targets: 2, join_candidates: 1, sample_complete: true, maintenance })
    expect(wrapper.findAll('h4').map(row => row.text())).toContain('经营维护')
    expect(wrapper.text()).toContain('维护列表仅显示最近 100 项')
    expect(wrapper.text()).toContain('这里的等待不阻塞已有有效授权或新增群')
    expect(wrapper.findAllComponents({ name: 'ElTable' }).map(row => row.props('data'))).toContainEqual(maintenance.items)
  })

  it('preserves known readiness when maintenance is unavailable and hides incomplete opportunity percentages', async () => {
    const wrapper = await show({ available: true, ad_targets: 2, join_candidates: 1, sample_complete: true,
      maintenance: { available: false, reason: 'maintenance_snapshot_unavailable', items: [] } },
    { ad_opportunities: { available: true, complete: false, completion_rate: 1, due: 1, completed: 1, pending: 0, pending_before_window: 0 } })
    expect(wrapper.findAll('.metric').find(row => row.text().includes('当前可执行广告目标'))?.text()).toBe('当前可执行广告目标 2')
    expect(wrapper.text()).toContain('经营维护状态暂不可用')
    expect(wrapper.text()).toContain('未具备完整窗口')
    expect(wrapper.text()).not.toContain('100.0%')
  })
})
