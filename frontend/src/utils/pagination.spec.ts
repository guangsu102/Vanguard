import { describe, expect, it } from 'vitest'

import {
  DEFAULT_PAGE_SIZE,
  MAX_PAGE_SIZE,
  normalizePageSize,
  normalizePageSizeOptions,
  PAGE_SIZE_OPTIONS,
} from './pagination'

describe('pagination settings', () => {
  it('defines the shared default and maximum page sizes', () => {
    expect(DEFAULT_PAGE_SIZE).toBe(20)
    expect(MAX_PAGE_SIZE).toBe(100)
    expect(PAGE_SIZE_OPTIONS).toEqual([10, 20, 50, 100])
  })

  it('normalizes page sizes and configurable options', () => {
    expect(normalizePageSize(undefined)).toBe(20)
    expect(normalizePageSize(101)).toBe(100)
    expect(normalizePageSize(50.8)).toBe(50)
    expect(normalizePageSizeOptions([100, 20, 20, 200, 50])).toEqual([20, 50, 100])
  })
})
