import apiClient from './client'

export interface VerificationSession {
  id: number
  session_id: string
  user_id: number
  chat_id: number
  verify_type: 'captcha' | 'question'
  state: 'pending' | 'passed' | 'failed' | 'expired'
  question?: string
  attempt_count: number
  max_attempts: number
  expires_at: string
  created_at: string
}

export interface VerificationConfig {
  id: number
  group_id: number
  enable_verification: boolean
  verification_type: 'captcha' | 'question'
  questions?: Array<{ question: string; answer: string }>
  welcome_message?: string
  timeout_minutes: number
  whitelist_bypass: boolean
  updated_at: string
}

export interface VerificationCreateParams {
  user_id: number
  chat_id: number
  verify_type: 'captcha' | 'question'
  question?: string
  answer?: string
}

export interface VerificationVerifyParams {
  session_id: string
  captcha_code?: string
  answer?: string
}

export const verificationApi = {
  create: (data: VerificationCreateParams) => {
    return apiClient.post<{ data: VerificationSession }>('/verification', data)
  },

  verify: (data: VerificationVerifyParams) => {
    return apiClient.post<{ data: { state: string; remaining_attempts?: number } }>('/verification/verify', data)
  },

  get: (sessionId: string) => {
    return apiClient.get<{ data: VerificationSession }>(`/verification/${sessionId}`)
  },

  getUserVerifications: (userId: number) => {
    return apiClient.get<{ data: VerificationSession[] }>(`/verification/user/${userId}`)
  },

  getConfig: (groupId: number) => {
    return apiClient.get<{ data: VerificationConfig }>(`/verification/config/${groupId}`)
  },

  createOrUpdateConfig: (data: Omit<VerificationConfig, 'id' | 'updated_at'>) => {
    return apiClient.post<{ data: VerificationConfig }>('/verification/config', data)
  },

  deleteConfig: (groupId: number) => {
    return apiClient.delete(`/verification/config/${groupId}`)
  },
}
