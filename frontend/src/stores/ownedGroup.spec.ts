import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";

const api = vi.hoisted(() => ({
  getGovernance: vi.fn(),
  getGovernanceCandidates: vi.fn(),
  bindGovernance: vi.fn(),
  reconcileGovernance: vi.fn(),
}));

vi.mock("@/api/ownedGroups", () => ({
  ownedGroupsApi: api,
}));

import { useOwnedGroupStore } from "./ownedGroup";

const createGovernance = (
  governanceStatus: "disabled" | "pending" | "managed" | "degraded",
) => ({
  asset_id: 18,
  asset_status: "ready",
  telegram_chat_id: -1001234567890,
  core_group_id: governanceStatus === "managed" ? 311 : null,
  managed_binding_id: governanceStatus === "managed" ? 27 : null,
  guardian_bot_account_id: 42,
  guardian_bot_profile_id: 9,
  owned_bot_profile_id: 16,
  governance_status: governanceStatus,
  binding_status: governanceStatus === "managed" ? "active" : "degraded",
  bot_role: governanceStatus === "managed" ? "admin" : null,
  permission_probe: null,
  capabilities: {
    verification: governanceStatus === "managed",
    sensitive_keywords: governanceStatus === "managed",
    anti_spam: governanceStatus === "managed",
    warn: governanceStatus === "managed",
    mute: governanceStatus === "managed",
    ban: governanceStatus === "managed",
    announcement: governanceStatus === "managed",
    pin_message: governanceStatus === "managed",
    activity: governanceStatus === "managed",
  },
  failure:
    governanceStatus === "degraded"
      ? {
          reason: "guardian_bot_not_admin",
          message: "Guardian Bot 不是管理员",
          retryable: true,
          missing_permissions: [],
          correlation_id: "og-gov-18-failed",
        }
      : null,
  governance_pending_at: null,
  governance_enabled_at: null,
  governance_last_checked_at: "2026-09-09T12:00:00Z",
  stale_pending: false,
  reused: false,
  correlation_id: "og-gov-18-test",
});

describe("ownedGroup governance store", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.clearAllMocks();
  });

  it("caches governance by asset id and updates the asset read model", async () => {
    api.getGovernance.mockResolvedValue(createGovernance("managed"));
    const store = useOwnedGroupStore();
    store.list = [
      {
        id: 18,
        internal_name: "owned-18",
        title: "Owned 18",
        visibility: "private",
        owner_account_id: 7,
        invite_mode: "direct_invite",
        status: "ready",
        member_count: 1,
        created_at: "2026-09-09T00:00:00Z",
        updated_at: "2026-09-09T00:00:00Z",
      },
    ];

    await store.fetchGovernance(18);

    expect(store.governanceByAssetId[18].governance_status).toBe(
      "managed",
    );
    expect(store.list[0]).toMatchObject({
      core_group_id: 311,
      managed_binding_id: 27,
      guardian_bot_account_id: 42,
      governance_status: "managed",
    });
    expect(store.governanceLoadingByAssetId[18]).toBe(false);
  });

  it("caches eligible candidates without credential fields", async () => {
    api.getGovernanceCandidates.mockResolvedValue([
      {
        guardian_bot_account_id: 42,
        guardian_bot_profile_id: 9,
        owned_bot_profile_id: 16,
        display_name: "Guardian A",
        username: "guardian_a",
        owned_profile_status: "verified",
        guardian_health_status: "healthy",
        local_membership_status: "member_verified",
        local_is_admin: false,
      },
    ]);
    const store = useOwnedGroupStore();

    await store.fetchGovernanceCandidates(18);

    expect(store.governanceCandidatesByAssetId[18]).toHaveLength(1);
    expect(store.governanceCandidatesByAssetId[18][0]).not.toHaveProperty(
      "bot_token",
    );
  });

  it("refreshes authoritative governance state after a bind error", async () => {
    const mutationError = new Error("bind failed");
    api.bindGovernance.mockRejectedValue(mutationError);
    api.getGovernance.mockResolvedValue(createGovernance("degraded"));
    const store = useOwnedGroupStore();

    await expect(store.bindGovernance(18, 42)).rejects.toBe(mutationError);

    expect(api.getGovernance).toHaveBeenCalledWith(18);
    expect(store.governanceByAssetId[18]).toMatchObject({
      governance_status: "degraded",
      failure: { reason: "guardian_bot_not_admin" },
    });
    expect(store.governanceLoadingByAssetId[18]).toBe(false);
  });
});
