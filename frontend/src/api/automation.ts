import apiClient from './client'

export interface AutomationRunResult {
  queued?: boolean
  status?: string
  task_name?: string
  task_id?: string
  payload?: Record<string, any>
  processed: number
  created: number
  updated: number
  succeeded: number
  skipped: number
  failed: number
  errors: string[]
  details: Record<string, any>[]
}

export type GroupFailoverStatus =
  | 'queued'
  | 'joining'
  | 'retry'
  | 'succeeded'
  | 'manual_required'
  | 'failed'
  | 'cancelled'

export interface GroupFailoverTask {
  id: number
  source_membership_id: number
  source_account_id: number
  source_account_label?: string
  target_account_id?: number
  target_account_label?: string
  group_id: number
  telegram_group_id: number
  group_title?: string
  group_username?: string
  status: GroupFailoverStatus
  reason?: string
  error?: string
  attempt_count: number
  next_retry_at?: string
  last_attempt_at?: string
  completed_at?: string
  created_at: string
  updated_at: string
}


export type AccountOperationMode = 'growth' | 'ad_only'

export interface AccountOperationConfig {
  id: number
  account_id: number
  operation_mode: AccountOperationMode
  auto_join_enabled: boolean
  auto_ads_enabled: boolean
  max_groups_per_day: number
  max_groups_total: number
  join_interval_min_seconds: number
  join_interval_max_seconds: number
  next_join_after?: string
  max_messages_per_day: number
  message_interval_seconds: number
  quiet_hours_start?: string
  quiet_hours_end?: string
  keyword_types: string[]
  keyword_auto_replenish_enabled: boolean
  keyword_replenish_requires_review: boolean
  risk_level: string
  business_stage: 'new' | 'normal' | 'hot' | 'cooldown' | string
  enabled: boolean
}

export type AccountOperationConfigUpdatePayload = Partial<
  Omit<AccountOperationConfig, 'id' | 'account_id' | 'risk_level' | 'business_stage'>
>

export interface AutoJoinSchedulerConfig {
  enabled: boolean
  scan_interval_minutes: number
  search_filter?: {
    title_blacklist_enabled: boolean
    title_blacklist: string[]
  }
  join_verification?: {
    enabled: boolean
    ai_enabled: boolean
    confidence_threshold: number
    post_action_wait_seconds: number
    post_action_recheck_attempts: number
    post_action_extra_wait_seconds: number
    message_limit: number
    ai_timeout_seconds: number
    action_timeout_seconds: number
    pending_sync_min_age_seconds: number
    pending_sync_limit: number
    unknown_challenge_action: 'leave' | 'manual' | 'wait' | 'skip'
    allow_button_clicks: boolean
    allow_text_answers: boolean
    answer_profile: string
  }
  group_capacity_cleanup?: {
    enabled: boolean
    no_conversion_days: number
    min_join_age_days: number
    max_cleanup_per_run: number
  }
}

export interface AutoJoinVerificationLog {
  id: number
  account_id: number
  group_id: number
  telegram_group_id: number
  group_username?: string
  group_title?: string
  membership_status: string
  audit_passed: boolean
  audit_reason?: string
  action: string
  source: 'ai' | 'local' | 'fallback' | 'unknown'
  challenge_type: string
  success?: boolean
  reason?: string
  error?: string
  confidence?: number
  decision_reason?: string
  button_text?: string
  answer?: string
  target_message_id?: number
  post_action_status?: string
  post_action_rechecks?: Array<{
    attempt: number
    can_send_messages?: boolean
    permission_reason?: string
    message_count?: number
    text_messages?: number
    chinese_messages?: number
    chinese_message_ratio?: number
    unique_senders?: number
  }>
  post_action_final_can_send?: boolean
  post_action_final_permission_reason?: string
  should_retry_audit: boolean
  should_leave: boolean
  joined_at?: string
  updated_at: string
}

export interface AdCreative {
  id: number
  name: string
  content: string
  creative_type: 'text' | 'image' | 'mixed'
  media_url?: string
  link_url?: string
  weight: number
  enabled: boolean
  created_at: string
  updated_at: string
}

export interface AdCampaign {
  id: number
  name: string
  enabled: boolean
  status: string
  send_mode: 'after_join' | 'interval' | 'scheduled'
  target_group_levels: string[]
  target_group_ids: number[]
  start_at?: string
  end_at?: string
  min_wait_after_join_minutes: number
  interval_minutes: number
  scheduled_times: string[]
  max_sends_per_group_per_day: number
  max_sends_per_account_per_day: number
  created_at?: string
  updated_at?: string
}

