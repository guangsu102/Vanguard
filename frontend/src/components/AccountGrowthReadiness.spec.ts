import { describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import AccountGrowthReadiness from './AccountGrowthReadiness.vue'

const requests = vi.hoisted(() => ({ runtime: vi.fn() }))
vi.mock('@/api/groupReviews', () => ({ groupReviewRuntime: requests.runtime }))
const box = { template: '<div><slot /></div>' }
const stubs = {
  ElCard: box, ElDescriptions: box,
  ElDescriptionsItem: { props: ['label'], template: '<div class="metric">{{ label }} <slot /></div>' },
  ElAlert: { props: ['title'], template: '<div>{{ title }}</div>' },
  ElSelect: { name: 'ElSelect', props: ['modelValue'], emits: ['update:modelValue'], template: '<select><slot /></select>' },
  ElOption: { template: '<option />' },
  ElButton: { props: ['disabled', 'loading'], template: '<button :disabled="disabled"><slot /></button>' },
  ElTable: { name: 'ElTable', props: ['data'], template: '<div><slot /></div>' },
  ElTableColumn: { template: '<span />' },
}
function response(readiness: Record<string, unknown>) { return { data: { data: { business: { readiness } } } } }
function setup() {
  requests.runtime.mockReset()
  const wrapper = mount(AccountGrowthReadiness, { props: { accounts: [{ id: 33, identifier: 'A' }, { id: 34, identifier: 'B' }] }, global: { stubs } })
  async function choose(id: number) {
    wrapper.findComponent({ name: 'ElSelect' }).vm.$emit('update:modelValue', id)
    await wrapper.vm.$nextTick()
  }
  return { wrapper, choose }
}

describe('account growth readiness summary', () => {
  it('shows configured and effective quotas, preserves zero and clears the previous account', async () => {
    const { wrapper, choose } = setup()
    requests.runtime.mockResolvedValue({ data: { data: { mode: 'writable_v2', controls: {
      available: true,
      configured: { join_daily: 20, ad_daily: 70, total_daily: 150 },
      effective: { join_daily: 10, ad_daily: 30, total_daily: 62, join_interval_seconds: 7200, ad_interval_seconds: 1800 },
      usage: { join_requests: 11, ad_success: 35, ad_unknown: 0, ad_counted: 35 },
      execution: { join: { used: 11, remaining: 0, reason: 'account_daily_cap', next_allowed_at: '2026-11-13T00:00:00' },
        ads: { used: 35, remaining: 0, reason: 'account_daily_cap', next_allowed_at: '2026-11-12T16:00:00' } },
      account_reason: null,
    } } } })
    await choose(33)
    await wrapper.find('button').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('70 / 30 条/天')
    expect(wrapper.text()).toContain('35 / 0')
    expect(wrapper.text()).toContain('当日额度已用完')
    expect(wrapper.text()).toContain('加群每日 08:00 重置')
    await choose(34)
    expect(wrapper.text()).not.toContain('70 / 30 条/天')
    expect(requests.runtime).toHaveBeenCalledTimes(1)
  })

  it('does no automatic calls on mount, selection, list changes or elapsed time', async () => {
    vi.useFakeTimers()
    try {
      const { wrapper, choose } = setup()
      expect(wrapper.find('button').attributes('disabled')).toBeDefined()
      await choose(33)
      await wrapper.setProps({ accounts: [{ id: 33, identifier: 'updated' }] })
      await vi.advanceTimersByTimeAsync(120000)
      expect(requests.runtime).not.toHaveBeenCalled()
      wrapper.unmount()
    } finally { vi.useRealTimers() }
  })

  it('fetches only the selected account on manual refresh and preserves real zero', async () => {
    const { wrapper, choose } = setup()
    requests.runtime.mockResolvedValue(response({ available: true, ad_targets: 0, join_candidates: 2,
      join_candidates_preview_pending: 1, join_candidates_pending_identity: 0, sample_complete: true,
      blocker_counts: { account_auto_join_disabled: 1 },
      maintenance: { available: true, items: [{ id: 8, state: 'DONE' }] } }))
    await choose(33)
    await wrapper.find('button').trigger('click')
    await flushPromises()
    expect(requests.runtime).toHaveBeenCalledTimes(1)
    expect(requests.runtime).toHaveBeenCalledWith(33)
    expect(wrapper.findAll('.metric').find(row => row.text().includes('当前可执行广告目标'))?.text()).toBe('当前可执行广告目标 0')
    expect(wrapper.text()).toContain('经营维护记录：1 项')
    expect(wrapper.findAllComponents({ name: 'ElTable' }).map(row => row.props('data'))).toContainEqual([{ code: 'account_auto_join_disabled', count: 1 }])
  })

  it('prevents overlapping refreshes and discards the previous account response', async () => {
    const { wrapper, choose } = setup()
    let resolve!: (value: unknown) => void
    const pending = new Promise(value => { resolve = value })
    requests.runtime.mockReturnValueOnce(pending).mockResolvedValueOnce(response({ available: true, ad_targets: 4, join_candidates: 1, sample_complete: true }))
    await choose(33)
    await wrapper.find('button').trigger('click')
    await wrapper.find('button').trigger('click')
    await choose(34)
    resolve(response({ available: true, ad_targets: 99, join_candidates: 99, sample_complete: true }))
    await flushPromises()
    expect(requests.runtime).toHaveBeenCalledTimes(1)
    expect(requests.runtime).toHaveBeenCalledWith(33)
    expect(wrapper.findAll('.metric')).toHaveLength(0)
    await wrapper.find('button').trigger('click')
    await flushPromises()
    expect(requests.runtime.mock.calls).toEqual([[33], [34]])
    expect(wrapper.text()).not.toContain('99')
    expect(wrapper.findAll('.metric').find(row => row.text().includes('当前可执行广告目标'))?.text()).toBe('当前可执行广告目标 4')
  })

  it('keeps unavailable inventory and maintenance distinct from zero', async () => {
    const { wrapper, choose } = setup()
    requests.runtime.mockResolvedValue(response({ available: false, ad_targets: null, join_candidates: null,
      maintenance: { available: false, items: [] } }))
    await choose(33)
    await wrapper.find('button').trigger('click')
    await flushPromises()
    expect(wrapper.findAll('.metric').find(row => row.text().includes('当前可执行广告目标'))?.text()).toBe('当前可执行广告目标 未知')
    expect(wrapper.text()).toContain('不能视为零')
    expect(wrapper.text()).toContain('经营维护状态暂不可用')
  })
})
