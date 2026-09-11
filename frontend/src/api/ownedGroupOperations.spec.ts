import { beforeEach, describe, expect, it, vi } from "vitest";

const get = vi.hoisted(() => vi.fn());
vi.mock("./client", () => ({ default: { get } }));

import { normalizeMember, normalizeOperationsCenterSummary, ownedGroupOperationsApi } from "./ownedGroupOperations";

const coverage = { data_scope: "managed_and_observed", is_complete: false, full_roster_supported: false, coverage_status: "collecting", observation_started_at: null, last_observed_at: null, blocking_reasons: [] };
const summary = (overrides: Record<string, unknown> = {}) => ({
  asset: { asset_id: 18, internal_name: "owned-18", title: "群 18", visibility: "private", asset_status: "ready", telegram_chat_id: -1001234567890, core_group_id: 3, managed_binding_id: 4, guardian_bot_account_id: 5, owner_account_id: 6, managed_resource_count: 3, updated_at: "2026-09-11T00:00:00Z" },
  sections: Object.fromEntries(["orchestration", "governance", "members", "messaging", "activities", "audit"].map((key) => [key, { state: "ready", can_view: true, can_manage: true, can_execute: true, blocking_reasons: [] }])),
  governance: { status: "managed", bot_account_id: 5, bot_role: "admin", health_status: "healthy", last_checked_at: null },
  messaging: { static_enabled: true, runtime_enabled: true, dry_run: false, can_send: true, policy_count: 1, enabled_policy_count: 1, pending_review_count: 0, sent_today: 0 },
  member_summary: { managed_resource_count: 3, observed_real_user_count: 2, counts_by_kind: { real_user: 2, system_ad_account: 2, system_bot: 1 }, unresolved_count: 0, conflict_count: 0, coverage },
  latest_operation: null,
  permissions: { manage_orchestration: true, manage_governance: true, manage_messaging: true, manage_persona: true, manage_activities: true, view_members: true, view_audit: true },
  snapshot_at: "2026-09-11T00:00:00Z",
  ...overrides,
});

const member = (overrides: Record<string, unknown> = {}) => ({
  member_key: "real_user:telegram:8", member_kind: "real_user", classification_status: "resolved", classification_reason: "observed_real_user",
  telegram_user_id: 8, display_name: "User", username: "@user", primary_source: "guardian_observation", sources: [], account_id: null,
  owned_bot_profile_id: null, guardian_bot_profile_id: null, parent_account_id: null, user_id: 7, operation_mode: null, account_status: null,
  account_risk_level: null, risk_pause_until: null, risk_scope: "user_global", persona_configured: null, persona_revision: null,
  telegram_role: "member", is_admin: false, admin_title: null, presence_status: "present", presence_confidence: "observed", raw_presence_status: "present",
  user_state: "normal", warning_count: 0, muted_until: null, joined_at: null, left_at: null, last_verified_at: null, last_observed_at: "2026-09-11T00:00:00Z",
  data_quality: "reliable", quality_codes: [], ...overrides,
});