export interface AdCampaignCreatePayload {
  name: string
  enabled?: boolean
  status?: string
  send_mode?: 'after_join' | 'interval' | 'scheduled'
  target_group_levels?: string[]
  target_group_ids?: number[]
  start_at?: string
  end_at?: string
  min_wait_after_join_minutes?: number
  interval_minutes?: number
  scheduled_times?: string[]
  max_sends_per_group_per_day?: number
  max_sends_per_account_per_day?: number
}

export interface AdFailurePolicy {
  enabled: boolean
  leave_on_group_control_failure: boolean
  group_control_failure_limit: number
  group_control_failure_window_hours: number
  levels: string[]
}

export interface AccountRiskActionBudget {
  daily_limit: number
  cooldown_seconds: number
}

export interface AccountRiskGuardSettings {
  enabled: boolean
  global_daily_limit: number
  group_write_daily_limit: number
  redis_fail_closed: boolean | null
  actions: Record<string, AccountRiskActionBudget>
  level_thresholds: Record<string, number>
  level_budget_multipliers: Record<string, number>
  risk_score_deltas: Record<string, number>
  lifecycle: Record<string, number>
  group_write_forbidden: Record<string, number>
  retention: Record<string, number>
}

export interface AccountWarmupTierPolicy {
  warmup_days: number
}

export interface AccountAssetTierPolicy {
  join_multiplier: number
  ad_multiplier: number
  run_multiplier: number
  probe_multiplier: number
  warmup_days: number
  age_floor_days: number
}

export interface AccountAssetPolicySettings {
  enabled: boolean
  tiers: Record<string, AccountAssetTierPolicy>
}

export interface AccountWarmupStagePolicy {
  limit_multiplier: number
  join_multiplier: number
  ad_multiplier: number
  run_multiplier: number
  probe_multiplier: number
  private_message_multiplier: number
  group_message_multiplier: number
  profile_update_multiplier: number
  allow_proactive_private_message: boolean
}

export interface AccountWarmupPolicySettings {
  enabled: boolean
  default_warmup_days: number
  minimum_warmup_days: number
  user_initiated_private_message_multiplier: number
  tiers: Record<string, AccountWarmupTierPolicy>
  stages: Record<string, AccountWarmupStagePolicy>
}

export interface AdDeliveryThrottleSettings {
  enabled: boolean
  delivery_interval_seconds: number
  batch_window_seconds: number
  batch_size_min: number
  batch_size_max: number
  cooldown_min_seconds: number
  cooldown_max_seconds: number
}

export interface AdDeliveryExecutionSettings {
  enabled: boolean
  dispatcher_interval_seconds: number
  max_deliveries_per_run: number
  max_deliveries_per_account_per_run: number
  group_campaign_cooldown_minutes: number
  stop_account_after_success: boolean
  stop_account_after_failure: boolean
}

export interface AdCapacitySettings {
  enabled: boolean
  timezone_offset_hours: number
  window_start_hour: number
  window_end_hour: number
  survival_check_delay_seconds: number
  survival_one_hour_seconds: number
  survival_twenty_four_hour_seconds: number
  survival_check_batch_size: number
  survival_retry_max_attempts: number
  survival_retry_base_seconds: number
  account_ad_daily_hard_cap: number
  account_group_daily_cap_default: number
  group_global_daily_hard_cap: number
  group_min_interval_seconds: number
  max_groups_per_account: number
  max_new_ad_groups_per_day: number
  leave_on_deleted_ad: boolean
  block_group_on_probe_failure: boolean
  ad_policy_ai_enabled: boolean
  ad_policy_ai_model: string
  ad_policy_ai_timeout_seconds: number
  ad_policy_ai_min_confidence: number
  ad_policy_ai_require_second_pass: boolean
  ad_policy_auto_probe_enabled: boolean
  ad_policy_auto_probe_daily_limit: number
  ad_policy_auto_probe_daily_limit_per_account: number
  ad_policy_auto_probe_interval_hours: number
  ad_policy_auto_ttl_days: number
  ad_policy_manual_ttl_days: number
  premium_min_samples: number
  premium_min_conversions: number
  premium_survival_rate_percent: number
  premium_clean_days_auto: number
  premium_clean_days_verified: number
  premium_growth_samples: number
  premium_full_capacity_samples: number
  premium_entry_capacity: number
  premium_growth_capacity: number
  premium_conversion_capacity_step: number
  deleted_ad_pause_hours: number
  membership_delete_block_count: number
  warmup_days_before_ads: number
  warmup_daily_interactions_min: number
  warmup_daily_interactions_max: number
  mature_daily_interactions_min: number
  mature_daily_interactions_max: number
  tier_daily_capacities: Record<string, number>
  hourly_weights: Record<string, number>
}

