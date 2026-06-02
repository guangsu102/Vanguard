import { beforeEach, describe, expect, it, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { useAuthStore } from './auth'

const loginMock = vi.fn()
const logoutMock = vi.fn()
const getUserInfoMock = vi.fn()

vi.mock('@/api/auth', () => ({
  authApi: {
    login: (...args: any[]) => loginMock(...args),
    logout: (...args: any[]) => logoutMock(...args),
    getUserInfo: (...args: any[]) => getUserInfoMock(...args),
  },
}))

vi.mock('@/router', () => ({
  default: { push: vi.fn() },
}))

describe('useAuthStore', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    localStorage.clear()
    loginMock.mockReset()
    logoutMock.mockReset()
    getUserInfoMock.mockReset()
  })

  it('stores token after login', async () => {
    loginMock.mockResolvedValue({
      data: {
        data: {
          token: 'token-123',
          user: { id: 1, username: 'admin', role: 'admin' },
        },
      },
    })

    const store = useAuthStore()
    await store.login({ username: 'admin', password: 'secret' })

    expect(store.token).toBe('token-123')
    expect(localStorage.getItem('token')).toBe('token-123')
  })

  it('fetches user info when token exists', async () => {
    localStorage.setItem('token', 'token-123')
    getUserInfoMock.mockResolvedValue({
      data: {
        data: {
          id: 1,
          username: 'admin',
          role: 'admin',
          createdAt: '2026-05-24T00:00:00Z',
        },
      },
    })

    const store = useAuthStore()
    const user = await store.fetchUserInfo()

    expect(user?.username).toBe('admin')
  })
})
