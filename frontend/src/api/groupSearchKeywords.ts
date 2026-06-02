import apiClient from './client'

export interface GroupSearchKeyword {
  id: number
  text: string
  keyword_type: string
  status: string
  source: string
  match_mode: string
  trigger_count: number
  requires_review: boolean
  enabled: boolean
  created_at: string
  updated_at: string
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
    apiClient.post<{ data: { created: number; auto_approved: boolean; keywords: GroupSearchKeyword[] } }>('/group-search-keywords/generate', data),
}