export interface GroupAdProfile {
  id: number
  group_id: number
  telegram_group_id: number
  group_title?: string
  group_status?: string
  group_level?: string
  ad_policy_mode: 'forbidden' | 'unknown' | 'unknown_probe' | 'approval_required' | 'soft_ad_trial' | 'soft_ad_allowed' | 'high_volume_ad_allowed' | string
  ad_policy_confidence: number
  ad_policy_source?: string
  ad_policy_verified_at?: string
  ad_policy_probe_status?: 'not_started' | 'sending' | 'sent' | 'survived' | 'deleted' | 'failed' | string
  ad_policy_probe_at?: string
  ad_policy_probe_account_id?: number
  ad_policy_probe_error?: string
  ad_policy_expires_at?: string
  ad_tier: string
  daily_capacity: number
  paused_until?: string
  survival_count: number
  deleted_count: number
  last_survived_at?: string
  last_deleted_at?: string
  blocked_reason?: string
  metrics: {
    completed_samples?: number
    survived_24h?: number
    deleted?: number
    survival_rate_24h?: number
    conversions?: number
    clean_days?: number
    premium_ready?: boolean
  }
}

export interface AdDynamicStatus {
  account_id: number
  account_label?: string
  account_status: string
  risk_level: string
  risk_score: number
  risk_reason?: string
  risk_pause_until?: string
  auto_join_enabled: boolean
  auto_ads_enabled: boolean
  business_stage: 'new' | 'normal' | 'hot' | 'cooldown' | string
  warmup_stage: 'observe' | 'seed' | 'soft' | 'ramp' | 'normal' | 'cooldown' | string
  managed_started_at?: string
  managed_age_days: number
  warmup_remaining_days: number
  warmup_action_multiplier: number
  health_score: number
  tier: 'hot' | 'normal' | 'conservative' | 'cooldown' | 'paused' | string
  success_24h: number
  failed_24h: number
  success_rate_24h: number
  group_control_failed_24h: number
  account_failed_24h: number
  transient_failed_24h: number
  dynamic_daily_limit: number
  dynamic_run_limit: number
  time_window_multiplier: number
  probe_based_daily_limit: number
  probe_factor: number
  probe_quality_multiplier: number
  recent_probe_success_6h: number
  recent_probe_failed_6h: number
  recent_probe_success_rate_6h: number
  ad_eligible_groups: number
  pending_probe_groups: number
  join_dynamic_daily_limit: number
  join_time_window_multiplier: number
  writable_rate: number
  probe_success_rate_24h: number
  ad_success_rate_24h: number
  average_group_quality_score: number
  warmup_summary: Array<{
    warmup_status: string
    probe_status: string
    count: number
  }>
  recent_errors: Array<{
    error: string
    count: number
  }>
  dynamic_health_diagnostic?: {
    primary_reason: string
    primary_label: string
    primary_severity: 'success' | 'warning' | 'danger' | 'info' | string
    reasons: Array<{
      reason: string
      label: string
      severity: 'success' | 'warning' | 'danger' | 'info' | string
      detail?: string
    }>
    adjustments: Array<{
      reason: string
      label: string
      delta: number
      severity: 'success' | 'warning' | 'danger' | 'info' | string
    }>
    negative_adjustments: Array<{
      reason: string
      label: string
      delta: number
      severity: 'success' | 'warning' | 'danger' | 'info' | string
    }>
    health_score: number
    risk_score: number
    warmup_action_multiplier: number
    probe_based_daily_limit: number
    probe_factor: number
    writable_rate: number
    probe_success_rate_24h: number
    ad_success_rate_24h: number
  }
  delivery_diagnostic?: {
    primary_block_reason: string
    primary_block_label: string
    primary_block_severity: 'success' | 'warning' | 'danger' | 'info' | string
    block_reasons: Array<{
      reason: string
      label: string
      severity: 'success' | 'warning' | 'danger' | 'info' | string
      detail?: string
    }>
    next_action: string
    next_action_label: string
    next_action_at?: string
    probe_execution_allowed: boolean
    ad_delivery_allowed: boolean
    active_campaign_id?: number
    active_campaign_name?: string
    enabled_binding_count: number
    group_diagnostics: {
      joined: number
      ready: number
      pending_probe: number
      probe_failed: number
      waiting_first_ad: number
      waiting_ad_eligible: number
      blocked: number
      group_not_active: number
      level_not_targeted: number
      ai_warmed?: number
      ad_permission_unknown?: number
      ad_permission_forbidden?: number
      ad_policy_expired?: number
      premium?: number
    }
    blocked_group_samples: Array<{
      group_id: number
      telegram_group_id: number
      title?: string
      level?: string
      group_status?: string
      reason: string
      label: string
      severity: 'success' | 'warning' | 'danger' | 'info' | string
      warmup_status: string
      probe_status: string
      ad_status: string
      probe_due_at?: string
      interaction_started_at?: string
      interaction_sent_today?: number
      first_ad_allowed_at?: string
      ad_eligible_after?: string
      last_probe_error?: string
      ad_policy_mode?: string
      ad_tier?: string
      ad_daily_capacity?: number
    }>
  }
}

