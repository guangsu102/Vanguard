import { describe, expect, it, vi } from 'vitest'
import { shallowMount } from '@vue/test-utils'
import GroupQualificationPanel from './GroupQualificationPanel.vue'

vi.mock('@/api/client', () => ({ default: { get: vi.fn(async () => ({ data: { data: [] } })) } }))

function panel() {
  const wrapper = shallowMount(GroupQualificationPanel, {
    global: {
      stubs: Object.fromEntries([
        'el-card', 'el-select', 'el-option', 'el-button', 'el-alert', 'el-descriptions',
        'el-descriptions-item', 'el-table', 'el-table-column', 'el-drawer',
      ].map(name => [name, { template: name === 'el-table-column' ? '<div />' : '<div><slot /></div>' }])),
      directives: { loading: {} },
    },
  })
  return wrapper.vm as unknown as {
    formatTime: (value: unknown) => string
    ordinaryAdsVerified: (value: unknown) => boolean
    metricValue: (value: unknown, available: boolean) => string | number
    effectiveUnknowns: (row: any) => string[]
    authorizationLabel: (row: any) => string
    stageLabel: (row: any) => string
    blockerLabel: (row: any) => string
    recordedCount: (value: unknown) => string | number
  }
}

describe('group qualification evidence display', () => {
  it('keeps an active grant visible while its maintenance job waits', () => {
    const view = panel()
    const row = { state: 'queued', authorization: { state: 'active', grant_audit_id: 5 },
      job: { state: 'WAIT_BUDGET' }, blocker: { code: 'telegram_read_budget', required_reads: 1 } }
    expect(view.authorizationLabel(row)).toBe('授权有效')
    expect(view.stageLabel(row)).toBe('等待读取额度')
    expect(view.blockerLabel(row)).toBe('等待读取额度')
    expect(view.authorizationLabel({ ...row, authorization: { state: 'invalidated' } })).toBe('授权已失效')
  })

  it('does not infer historical SDK entry from join intent or missing data', () => {
    const view = panel()
    expect(view.recordedCount(null)).toBe('未记录')
    expect(view.recordedCount(undefined)).toBe('未记录')
    expect(view.recordedCount(0)).toBe(0)
    expect(view.recordedCount(12)).toBe(12)
  })

  it('shows an unavailable runtime as unknown instead of completed', () => {
    const view = panel()
    const row = { authorization: { state: 'unavailable' }, blocker: { code: 'runtime_unavailable' } }
    expect(view.authorizationLabel(row)).toBe('授权状态暂不可用')
    expect(view.blockerLabel(row)).toBe('进度暂不可用')
  })
  it('shows current pending gaps instead of stale completed material', () => {
    const view = panel()
    const row = { evidence: { root_ad_history_sufficient: true, unknowns: [],
      pending_collection: { unknowns: ['identity_incomplete'] } } }
    expect(view.effectiveUnknowns(row)).toEqual(['identity_incomplete'])
    expect(view.ordinaryAdsVerified(row)).toBe(false)
    expect(view.effectiveUnknowns({ ...row, read_status: { effective_unknowns: ['history_gap'] } })).toEqual(['history_gap'])
  })
  it('never presents missing or incomplete measurements as zero', () => {
    const view = panel()
    expect(view.metricValue(0, false)).toBe('暂无完整计量')
    expect(view.metricValue(null, true)).toBe('暂无完整计量')
    expect(view.metricValue(0, true)).toBe(0)
    expect(view.metricValue(12, true)).toBe(12)
  })
  it('treats naive server ISO as UTC and displays an explicit Shanghai timezone', () => {
    const view = panel()
    expect(view.formatTime('2026-11-03T01:02:03')).toBe('2026/11/03 09:02:03（北京时间 UTC+8）')
    expect(view.formatTime('2026-11-03T01:02:03Z')).toBe(view.formatTime('2026-11-03T09:02:03+08:00'))
    expect(view.formatTime('2026-11-03T17:02:03.123456')).toContain('2026/11/04 01:02:03')
    expect(view.formatTime('not a date')).toBe('时间格式无效')
    expect(view.formatTime(null)).toBe('未知')
  })

  it('recognizes sufficient fast-path proof only when its dependencies are complete', () => {
    const view = panel()
    const evidence = { sampling_stopped_reason: 'sufficient_ordinary_root_trial_proof' }
    expect(view.ordinaryAdsVerified({ evidence })).toBe(false)
    expect(view.ordinaryAdsVerified({ evidence: { ...evidence, decision_dependencies_complete: true } })).toBe(true)
    expect(view.ordinaryAdsVerified({ evidence: { decision_dependencies_complete: true } })).toBe(false)
    expect(view.ordinaryAdsVerified({ evidence: { root_ad_history_sufficient: true } })).toBe(true)
  })
})
