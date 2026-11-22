import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { accountsApi, type Account } from '@/api/accounts'
import { useAccountStore } from './account'

describe('account recovery connection controls', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.restoreAllMocks()
  })

  it('preserves ERROR in the list when stopping an abnormal account', async () => {
    const store = useAccountStore()
    store.list = [{ id: 135, status: 'error', is_active: true } as Account]
    vi.spyOn(accountsApi, 'disconnect').mockResolvedValue({ account_id: 135, status: 'error', is_active: false })
    await store.disable(135)
    expect(store.list[0].status).toBe('error')
    expect(store.list[0].is_active).toBe(false)
  })

  it('keeps a failed recovery unchanged instead of optimistically publishing ONLINE', async () => {
    const store = useAccountStore()
    store.list = [{ id: 135, status: 'error', is_active: true } as Account]
    vi.spyOn(accountsApi, 'connect').mockRejectedValue(new Error('identity_mismatch'))
    await expect(store.enable(135)).rejects.toThrow('identity_mismatch')
    expect(store.list[0].status).toBe('error')
  })
})
