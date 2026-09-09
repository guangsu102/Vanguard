import { beforeEach, describe, expect, it, vi } from 'vitest'

const { get } = vi.hoisted(() => ({ get: vi.fn() }))

vi.mock('./client', () => ({
  default: { get },
}))

import { automationApi } from './automation'

describe('automationApi.getGroupAdProfiles', () => {
  beforeEach(() => {
    get.mockReset()
  })

  it('requests the complete supported group profile list by default', () => {
    automationApi.getGroupAdProfiles()

    expect(get).toHaveBeenCalledWith('/automation/ads/group-profiles', {
      params: { limit: 300 },
    })
  })

  it('keeps explicit filters and limit overrides', () => {
    automationApi.getGroupAdProfiles({ policy_mode: 'unknown', limit: 50 })

    expect(get).toHaveBeenCalledWith('/automation/ads/group-profiles', {
      params: { limit: 50, policy_mode: 'unknown' },
    })
  })
})