export interface EffectiveLimitSource {
  key: string
  value: number
  active: boolean
}

export interface EffectiveLimitItem {
  key: string
  value: number | null
  unit: 'count_per_day' | 'count_per_run' | 'seconds' | string
  formula: 'min' | 'max' | string
  sources: EffectiveLimitSource[]
  dynamicFactors: string[]
}

export interface EffectiveLimitSummary {
  source: string
  riskGuardEnabled: boolean
  items: EffectiveLimitItem[]
  accountDynamicFields: {
    adDaily: string
    adPerRun: string
    joinDaily: string
  }
}

export interface AccountAdBinding {
  id: number
  account_id: number
  ad_campaign_id: number
  creative_id?: number
  enabled: boolean
  priority: number
  created_at?: string
  updated_at?: string
}

export interface AccountAdBindingBatchCreatePayload {
  account_ids?: number[]
  account_id?: number
  ad_campaign_id: number
  creative_ids: number[]
  enabled?: boolean
  priority?: number
}

export interface CreativePoolEnsurePayload {
  account_id: number
  ad_campaign_id: number
  min_pool_size?: number
  generate_count?: number
}

export const automationApi = {
  getAutoJoinSchedulerConfig: () => {
    return apiClient.get<{ data: AutoJoinSchedulerConfig }>('/automation/auto-join/scheduler-config')
  },

  updateAutoJoinSchedulerConfig: (data: Partial<AutoJoinSchedulerConfig>) => {
    return apiClient.put<{ data: AutoJoinSchedulerConfig }>('/automation/auto-join/scheduler-config', data)
  },

  getAdFailurePolicy: () => {
    return apiClient.get<{ data: AdFailurePolicy }>('/automation/ads/failure-policy')
  },

  updateAdFailurePolicy: (data: Partial<AdFailurePolicy>) => {
    return apiClient.put<{ data: AdFailurePolicy }>('/automation/ads/failure-policy', data)
  },

  getAccountRiskGuard: () => {
    return apiClient.get<{ data: AccountRiskGuardSettings }>('/automation/account-risk-guard')
  },

  updateAccountRiskGuard: (data: Partial<AccountRiskGuardSettings>) => {
    return apiClient.put<{ data: AccountRiskGuardSettings }>('/automation/account-risk-guard', data)
  },

  getAccountAssetPolicy: () => {
    return apiClient.get<{ data: AccountAssetPolicySettings }>('/automation/account-asset-policy')
  },

  updateAccountAssetPolicy: (data: Partial<AccountAssetPolicySettings>) => {
    return apiClient.put<{ data: AccountAssetPolicySettings }>('/automation/account-asset-policy', data)
  },

  getAccountWarmupPolicy: () => {
    return apiClient.get<{ data: AccountWarmupPolicySettings }>('/automation/account-warmup-policy')
  },

  updateAccountWarmupPolicy: (data: Partial<AccountWarmupPolicySettings>) => {
    return apiClient.put<{ data: AccountWarmupPolicySettings }>('/automation/account-warmup-policy', data)
  },

  getAdDeliveryThrottle: () => {
    return apiClient.get<{ data: AdDeliveryThrottleSettings }>('/automation/ads/delivery-throttle')
  },

  updateAdDeliveryThrottle: (data: Partial<AdDeliveryThrottleSettings>) => {
    return apiClient.put<{ data: AdDeliveryThrottleSettings }>('/automation/ads/delivery-throttle', data)
  },

  getAdDeliveryExecution: () => {
    return apiClient.get<{ data: AdDeliveryExecutionSettings }>('/automation/ads/delivery-execution')
  },

  updateAdDeliveryExecution: (data: Partial<AdDeliveryExecutionSettings>) => {
    return apiClient.put<{ data: AdDeliveryExecutionSettings }>('/automation/ads/delivery-execution', data)
  },

  getAdCapacity: () => {
    return apiClient.get<{ data: AdCapacitySettings }>('/automation/ads/capacity')
  },

  getEffectiveLimits: () => {
    return apiClient.get<{ data: EffectiveLimitSummary }>('/automation/effective-limits')
  },

  updateAdCapacity: (data: Partial<AdCapacitySettings>) => {
    return apiClient.put<{ data: AdCapacitySettings }>('/automation/ads/capacity', data)
  },

  getGroupAdProfiles: () => {
    return apiClient.get<{ data: GroupAdProfile[] }>('/automation/ads/group-profiles')
  },

  updateGroupAdPolicy: (
    groupId: number,
    data: { mode: string; confidence?: number; expires_days?: number; note?: string },
  ) => {
    return apiClient.put<{ data: GroupAdProfile }>(`/automation/ads/group-profiles/${groupId}/policy`, data)
  },

  triggerGroupAdPolicyProbe: (groupId: number, accountId?: number) => {
    return apiClient.post<{ data: {
      group_id: number
      telegram_group_id: number
      account_id: number
      campaign_id: number
      message_id: number
      ad_policy_mode: string
      ad_policy_probe_status: string
    } }>('/automation/ads/group-profiles/' + groupId + '/probe', accountId ? { account_id: accountId } : {})
  },

  getAdDynamicStatus: () => {
    return apiClient.get<{ data: AdDynamicStatus[] }>('/automation/ads/dynamic-status')
  },

  getAccountOperationConfig: (accountId: number) => {
    return apiClient.get<{ data: AccountOperationConfig }>(`/automation/accounts/${accountId}/operation-config`)
  },

  updateAccountOperationConfig: (accountId: number, data: AccountOperationConfigUpdatePayload) => {
    return apiClient.put<{ data: AccountOperationConfig }>(`/automation/accounts/${accountId}/operation-config`, data)
  },

  updateAccountOperationConfigsBatch: (data: { account_ids: number[]; config: AccountOperationConfigUpdatePayload }) => {
    return apiClient.put<{
      data: {
        updated_count: number
        skipped_count: number
        updated: AccountOperationConfig[]
        skipped: Array<{ account_id: number; reason: string }>
      }
    }>('/automation/accounts/operation-config/batch', data)
  },

  replenishKeywords: (data: { min_per_type?: Record<string, number>; generate_counts?: Record<string, number>; auto_approve?: boolean }) => {
    return apiClient.post<{ data: AutomationRunResult }>('/automation/keywords/replenish', data)
  },

  runAutoJoin: (data: { max_accounts?: number; keywords_per_account?: number; max_groups_per_keyword?: number; dry_run?: boolean }) => {
    return apiClient.post<{ data: AutomationRunResult }>('/automation/auto-join/run', data)
  },
  runGroupFailover: (data: { max_tasks?: number; dry_run?: boolean; target_account_ids?: number[] }) => {
    return apiClient.post<{ data: AutomationRunResult }>('/automation/auto-join/failover/run', data)
  },

  getGroupFailoverTasks: (params?: {
    status?: GroupFailoverStatus
    source_account_id?: number
    target_account_id?: number
    page?: number
    page_size?: number
  }) => {
    return apiClient.get<{
      data: GroupFailoverTask[]
      total: number
      page: number
      page_size: number
      summary: Partial<Record<GroupFailoverStatus, number>>
    }>('/automation/auto-join/failover/tasks', { params })
  },

  retryGroupFailoverTask: (id: number, targetAccountId?: number) => {
    return apiClient.post<{ data: GroupFailoverTask }>(
      `/automation/auto-join/failover/tasks/${id}/retry`,
      { target_account_id: targetAccountId },
    )
  },

  cancelGroupFailoverTask: (id: number) => {
    return apiClient.post<{ data: GroupFailoverTask }>(`/automation/auto-join/failover/tasks/${id}/cancel`)
  },



  runAds: (data: { max_deliveries?: number; dry_run?: boolean }) => {
    return apiClient.post<{ data: AutomationRunResult }>('/automation/ads/run', data)
  },

  runAdSurvivalCheck: (data: { limit?: number }) => {
    return apiClient.post<{ data: AutomationRunResult }>('/automation/ads/survival-check/run', data)
  },

  getAutoJoinAttempts: (params?: { account_id?: number; status?: string; limit?: number }) => {
    return apiClient.get<{ data: any[] }>('/automation/auto-join/attempts', { params })
  },

  getAutoJoinVerificationLogs: (params?: { account_id?: number; source?: string; success?: boolean; limit?: number }) => {
    return apiClient.get<{ data: AutoJoinVerificationLog[] }>('/automation/auto-join/verification-logs', { params })
  },

  getCreatives: (params?: { enabled?: boolean; page?: number; page_size?: number }) => {
    return apiClient.get<{ data: AdCreative[]; total: number }>('/automation/ads/creatives', { params })
  },

  createCreative: (data: Omit<AdCreative, 'id' | 'created_at' | 'updated_at'>) => {
    return apiClient.post<{ data: AdCreative }>('/automation/ads/creatives', data)
  },

  updateCreative: (id: number, data: Partial<Omit<AdCreative, 'id' | 'created_at' | 'updated_at'>>) => {
    return apiClient.put<{ data: AdCreative }>(`/automation/ads/creatives/${id}`, data)
  },

  deleteCreative: (id: number) => {
    return apiClient.delete(`/automation/ads/creatives/${id}`)
  },

  cleanupInvalidCreatives: () => {
    return apiClient.post<{ data: { disabled_count: number; creative_ids: number[] } }>('/automation/ads/creatives/cleanup-invalid')
  },

  getCampaigns: (params?: { enabled?: boolean; page?: number; page_size?: number }) => {
    return apiClient.get<{ data: AdCampaign[]; total: number }>('/automation/ads/campaigns', { params })
  },

  createCampaign: (data: AdCampaignCreatePayload) => {
    return apiClient.post<{ data: AdCampaign }>('/automation/ads/campaigns', data)
  },

  updateCampaign: (id: number, data: Partial<AdCampaignCreatePayload>) => {
    return apiClient.put<{ data: AdCampaign }>(`/automation/ads/campaigns/${id}`, data)
  },

  deleteCampaign: (id: number) => {
    return apiClient.delete(`/automation/ads/campaigns/${id}`)
  },

  getBindings: (params?: { account_id?: number; campaign_id?: number }) => {
    return apiClient.get<{ data: AccountAdBinding[] }>('/automation/ads/bindings', { params })
  },

  createBinding: (data: Omit<AccountAdBinding, 'id'>) => {
    return apiClient.post<{ data: AccountAdBinding }>('/automation/ads/bindings', data)
  },

  updateBinding: (id: number, data: Partial<Omit<AccountAdBinding, 'id' | 'account_id' | 'ad_campaign_id'>>) => {
    return apiClient.put<{ data: AccountAdBinding }>(`/automation/ads/bindings/${id}`, data)
  },

  deleteBinding: (id: number) => {
    return apiClient.delete(`/automation/ads/bindings/${id}`)
  },

  createBindingsBatch: (data: AccountAdBindingBatchCreatePayload) => {
    return apiClient.post<{ data: AccountAdBinding[] }>('/automation/ads/bindings/batch', data)
  },

  ensureCreativePool: (data: CreativePoolEnsurePayload) => {
    return apiClient.post<{ data: { account_id: number; ad_campaign_id: number; pool_size: number; created_count: number; creative_ids: number[] } }>(
      '/automation/ads/creatives/ensure-pool',
      data,
    )
  },

  getDeliveryLogs: (params?: {
    account_id?: number
    campaign_id?: number
    status?: string
    start_at?: string
    end_at?: string
    page?: number
    page_size?: number
    limit?: number
  }) => {
    return apiClient.get<{ data: any[]; total: number; page: number; page_size: number }>('/automation/ads/delivery-logs', { params })
  },
}
