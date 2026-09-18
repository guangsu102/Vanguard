import { beforeEach, describe, expect, it, vi } from 'vitest'

import apiClient from '@/api/client'
import {
  cancelAccountProfileUpdateOperation,
  createAccountProfileUpdateOperation,
  getAccountProfileUpdateOperation,
  getLatestAccountProfileUpdateOperation,
  type AccountProfileUpdateOperation,
} from '@/api/accountProfileUpdates'

vi.mock('@/api/client', () => ({
  default: {
    get: vi.fn(),
    post: vi.fn(),
  },
}))

const operation: AccountProfileUpdateOperation = {
  id: 41,
  status: 'running',
  profile_bio: '广告账号公开简介',
  total_accounts: 3,
  processed_accounts: 1,
  succeeded_accounts: 1,
  failed_accounts: 0,
  cancelled_accounts: 0,
  skipped_accounts: 0,
  max_attempts: 3,
  last_error: null,
  created_at: '2026-09-15T02:00:00Z',
  items: [
    {
      id: 8,
      account_id: 11,
      status: 'succeeded',
      attempts: 1,
      reason_code: null,
      error_message: null,
    },
  ],
}

describe('accountProfileUpdatesApi', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('creates a durable batch with an Idempotency-Key instead of direct Telegram calls', async () => {
    vi.mocked(apiClient.post).mockResolvedValue({ data: { data: operation } })

    await expect(createAccountProfileUpdateOperation(
      { account_ids: [11, 12, 13], profile_bio: '广告账号公开简介' },
      'profile-update-key',
    )).resolves.toEqual(operation)

    expect(apiClient.post).toHaveBeenCalledWith(
      '/accounts/profile-updates/operations',
      { account_ids: [11, 12, 13], profile_bio: '广告账号公开简介' },
      { headers: { 'Idempotency-Key': 'profile-update-key' } },
    )
  })

  it('loads the latest operation and preserves an empty queue result', async () => {
    vi.mocked(apiClient.get).mockResolvedValueOnce({ data: { data: operation } })
    vi.mocked(apiClient.get).mockResolvedValueOnce({ data: { data: null } })

    await expect(getLatestAccountProfileUpdateOperation()).resolves.toEqual(operation)
    await expect(getLatestAccountProfileUpdateOperation()).resolves.toBeNull()
    expect(apiClient.get).toHaveBeenNthCalledWith(1, '/accounts/profile-updates/operations/latest')
  })

  it('loads one operation and requests cancellation through the queue API', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({ data: operation })
    vi.mocked(apiClient.post).mockResolvedValue({
      data: { data: { ...operation, status: 'cancelling' } },
    })

    await expect(getAccountProfileUpdateOperation(41)).resolves.toEqual(operation)
    await expect(cancelAccountProfileUpdateOperation(41)).resolves.toMatchObject({
      id: 41,
      status: 'cancelling',
    })
    expect(apiClient.get).toHaveBeenCalledWith('/accounts/profile-updates/operations/41')
    expect(apiClient.post).toHaveBeenCalledWith('/accounts/profile-updates/operations/41/cancel')
  })
})