describe("ownedGroupOperations API", () => {
  beforeEach(() => get.mockReset());

  it("normalizes unknown section state and unsafe Telegram chat ID fail-closed", () => {
    const raw = summary();
    (raw.asset as Record<string, unknown>).telegram_chat_id = 9007199254740992;
    (raw.sections as Record<string, Record<string, unknown>>).messaging.state = "future_state";
    const result = normalizeOperationsCenterSummary(raw);
    expect(result.asset.telegram_chat_id).toBeNull();
    expect(result.asset.telegram_chat_id_raw).toBe("9007199254740992");
    expect(result.sections.governance).toMatchObject({ state: "unavailable", can_execute: false });
    expect(result.sections.governance.blocking_reasons).toContain("telegram_chat_id_unsafe");
    expect(result.sections.messaging.state).toBe("unavailable");
    expect(result.sections.messaging.blocking_reasons[0]).toContain("unknown_section_state");
  });

  it("normalizes unsafe identity and unknown member enums without inventing a type", () => {
    const result = normalizeMember(member({ telegram_user_id: 9007199254740992, member_kind: "future_kind", presence_status: "live" }));
    expect(result.telegram_user_id).toBeNull();
    expect(result.member_kind).toBeNull();
    expect(result.classification_status).toBe("unresolved");
    expect(result.presence_status).toBe("unknown");
    expect(result.data_quality).toBe("partial");
    expect(result.quality_codes).toEqual(expect.arrayContaining(["telegram_user_id_unsafe", "unknown_member_kind:future_kind", "unknown_presence_status:live"]));
  });

  it("rejects zero Telegram chat IDs and non-positive Telegram user IDs", () => {
    const raw = summary();
    (raw.asset as Record<string, unknown>).telegram_chat_id = 0;
    const normalizedSummary = normalizeOperationsCenterSummary(raw);
    expect(normalizedSummary.asset.telegram_chat_id).toBeNull();
    expect(normalizedSummary.asset.telegram_chat_id_raw).toBe("0");
    expect(normalizedSummary.sections.governance.blocking_reasons).toContain("telegram_chat_id_unsafe");

    const normalizedMember = normalizeMember(member({ telegram_user_id: -8 }));
    expect(normalizedMember.telegram_user_id).toBeNull();
    expect(normalizedMember.telegram_user_id_raw).toBe("-8");
    expect(normalizedMember.quality_codes).toContain("telegram_user_id_unsafe");
  });

  it("adds quality codes for every unknown member enum", () => {
    const result = normalizeMember(member({
      classification_status: "future_classification",
      presence_confidence: "future_confidence",
      telegram_role: "future_role",
      data_quality: "future_quality",
    }));
    expect(result).toMatchObject({ classification_status: "unresolved", presence_confidence: "unknown", telegram_role: "unknown", data_quality: "partial" });
    expect(result.quality_codes).toEqual(expect.arrayContaining([
      "unknown_classification_status:future_classification",
      "unknown_presence_confidence:future_confidence",
      "unknown_telegram_role:future_role",
      "unknown_data_quality:future_quality",
    ]));
  });

  it("validates the new envelope and keeps server member classification", async () => {
    get.mockResolvedValue({ data: { data: [member()], total: 1, offset: 0, limit: 50, coverage, summary: summary().member_summary, correlation_id: "ops-1" } });
    const page = await ownedGroupOperationsApi.listMembers(18, { offset: 0, limit: 50 });
    expect(get).toHaveBeenCalledWith("/owned-groups/18/members", expect.objectContaining({ params: { offset: 0, limit: 50 } }));
    expect(page.items[0]).toMatchObject({ member_kind: "real_user", username: "user" });
    expect(page.coverage.is_complete).toBe(false);
  });

  it("normalizes legacy data to items and preserves unknown operation status", async () => {
    get.mockResolvedValue({ data: { code: 0, message: "success", data: [{ id: 1, group_asset_id: 18, status: "future", planned_count: 2, completed_count: 1, skipped_count: 0, failed_count: 0, selection_snapshot_hash: "a", config_snapshot_hash: "b", idempotency_key: "c", schedule_at: null, created_at: "x", updated_at: "y" }], total: 1 } });
    const page = await ownedGroupOperationsApi.listOperations(18, { offset: 0, limit: 20 });
    expect(page.items[0]).toMatchObject({ status: "unknown", raw_status: "future" });
    expect(page).not.toHaveProperty("data");
  });

  it("rejects invalid new and legacy wire envelopes", async () => {
    get.mockResolvedValueOnce({ data: { data: null } });
    await expect(ownedGroupOperationsApi.getSummary(18)).rejects.toThrow("Invalid operations center response envelope");
    get.mockResolvedValueOnce({ data: { code: 1, data: [], total: 0 } });
    await expect(ownedGroupOperationsApi.listAuditEvents(18, { offset: 0, limit: 20 })).rejects.toThrow("Invalid legacy list response");
  });
});
