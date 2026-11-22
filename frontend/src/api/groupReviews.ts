import apiClient from './client'

export interface QualificationAuthorization {
  state: 'active' | 'invalidated' | 'missing' | 'unavailable' | string
  grant_audit_id?: number | null
  version?: number
  invalidated_reason?: string | null
}

export interface QualificationJob {
  id: number
  state: string
  kind: string
  ready_at?: string | null
  next_step_reads?: number | null
  missing_dependencies?: string[]
}

export interface QualificationBlocker {
  code: string
  required_reads?: number | null
  available_reads?: number | null
  retry_at?: string | null
  observed_at?: string | null
}

export interface GroupReview {
  id: number
  account_id: number
  group_id: number
  membership_id?: number
  state: string
  decision: string
  reason: string
  checked_at: string | null
  expires_at: string | null
  evidence: Record<string, any>
  wait?: Record<string, any>
  read_status?: Record<string, any>
  authorization?: QualificationAuthorization | null
  job?: QualificationJob | null
  blocker?: QualificationBlocker | null
}

export interface GrowthBusinessMetrics {
  available: boolean
  reason?: string
  window_start?: string
  window_end?: string
  ads?: { attempted: number; succeeded: number; unknown: number }
  readiness?: {
    available: boolean
    reason?: string
    ad_targets?: number | null
    join_candidates?: number | null
    join_candidates_total?: number | null
    join_candidates_preview_pending?: number | null
    join_candidates_pending_identity?: number | null
    sample_complete?: boolean
    blocker_counts?: Record<string, number> | null
    maintenance?: {
      available: boolean
      reason?: string
      truncated?: boolean
      blocks_qualification?: boolean
      retry_is_prediction?: boolean
      items: { id: number; group_id: number; state: string; reason?: string | null; ready_at?: string | null; next_step_reads?: number | null }[]
    }
  }
  ad_opportunities?: {
    available: boolean
    complete: boolean
    incomplete_reasons: string[]
    due: number
    completed: number
    pending: number
    completion_rate: number | null
    pending_before_window: number
    pending_by_reason: Record<string, number>
  }
  joins?: {
    intent: number | null
    sdk_entered: number | null
    platform_responded: number | null
    membership_confirmed: number | null
    unknown: number | null
    sdk_entry_unrecorded?: number | null
    platform_response_unrecorded?: number | null
  }
}

export interface GrowthExecutionControls {
  available: boolean
  configured: { join_daily: number; ad_daily: number; total_daily: number | null }
  effective: { join_daily: number; ad_daily: number; total_daily: number; join_interval_seconds: number; ad_interval_seconds: number }
  usage: { join_requests: number; ad_success: number; ad_unknown: number; ad_counted: number }
  execution?: {
    join?: { used: number; remaining: number; reason: string | null; next_allowed_at: string | null }
    ads?: { used: number; remaining: number; reason: string | null; next_allowed_at: string | null }
    available?: boolean
    reason?: string
  }
  reset_at: { join: string; ads: string }
  account_reason: string | null
  diagnostic_pause?: { reason: string; at: string } | null
  spam_check: { status: string; checked_at: string | null }
}

export interface GroupReviewRuntime extends Record<string, any> {
  controls?: GrowthExecutionControls | null
  business?: GrowthBusinessMetrics
  current_work?: {
    available: boolean
    truncated?: boolean
    items: {
      membership_id: number
      account_id: number
      group_id: number
      authorization?: QualificationAuthorization | null
      job?: QualificationJob | null
      blocker?: QualificationBlocker | null
    }[]
  }
}

export function listGroupReviews(accountId?: number) {
  return apiClient.get<{ code: number; data: GroupReview[] }>('/automation/group-reviews', {
    params: accountId ? { account_id: accountId } : {},
  })
}

export function groupReviewRuntime(accountId?: number) {
  return apiClient.get<{ code: number; data: GroupReviewRuntime }>('/automation/group-reviews/runtime', {
    params: accountId ? { account_id: accountId } : {},
  })
}

export interface MembershipPermission {
  ad_failure_streak?: number
  last_send_outcome?: string | null
  last_send_error?: string | null
  leave_state?: string
  leave_reason?: string | null
  leave_attempts?: number
  telegram_group_id?: number
  id: number
  membership_id: number
  account_id: number
  group_id: number
  group_title?: string | null
  status: string
  review_status: 'pending' | 'approved' | 'rejected'
  verification_status: string
  can_send_text: boolean | null
  reason?: string | null
  checked_at?: string | null
  next_review_at?: string | null
  next_ad_allowed_at?: string | null
  ad_status: string
}
