import { beforeEach, describe, expect, it, vi } from 'vitest'

const client = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
}))

vi.mock('./client', () => ({ default: client }))

import { isUserBlocked, isUserMuted, usersApi } from './users'

const serverUser = {
  id: 80,
  telegram_id: 1002,
  xboard_user_id: null,
  username: 'alice',
  state: 'pending',
  warning_count: 2,
  muted_until: null,
  trial_started_at: '2026-11-01T00:00:00',
  trial_expires_at: '2026-11-08T00:00:00',
  created_at: '2026-10-01T00:00:00',
  updated_at: '2026-11-10T00:00:00',
}

describe('users API', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('maps list params to the backend contract (page, keyword, state, date range)', async () => {
    client.get.mockResolvedValue({ data: { data: [serverUser], total: 1 } })

    const res = await usersApi.list({
      page: 2,
      pageSize: 20,
      keyword: 'QA_NO_MATCH',
      state: 'active',
      registeredFrom: '2026-01-01 00:00:00',
      registeredTo: '2026-01-31 23:59:59',
    })

    expect(client.get).toHaveBeenCalledWith('/users', {
      params: {
        page: 2,
        page_size: 20,
        search: 'QA_NO_MATCH',
        state_filter: 'active',
        registered_from: '2026-01-01 00:00:00',
        registered_to: '2026-01-31 23:59:59',
      },
    })

    const user = res.data.data.list[0]
    expect(user.telegramId).toBe('1002')
    expect(user.state).toBe('pending')
    expect(user.warningCount).toBe(2)
    expect(user.createdAt).toBe('2026-10-01T00:00:00')
    expect(res.data.data.total).toBe(1)
  })

  it('drops empty filters so unfiltered lists are not restricted', async () => {
    client.get.mockResolvedValue({ data: { data: [], total: 0 } })

    await usersApi.list({ page: 1, pageSize: 20, state: '', keyword: '' })

    expect(client.get).toHaveBeenCalledWith('/users', {
      params: { page: 1, page_size: 20 },
    })
  })

  it('wraps the bare backend detail payload and maps it to camelCase', async () => {
    client.get.mockResolvedValue({ data: serverUser })

    const res = await usersApi.getById(80)

    expect(client.get).toHaveBeenCalledWith('/users/80')
    expect(res.data.data.id).toBe(80)
    expect(res.data.data.telegramId).toBe('1002')
    expect(res.data.data.username).toBe('alice')
  })

  it('reads history from the violations endpoint and maps it to activities', async () => {
    client.get.mockResolvedValue({
      data: {
        data: [
          {
            id: 5,
            user_id: 80,
            group_id: 3,
            rule_type: 'spam',
            rule_pattern: '广告',
            content: '垃圾消息内容',
            action_taken: 'muted',
            action_duration: 24,
            created_at: '2026-11-10T08:00:00',
          },
        ],
        total: 1,
      },
    })

    const res = await usersApi.getActivities(80, { pageSize: 20 })

    expect(client.get).toHaveBeenCalledWith('/users/80/violations', { params: { limit: 20 } })
    const activity = res.data.data.list[0]
    expect(activity.type).toBe('spam')
    expect(activity.description).toBe('垃圾消息内容')
    expect(activity.timestamp).toBe('2026-11-10T08:00:00')
  })

  it('targets the real moderation endpoints for mute and blacklist', async () => {
    client.post.mockResolvedValue({ data: { code: 0, data: { muted_until: '2026-11-13T00:00:00' } } })

    await usersApi.mute(80, { durationHours: 24, reason: '手动禁言' })
    expect(client.post).toHaveBeenCalledWith('/users/80/mute', {
      duration_hours: 24,
      reason: '手动禁言',
    })

    await usersApi.blacklist(80)
    expect(client.post).toHaveBeenCalledWith('/users/80/block')

    await usersApi.removeBlacklist(80)
    expect(client.post).toHaveBeenCalledWith('/users/80/unblock')
  })

  it('derives muted/blocked display state from real fields', () => {
    expect(isUserBlocked({ state: 'blocked' })).toBe(true)
    expect(isUserBlocked({ state: 'active' })).toBe(false)

    expect(isUserMuted({ mutedUntil: new Date(Date.now() + 60_000).toISOString() })).toBe(true)
    expect(isUserMuted({ mutedUntil: new Date(Date.now() - 60_000).toISOString() })).toBe(false)
    expect(isUserMuted({ mutedUntil: null })).toBe(false)
  })
})
