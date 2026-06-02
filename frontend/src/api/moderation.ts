import apiClient from './client'

export type SuggestionStatus = 'pending' | 'approved' | 'rejected'

export interface ModerationSuggestion {
  id: number
  keyword: string
  category: string
  confidence: number
  source_sample: string
  status: SuggestionStatus
  created_at: string
}

export interface ModerationSuggestionStats {
  total: number
  pending: number
  approved: number
  rejected: number
  by_category: Record<string, { pending: number; approved: number; rejected: number }>
}

export interface ViolationRecord {
  id: number
  user_id: number
  group_id: number
  rule_type: string
  rule_pattern?: string
  content?: string
  action_taken: string
  action_duration?: number
  created_at: string
}

export const moderationApi = {
  listSuggestions: (params?: { page?: number; page_size?: number; status_filter?: SuggestionStatus | ''; category?: string }) =>
    apiClient.get<{ data: ModerationSuggestion[]; total: number }>('/moderation/suggestions', { params }),

  approveSuggestion: (id: number) =>
    apiClient.post(`/moderation/suggestions/${id}/approve`),

  rejectSuggestion: (id: number, reason?: string) =>
    apiClient.post(`/moderation/suggestions/${id}/reject`, null, { params: reason ? { reason } : undefined }),

  batchReview: (data: { suggestion_ids: number[]; action: 'approve' | 'reject' }) =>
    apiClient.post('/moderation/suggestions/batch-review', data),

  getStats: () =>
    apiClient.get<{ data: ModerationSuggestionStats }>('/moderation/stats'),

  listViolations: (params?: { page?: number; page_size?: number; group_id?: number; rule_type?: string }) =>
    apiClient.get<{ data: ViolationRecord[]; total: number }>('/moderation/violations', { params }),

  generateSuggestions: (data: { samples: string[]; category?: string; match_mode?: string }) =>
    apiClient.post('/moderation/suggestions/generate', data),
}
