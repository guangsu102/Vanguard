import apiClient from './client'

export interface GroupSearchKeyword {
  id: number
  text: string
  keyword_type: string
  status: string
  source: string
  match_mode: string
  trigger_count: number
  use_count: number
  used_at: string | null
  requires_review: boolean
  enabled: boolean
  created_at: string
  updated_at: string
}

export interface GenerateGroupSearchKeywordsResult {
  requested: number
  generated: number
  attempts: number
  created: number
  skipped_existing: number
  skipped_duplicate: number
  skipped_invalid: number
  skipped_invalid_reasons: Record<string, number>
  skipped_empty: number
  skipped_existing_keywords: string[]
  skipped_duplicate_keywords: string[]
  skipped_invalid_keywords: string[]
  candidate_exhausted: boolean
  llm_configured: boolean
  auto_approved: boolean
  keywords: GroupSearchKeyword[]
}

export const groupSearchKeywordsApi = {
  list: (params?: { keyword_type?: string; status?: string; enabled?: boolean; keyword?: string; page?: number; page_size?: number }) =>
    apiClient.get<{ data: GroupSearchKeyword[]; total: number }>('/group-search-keywords', { params }),

  create: (data: Record<string, any>) =>
    apiClient.post<{ data: GroupSearchKeyword }>('/group-search-keywords', data),

  update: (id: number, data: Record<string, any>) =>
    apiClient.put<{ data: GroupSearchKeyword }>(`/group-search-keywords/${id}`, data),

  remove: (id: number) =>
    apiClient.delete(`/group-search-keywords/${id}`),

  generate: (data: { keyword_type: string; count: number; auto_approve?: boolean }) =>
    apiClient.post<{ data: GenerateGroupSearchKeywordsResult }>('/group-search-keywords/generate', data),
}
