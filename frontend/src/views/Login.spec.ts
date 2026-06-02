import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import Login from './Login.vue'

const login = vi.fn().mockResolvedValue({})
const push = vi.fn()

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({
    login,
  }),
}))

vi.mock('vue-router', () => ({
  useRouter: () => ({ push }),
}))

describe('Login view', () => {
  beforeEach(() => {
    login.mockClear()
    push.mockClear()
  })

  it('renders login form', () => {
    const wrapper = mount(Login, {
      global: {
        stubs: ['el-form', 'el-form-item', 'el-input', 'el-button', 'el-checkbox', 'el-icon'],
      },
    })

    expect(wrapper.text()).toContain('Vanguard')
    expect(wrapper.text()).toContain('Telegram Bot 管理平台')
  })

  it('submits login and navigates to dashboard when validation passes', async () => {
    const wrapper = mount(Login, {
      global: {
        stubs: ['el-form', 'el-form-item', 'el-input', 'el-button', 'el-checkbox', 'el-icon'],
      },
    })

    const vm = wrapper.vm as any
    vm.loginFormRef = {
      validate: vi.fn().mockResolvedValue(true),
    }
    vm.loginForm.username = 'admin'
    vm.loginForm.password = 'secret'

    await vm.handleLogin()

    expect(login).toHaveBeenCalledWith({ username: 'admin', password: 'secret' })
    expect(push).toHaveBeenCalledWith('/dashboard')
  })

  it('does not login when validation fails', async () => {
    const wrapper = mount(Login, {
      global: {
        stubs: ['el-form', 'el-form-item', 'el-input', 'el-button', 'el-checkbox', 'el-icon'],
      },
    })

    const vm = wrapper.vm as any
    vm.loginFormRef = {
      validate: vi.fn().mockRejectedValue(new Error('validation failed')),
    }

    await vm.handleLogin()

    expect(login).not.toHaveBeenCalled()
    expect(push).not.toHaveBeenCalled()
  })

  it('toggles password visibility state', () => {
    const wrapper = mount(Login, {
      global: {
        stubs: ['el-form', 'el-form-item', 'el-input', 'el-button', 'el-checkbox', 'el-icon'],
      },
    })

    const vm = wrapper.vm as any
    expect(vm.showPassword).toBe(false)
    vm.showPassword = true
    expect(vm.showPassword).toBe(true)
  })
})
