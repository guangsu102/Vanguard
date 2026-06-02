import { defineStore } from 'pinia'
import { ref } from 'vue'
import { authApi, type LoginRequest, type UserInfo } from '@/api/auth'
import router from '@/router'

export const useAuthStore = defineStore('auth', () => {
  const token = ref<string | null>(localStorage.getItem('token'))
  const userInfo = ref<UserInfo | null>(null)
  const loading = ref(false)

  const setToken = (newToken: string) => {
    token.value = newToken
    localStorage.setItem('token', newToken)
  }

  const removeToken = () => {
    token.value = null
    localStorage.removeItem('token')
  }

  const login = async (data: LoginRequest) => {
    loading.value = true
    try {
      const res = await authApi.login(data)
      setToken(res.data.data.token)
      userInfo.value = res.data.data.user
      return res.data.data
    } finally {
      loading.value = false
    }
  }

  const logout = async () => {
    try {
      await authApi.logout()
    } catch {
      // ignore error
    } finally {
      removeToken()
      userInfo.value = null
      router.push('/login')
    }
  }

  const fetchUserInfo = async () => {
    if (!token.value) return null
    try {
      const res = await authApi.getUserInfo()
      userInfo.value = res.data.data
      return userInfo.value
    } catch {
      removeToken()
      router.push('/login')
      return null
    }
  }

  const isAuthenticated = () => {
    return !!token.value
  }

  return {
    token,
    userInfo,
    loading,
    login,
    logout,
    fetchUserInfo,
    isAuthenticated,
    setToken,
    removeToken,
  }
})
