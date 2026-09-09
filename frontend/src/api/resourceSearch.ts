import apiClient from './client'

export type ResourceSearchStatus =
  | 'queued'
  | 'running'
  | 'completed'
  | 'partial'
  | 'failed'
  | 'cancelled'
export type ResourceReviewStatus = 'pending' | 'shortlisted' | 'confirmed' | 'rejected'

export interface ResourceSearchAccount {
  id: number
  identifier: string
  display_name?: string
  status: 'online' | 'idle'
  operation_mode: 'growth' | 'ad_only'
  session_available: boolean
}

export interface ResourceSearchAccountTask {
  id: number
  account_id?: number
  account_identifier: string
  status: ResourceSearchStatus
  keywords_completed: number
  successful_keywords: number
  result_count: number
  error?: string
  flood_wait_seconds?: number
  started_at?: string
  completed_at?: string
}

export interface ResourceSearchRun {
  id: number
  keywords: string[]
  account_ids: number[]
  max_results_per_keyword: number
  status: ResourceSearchStatus
  total_accounts: number
  completed_accounts: number
  successful_accounts: number
  failed_accounts: number
  raw_result_count: number
  unique_result_count: number
  error_summary?: string
  created_by_id?: number
  celery_task_id?: string
  heartbeat_at?: string
  cancel_requested_at?: string
  started_at?: string
  completed_at?: string
  created_at: string
  updated_at: string
  accounts?: ResourceSearchAccountTask[]
}

export interface DiscoveredByAccount {
  id: number
  identifier: string
}

export interface ResourceSearchResult {
  id: number
  run_id: number
  telegram_group_id?: number
  title: string
  username?: string
  invite_link?: string
  member_count: number
  is_private: boolean
  matched_keywords: string[]
  discovered_by_accounts: DiscoveredByAccount[]
  discovery_count: number
  review_status: ResourceReviewStatus
  note?: string
  first_found_at: string
  last_found_at: string
  reviewed_at?: string
  reviewed_by_id?: number
}

export interface ResourceSearchResultParams {
  page?: number
  page_size?: number
  search?: string
  keyword?: string
  review_status?: ResourceReviewStatus | ''
  min_members?: number
}

export const resourceSearchApi = {
  listAccounts: async (): Promise<ResourceSearchAccount[]> => {
    const response = await apiClient.get('/resource-search/accounts')
    return response.data?.data || []
  },

  createRun: async (data: {
    keywords: string[]
    account_ids: number[]
    max_results_per_keyword: number
  }): Promise<ResourceSearchRun> => {
    const response = await apiClient.post('/resource-search/runs', data)
    return response.data.data
  },

  listRuns: async (
    page = 1,
    pageSize = 20,
  ): Promise<{ list: ResourceSearchRun[]; total: number }> => {
    const response = await apiClient.get('/resource-search/runs', {
      params: { page, page_size: pageSize },
    })
    return {
      list: response.data?.data || [],
      total: Number(response.data?.total || 0),
    }
  },

  getRun: async (runId: number): Promise<ResourceSearchRun> => {
    const response = await apiClient.get(`/resource-search/runs/${runId}`)
    return response.data.data
  },

  cancelRun: async (runId: number): Promise<ResourceSearchRun> => {
    const response = await apiClient.post('/resource-search/runs/' + runId + '/cancel')
    return response.data.data
  },

  deleteRun: async (runId: number): Promise<void> => {
    await apiClient.delete('/resource-search/runs/' + runId)
  },

  listResults: async (
    runId: number,
    params: ResourceSearchResultParams,
  ): Promise<{ list: ResourceSearchResult[]; total: number }> => {
    const response = await apiClient.get(`/resource-search/runs/${runId}/results`, { params })
    return {
      list: response.data?.data || [],
      total: Number(response.data?.total || 0),
    }
  },

  updateResult: async (
    resultId: number,
    data: { review_status?: ResourceReviewStatus; note?: string },
  ): Promise<ResourceSearchResult> => {
    const response = await apiClient.patch(`/resource-search/results/${resultId}`, data)
    return response.data.data
  },
}
