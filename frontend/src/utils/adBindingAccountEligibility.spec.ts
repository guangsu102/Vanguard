import { describe, expect, it } from 'vitest'

import { filterAccountsByDeliveryPolicy } from './adBindingAccountEligibility'

const accounts = [{ id: 1 }, { id: 2 }, { id: 3 }]
const accountModes = new Map<number, 'growth' | 'ad_only'>([
  [1, 'growth'],
  [2, 'ad_only'],
])

describe('filterAccountsByDeliveryPolicy', () => {
  it('shows only explicitly configured Ad-only accounts for Ad-only plans', () => {
    expect(filterAccountsByDeliveryPolicy(accounts, accountModes, 'ad_only')).toEqual([
      { id: 2 },
    ])
  })

  it('treats accounts without operation config as Growth accounts', () => {
    expect(filterAccountsByDeliveryPolicy(accounts, accountModes, 'growth')).toEqual([
      { id: 1 },
      { id: 3 },
    ])
  })

  it('returns no accounts until a plan is selected', () => {
    expect(filterAccountsByDeliveryPolicy(accounts, accountModes)).toEqual([])
  })
})
