import { describe, expect, it } from 'vitest'
import {
  createDefaultAdCapacity,
  createDefaultAdDeliveryExecution,
  createDefaultAdDeliveryThrottle,
  createDefaultAdFailurePolicy,
  createDefaultAutoJoinScheduler,
  createDefaultRiskGuard,
  createDefaultWarmupPolicy,
} from './automationDefaults'

describe('automation configuration defaults', () => {
  it('uses the backend scheduler and warmup minimums', () => {
    expect(createDefaultAutoJoinScheduler().scan_interval_minutes).toBe(5)

    const warmup = createDefaultWarmupPolicy()
    expect(warmup.minimum_warmup_days).toBe(7)
    expect(Object.values(warmup.tiers).every((tier) => tier.warmup_days >= 7)).toBe(true)
  })

  it('uses policy-specific delivery pacing and dispatcher settings', () => {
    const execution = createDefaultAdDeliveryExecution()
    expect(execution.growth_group_global_cooldown_seconds).toBe(86400)
    expect(execution.dispatcher_batch_size).toBe(100)
    expect(execution.max_parallel_accounts).toBe(10)

    const throttle = createDefaultAdDeliveryThrottle()
    expect(throttle.growth_min_interval_seconds).toBe(1800)
    expect(throttle.growth_max_interval_seconds).toBe(10800)
  })

  it('keeps ad policy and survival defaults without retired delivery caps', () => {
    const capacity = createDefaultAdCapacity()
    expect(capacity.max_new_ad_groups_per_day).toBe(2)
    expect(capacity.survival_check_delay_seconds).toBe(120)
    expect('account_ad_daily_hard_cap' in capacity).toBe(false)
    expect('tier_daily_capacities' in capacity).toBe(false)
  })

  it('exposes the unified account outbound hard cap', () => {
    const risk = createDefaultRiskGuard()
    expect(risk.account_outbound_message_hard_cap_default).toBe(30)
    expect(risk.redis_fail_closed).toBe(true)
    expect(risk.actions.join).toEqual({ daily_limit: 10, cooldown_seconds: 7200 })
    expect(risk.actions.owned_group_message).toEqual({ daily_limit: 0, cooldown_seconds: 0 })
    expect(risk.actions.ad_probe).toBeUndefined()
    expect(risk.actions.ad_delivery).toBeUndefined()
    const failurePolicy = createDefaultAdFailurePolicy()
    expect(failurePolicy.group_control_failure_limit).toBe(2)
    expect(failurePolicy.group_control_failure_window_hours).toBe(48)
  })
})
