import { beforeEach, describe, expect, it, vi } from 'vitest'

import apiClient from '@/api/client'
import {
  cancelSpamCheckOperation,
  createSpamCheckOperation,
  getLatestSpamCheckOperation,
  getSpamCheckOperation,
  type SpamCheckOperation,
} from '@/api/accountSpamChecks'

vi.mock('@/api/client', () => ({
  default: {
    get: vi.fn(),
    post: vi.fn(),
  },
}))

const operation: SpamCheckOperation = {
  id: 23,
  status: 'running',
  total_accounts: 3,
  processed_accounts: 1,
  clear_accounts: 1,
  restricted_accounts: 0,
  failed_accounts: 0,
  cancelled_accounts: 0,
  last_error: null,
  created_at: '2026-09-13T01:00:00Z',
  started_at: '2026-09-13T01:00:01Z',
  items: [
    {
      id: 201,
      operation_id: 23,
      account_id: 11,
      status: 'succeeded',
      result: 'clear',
      attempts: 1,
      checked_at: '2026-09-13T01:00:02Z',
    },
  ],
}

describe('accountSpamChecksApi', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('creates a check operation with a stable Idempotency-Key', async () => {
    vi.mocked(apiClient.post).mockResolvedValue({ data: { data: operation } })

    await expect(createSpamCheckOperation({ account_ids: [11, 12, 13] }, 'spam-key')).resolves.toEqual(operation)
    expect(apiClient.post).toHaveBeenCalledWith(
      '/accounts/spam-check/operations',
      { account_ids: [11, 12, 13] },
      { headers: { 'Idempotency-Key': 'spam-key' } },
    )
  })

  it('loads the latest operation and supports an empty result', async () => {
    vi.mocked(apiClient.get).mockResolvedValueOnce({ data: { data: operation } })
    vi.mocked(apiClient.get).mockResolvedValueOnce({ data: { data: null } })

    await expect(getLatestSpamCheckOperation()).resolves.toEqual(operation)
    await expect(getLatestSpamCheckOperation()).resolves.toBeNull()
    expect(apiClient.get).toHaveBeenNthCalledWith(1, '/accounts/spam-check/operations/latest')
  })

  it('loads one operation from an unwrapped response', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({ data: operation })

    await expect(getSpamCheckOperation(23)).resolves.toEqual(operation)
    expect(apiClient.get).toHaveBeenCalledWith('/accounts/spam-check/operations/23')
  })

  it('cancels an operation', async () => {
    const cancelled = { ...operation, status: 'cancelled' as const }
    vi.mocked(apiClient.post).mockResolvedValue({ data: { data: cancelled } })

    await expect(cancelSpamCheckOperation(23)).resolves.toEqual(cancelled)
    expect(apiClient.post).toHaveBeenCalledWith('/accounts/spam-check/operations/23/cancel')
  })
})