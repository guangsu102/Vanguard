import type {
  AccountRiskGuardSettings,
  AccountAssetPolicySettings,
  AccountWarmupPolicySettings,
  AdCapacitySettings,
  AdDeliveryExecutionSettings,
  AdDeliveryThrottleSettings,
  AdFailurePolicy,
  AutoJoinSchedulerConfig,
} from '@/api/automation'

type CompleteAutoJoinSchedulerConfig = AutoJoinSchedulerConfig & {
  search_filter: NonNullable<AutoJoinSchedulerConfig['search_filter']>
  join_verification: NonNullable<AutoJoinSchedulerConfig['join_verification']>
  group_capacity_cleanup: NonNullable<AutoJoinSchedulerConfig['group_capacity_cleanup']>
}

export const createDefaultAutoJoinScheduler = (): CompleteAutoJoinSchedulerConfig => ({
  enabled: true,
  scan_interval_minutes: 5,
  search_filter: { title_blacklist_enabled: true, title_blacklist: [] },
  join_verification: {
    enabled: true,
    ai_enabled: true,
    confidence_threshold: 0.72,
    post_action_wait_seconds: 8,
    post_action_recheck_attempts: 3,
    post_action_extra_wait_seconds: 12,
    message_limit: 20,
    ai_timeout_seconds: 45,
    action_timeout_seconds: 5,
    pending_sync_min_age_seconds: 120,
    pending_sync_limit: 5,
    unknown_challenge_action: 'leave',
    allow_button_clicks: true,
    allow_text_answers: true,
    answer_profile: '中文用户，主要为了学习交流、找资料、行业沟通。',
  },
  group_capacity_cleanup: { enabled: false, no_conversion_days: 30, min_join_age_days: 30, max_cleanup_per_run: 15 },
})

export const createDefaultRiskGuard = (): AccountRiskGuardSettings => ({
  enabled: true,
  global_daily_limit: 30,
  group_write_daily_limit: 8,
  redis_fail_closed: null,
  actions: {
    search: { daily_limit: 100, cooldown_seconds: 30 },
    join: { daily_limit: 6, cooldown_seconds: 7200 },
    private_message: { daily_limit: 20, cooldown_seconds: 300 },
    group_message: { daily_limit: 4, cooldown_seconds: 7200 },
    ad_probe: { daily_limit: 10, cooldown_seconds: 3600 },
    ai_warmup: { daily_limit: 1, cooldown_seconds: 21600 },
    moderation: { daily_limit: 60, cooldown_seconds: 15 },
    ad_delivery: { daily_limit: 5, cooldown_seconds: 9000 },
    profile_update: { daily_limit: 5, cooldown_seconds: 3600 },
    reaction: { daily_limit: 120, cooldown_seconds: 10 },
    forward: { daily_limit: 25, cooldown_seconds: 120 },
    pin: { daily_limit: 20, cooldown_seconds: 120 },
    bot_message: { daily_limit: 500, cooldown_seconds: 1 },
    bot_pin: { daily_limit: 100, cooldown_seconds: 5 },
    channel_create: { daily_limit: 1, cooldown_seconds: 86400 },
  },
  level_thresholds: { watch: 20, limited: 45, frozen: 70, quarantined: 90 },
  level_budget_multipliers: { normal: 1, watch: 0.7, limited: 0.45, frozen: 0, quarantined: 0 },
  risk_score_deltas: { group_write_forbidden: 4, platform_group_write_forbidden: 12, flood_wait: 15, peer_flood: 35, account_banned: 50, account_restricted: 50, generic_failure: 5, block: 1 },
  lifecycle: { default_freeze_seconds: 3600, flood_wait_buffer_seconds: 60, peer_flood_freeze_seconds: 86400, account_restricted_freeze_seconds: 86400, group_write_forbidden_freeze_seconds: 43200, recovery_seconds: 86400, post_freeze_score_cap: 69, manual_clear_score_cap: 44, decay_interval_hours: 24, decay_points_per_interval: 8, new_account_days: 3, new_account_multiplier: 0.3, recovery_multiplier: 0.5, healthy_account_days: 14, healthy_account_multiplier: 1, max_budget_multiplier: 1 },
  group_write_forbidden: { freeze_window_hours: 2, freeze_distinct_groups: 5, quarantine_window_hours: 24, quarantine_distinct_groups: 10 },
  retention: { low_value_detail_retention_days: 14, high_value_detail_retention_days: 90, daily_stat_retention_days: 370 },
})

const createDefaultWarmupStages = (): AccountWarmupPolicySettings['stages'] => ({
  observe: { limit_multiplier: 0.08, join_multiplier: 0, ad_multiplier: 0, run_multiplier: 0, probe_multiplier: 0.1, private_message_multiplier: 0, group_message_multiplier: 0.05, profile_update_multiplier: 0.2, allow_proactive_private_message: false },
  seed: { limit_multiplier: 0.15, join_multiplier: 0.15, ad_multiplier: 0, run_multiplier: 0, probe_multiplier: 0.25, private_message_multiplier: 0, group_message_multiplier: 0.15, profile_update_multiplier: 0.5, allow_proactive_private_message: false },
  soft: { limit_multiplier: 0.35, join_multiplier: 0.35, ad_multiplier: 0.25, run_multiplier: 0.25, probe_multiplier: 0.45, private_message_multiplier: 0.1, group_message_multiplier: 0.35, profile_update_multiplier: 0.75, allow_proactive_private_message: false },
  ramp: { limit_multiplier: 0.65, join_multiplier: 0.65, ad_multiplier: 0.65, run_multiplier: 0.65, probe_multiplier: 0.75, private_message_multiplier: 0.25, group_message_multiplier: 0.65, profile_update_multiplier: 1, allow_proactive_private_message: false },
  normal: { limit_multiplier: 1, join_multiplier: 1, ad_multiplier: 1, run_multiplier: 1, probe_multiplier: 1, private_message_multiplier: 1, group_message_multiplier: 1, profile_update_multiplier: 1, allow_proactive_private_message: true },
  cooldown: { limit_multiplier: 0, join_multiplier: 0, ad_multiplier: 0, run_multiplier: 0, probe_multiplier: 0, private_message_multiplier: 0, group_message_multiplier: 0, profile_update_multiplier: 0, allow_proactive_private_message: false },
})

