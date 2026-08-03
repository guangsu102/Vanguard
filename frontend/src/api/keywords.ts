import apiClient from './client'

export type KeywordType = 'demand' | 'inquiry' | 'price' | 'competitor'
export type LegacyKeywordType = 'whitelist' | 'blacklist'
export type KeywordStatus = 'active' | 'inactive' | 'pending' | 'approved' | 'executing' | 'completed' | 'discarded'
export type MatchMode = 'exact' | 'contains' | 'fuzzy' | 'regex'

export const GROUP_SEARCH_KEYWORD_TYPE_OPTIONS = [
  { label: '行业人群词', value: 'demand' as KeywordType, tag: 'success' as const },
  { label: '平台生态词', value: 'inquiry' as KeywordType, tag: 'info' as const },
  { label: '场景痛点词', value: 'price' as KeywordType, tag: 'warning' as const },
  { label: '地区市场词', value: 'competitor' as KeywordType, tag: 'danger' as const },
]

export const DEFAULT_GROUP_SEARCH_KEYWORD_TYPES: KeywordType[] = GROUP_SEARCH_KEYWORD_TYPE_OPTIONS.map((item) => item.value)

export interface Keyword {
  id: number
  word: string
  text?: string
  type: KeywordType
  rawType?: KeywordType | LegacyKeywordType | string
  matchMode: MatchMode
  match_mode?: MatchMode
  description?: string
  hitCount: number
  trigger_count?: number
  status: KeywordStatus
  createdBy: string
  createdAt: string
  created_at?: string
  updatedAt: string
  updated_at?: string
}

export interface ModerationItem extends Keyword {
  source?: string
}

export interface KeywordListParams {
  page?: number
  pageSize?: number
  type?: KeywordType | ''
  status?: KeywordStatus | ''
  matchMode?: MatchMode | ''
  keyword?: string
}

export interface KeywordListResponse {
  list: Keyword[]
  total: number
  page: number
  pageSize: number
}

export interface KeywordFormData {
  word: string
  type: KeywordType
  matchMode: MatchMode
  description?: string
}

export interface AIGenerateParams {
  category: KeywordType
  count: number
}

const keywordTypeToApi = (type?: KeywordType | LegacyKeywordType | string) => {
  if (type === 'whitelist') return 'demand'
  if (type === 'blacklist') return 'competitor'
  return type || 'demand'
}

const matchModeToApi = (mode?: MatchMode | string) => {
  if (mode === 'contains') return 'fuzzy'
  return mode || 'fuzzy'
}

export const keywordsApi = {
  list: (params?: KeywordListParams & { page_size?: number }) => {
    const query = {
      ...params,
      page_size: params?.page_size ?? params?.pageSize,
      keyword_type: keywordTypeToApi(params?.type),
      status_filter: params?.status === 'active'
        ? 'approved'
        : params?.status === 'inactive'
          ? 'discarded'
          : params?.status,
    }
    delete (query as any).pageSize
    delete (query as any).type
    delete (query as any).status
    delete (query as any).matchMode
    return apiClient.get<{ data: KeywordListResponse }>('/keywords', { params: query })
  },

  create: (data: KeywordFormData) => {
    return apiClient.post<{ data: Keyword }>('/keywords', {
      text: data.word,
      type: keywordTypeToApi(data.type),
      match_mode: matchModeToApi(data.matchMode),
    })
  },

  update: (id: number, data: Partial<KeywordFormData>) => {
    return apiClient.put<{ data: Keyword }>(`/keywords/${id}`, {
      ...(data.word !== undefined ? { text: data.word } : {}),
      ...(data.type !== undefined ? { type: keywordTypeToApi(data.type) } : {}),
      ...(data.matchMode !== undefined ? { match_mode: matchModeToApi(data.matchMode) } : {}),
    })
  },

  delete: (id: number) => {
    return apiClient.delete(`/keywords/${id}`)
  },

  batchCreate: (data: { words: string[]; type: KeywordType; matchMode: MatchMode }) => {
    return apiClient.post<{ data: { success: number; failed: number } }>('/keywords/batch-add', {
      keywords: data.words,
      category: keywordTypeToApi(data.type),
      match_mode: matchModeToApi(data.matchMode),
    })
  },

  getModerationQueue: (params?: { page?: number; pageSize?: number }) => {
    return apiClient.get<{ data: { list: ModerationItem[]; total: number } }>('/keywords/moderation', {
      params: {
        page: params?.page,
        page_size: params?.pageSize,
      },
    })
  },

  approveKeyword: (id: number) => {
    return apiClient.post<{ data: Keyword }>(`/keywords/moderation/${id}/approve`)
  },

  rejectKeyword: (id: number) => {
    return apiClient.post(`/keywords/moderation/${id}/reject`)
  },

  generateByAI: (data: AIGenerateParams) => {
    return apiClient.post<{ data: { words?: string[]; keywords?: Array<{ text: string }> } }>('/keywords/generate', {
      category: keywordTypeToApi(data.category),
      count: data.count,
    })
  },

  import: (data: FormData) => {
    return apiClient.post<{ data: { success: number; failed: number } }>('/keywords/import', data, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
  },

  export: () => {
    return apiClient.get('/keywords/export', { responseType: 'blob' })
  },
}
