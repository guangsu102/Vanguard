import apiClient from './client'

export interface LoginRequest {
  username: string
  password: string
}

export interface LoginResponse {
  token: string
  user: {
    id: number
    username: string
    role: string
    avatar?: string
  }
}

export interface UserInfo {
  id: number
  username: string
  role: string
  email?: string
  avatar?: string
  createdAt: string
}

export const authApi = {
  login: (data: LoginRequest) => {
    return apiClient.post<{ data: LoginResponse }>('/auth/login', data)
  },

  logout: () => {
    return apiClient.post('/auth/logout')
  },

  getUserInfo: () => {
    return apiClient.get<{ data: UserInfo }>('/auth/user')
  },

  updatePassword: (data: { oldPassword: string; newPassword: string }) => {
    return apiClient.put('/auth/password', data)
  },
}
