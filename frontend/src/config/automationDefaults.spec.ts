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

  it('keeps delivery defaults inside backend hard limits', () => {
    const execution = createDefaultAdDeliveryExecution()
    expect(execution.group_campaign_cooldown_minutes).toBeGreaterThanOrEqual(4320)

    const throttle = createDefaultAdDeliveryThrottle()
    expect(throttle.delivery_interval_seconds).toBeGreaterThanOrEqual(9000)
    expect(throttle.cooldown_min_seconds).toBeGreaterThanOrEqual(9000)
  })

  it('keeps ad capacity defaults within system hard limits', () => {
    const capacity = createDefaultAdCapacity()
    expect(capacity.account_ad_daily_hard_cap).toBe(5)
    expect(capacity.group_min_interval_seconds).toBe(259200)
    expect(capacity.max_new_ad_groups_per_day).toBe(2)
  })

  it('exposes one complete default shape to both configuration pages', () => {
    const risk = createDefaultRiskGuard()
    expect(risk.global_daily_limit).toBe(30)
    expect(risk.group_write_daily_limit).toBe(8)
    expect(risk.actions.ad_delivery.daily_limit).toBe(5)
    expect(createDefaultAdFailurePolicy().group_control_failure_limit).toBe(1)
  })
})