export const createDefaultWarmupPolicy = (): AccountWarmupPolicySettings => ({
  enabled: true,
  default_warmup_days: 15,
  minimum_warmup_days: 7,
  user_initiated_private_message_multiplier: 1,
  tiers: { unknown: { warmup_days: 15 }, month_1: { warmup_days: 18 }, month_3_6: { warmup_days: 12 }, year_1: { warmup_days: 9 }, year_2: { warmup_days: 7 }, year_3_plus: { warmup_days: 7 } },
  stages: createDefaultWarmupStages(),
})

export const createDefaultAssetPolicy = (): AccountAssetPolicySettings => ({
  enabled: true,
  tiers: {
    unknown: { join_multiplier: 0.6, ad_multiplier: 0.5, run_multiplier: 0.5, probe_multiplier: 0.7, warmup_days: 18, age_floor_days: 0 },
    month_1: { join_multiplier: 0.4, ad_multiplier: 0.25, run_multiplier: 0.25, probe_multiplier: 0.45, warmup_days: 25, age_floor_days: 30 },
    month_3_6: { join_multiplier: 0.7, ad_multiplier: 0.6, run_multiplier: 0.6, probe_multiplier: 0.75, warmup_days: 18, age_floor_days: 120 },
    year_1: { join_multiplier: 1, ad_multiplier: 1, run_multiplier: 1, probe_multiplier: 1, warmup_days: 12, age_floor_days: 365 },
    year_2: { join_multiplier: 1.15, ad_multiplier: 1.2, run_multiplier: 1.15, probe_multiplier: 1.1, warmup_days: 9, age_floor_days: 730 },
    year_3_plus: { join_multiplier: 1.3, ad_multiplier: 1.35, run_multiplier: 1.25, probe_multiplier: 1.15, warmup_days: 7, age_floor_days: 1095 },
  },
})
export const createDefaultAdDeliveryExecution = (): AdDeliveryExecutionSettings => ({ enabled: true, dispatcher_interval_seconds: 60, max_deliveries_per_run: 1, max_deliveries_per_account_per_run: 1, group_campaign_cooldown_minutes: 4320, stop_account_after_success: true, stop_account_after_failure: true })
export const createDefaultAdDeliveryThrottle = (): AdDeliveryThrottleSettings => ({ enabled: true, delivery_interval_seconds: 9000, batch_window_seconds: 3600, batch_size_min: 1, batch_size_max: 1, cooldown_min_seconds: 9000, cooldown_max_seconds: 10800 })

export const createDefaultAdCapacity = (): AdCapacitySettings => ({
  enabled: true, timezone_offset_hours: 8, window_start_hour: 9, window_end_hour: 2,
  survival_check_delay_seconds: 120, survival_one_hour_seconds: 3600, survival_twenty_four_hour_seconds: 86400,
  survival_check_batch_size: 50, survival_retry_max_attempts: 3, survival_retry_base_seconds: 300,
  account_ad_daily_hard_cap: 5, account_group_daily_cap_default: 1, group_global_daily_hard_cap: 400,
  group_min_interval_seconds: 259200, max_groups_per_account: 400, max_new_ad_groups_per_day: 2,
  leave_on_deleted_ad: true, block_group_on_probe_failure: true, ad_policy_ai_enabled: true,
  ad_policy_ai_model: 'gpt-5.6-terra', ad_policy_ai_timeout_seconds: 45, ad_policy_ai_min_confidence: 95,
  ad_policy_ai_require_second_pass: true, ad_policy_auto_probe_enabled: false, ad_policy_auto_probe_daily_limit: 1,
  ad_policy_auto_probe_daily_limit_per_account: 10, ad_policy_auto_probe_interval_hours: 24, ad_policy_auto_ttl_days: 7,
  ad_policy_manual_ttl_days: 30, premium_min_samples: 20, premium_min_conversions: 1, premium_survival_rate_percent: 95,
  premium_clean_days_auto: 5, premium_clean_days_verified: 3, premium_growth_samples: 100, premium_full_capacity_samples: 1000,
  premium_entry_capacity: 20, premium_growth_capacity: 50, premium_conversion_capacity_step: 20,
  deleted_ad_pause_hours: 72, membership_delete_block_count: 2, warmup_days_before_ads: 15,
  warmup_daily_interactions_min: 0, warmup_daily_interactions_max: 1, mature_daily_interactions_min: 0, mature_daily_interactions_max: 1,
  tier_daily_capacities: { blocked: 0, observing: 0, trial: 1, validated: 3, stable: 10, low: 3, medium: 10, high: 20, premium: 400 },
  hourly_weights: {},
})

export const createDefaultAdFailurePolicy = (): AdFailurePolicy => ({ enabled: true, leave_on_group_control_failure: true, group_control_failure_limit: 1, group_control_failure_window_hours: 720, levels: ['A', 'B', 'C', 'UNRATED'] })
