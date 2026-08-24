import { describe, expect, it } from 'vitest'

import { formatAdTargetGroupLevels } from './adCampaign'

describe('formatAdTargetGroupLevels', () => {
  it('sorts known levels and replaces internal values with user-facing labels', () => {
    expect(formatAdTargetGroupLevels(['B', 'unrated', 'A'])).toBe('A级 / B级 / 未评级')
  })

  it('removes duplicate and blank values', () => {
    expect(formatAdTargetGroupLevels([' B ', '', 'B', 'A'])).toBe('A级 / B级')
  })

  it('keeps unknown levels after known levels', () => {
    expect(formatAdTargetGroupLevels(['vip', 'C'])).toBe('C级 / vip')
  })

  it('shows an explicit empty-state label', () => {
    expect(formatAdTargetGroupLevels(undefined)).toBe('未设置')
    expect(formatAdTargetGroupLevels([])).toBe('未设置')
  })
})
