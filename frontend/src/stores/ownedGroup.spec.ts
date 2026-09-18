import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";

const api = vi.hoisted(() => ({
  getGovernance: vi.fn(),
  getGovernanceCandidates: vi.fn(),
  bindGovernance: vi.fn(),
  reconcileGovernance: vi.fn(),
  listBotProfiles: vi.fn(),
  deleteFailedDraft: vi.fn(),
  queueDissolution: vi.fn(),
  resolveDissolution: vi.fn(),
  getById: vi.fn(),
  getOperation: vi.fn(),
  list: vi.fn(),
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

  it("loads every Bot profile page so later 2000-resource batches remain selectable", async () => {
    const firstPage = Array.from({ length: 200 }, (_, index) => ({
      id: index + 1,
      owner_account_id: 7,
      account_id: index + 1000,
      status: "verified",
      enabled: true,
    }));
    const secondPage = [
      {
        id: 201,
        owner_account_id: 7,
        account_id: 1201,
        status: "verified",
        enabled: true,
      },
    ];
    api.listBotProfiles
      .mockResolvedValueOnce({ data: firstPage, total: 201 })
      .mockResolvedValueOnce({ data: secondPage, total: 201 });
    const store = useOwnedGroupStore();

    const response = await store.fetchBotProfiles();

    expect(api.listBotProfiles).toHaveBeenNthCalledWith(1, {
      limit: 200,
      offset: 0,
    });
    expect(api.listBotProfiles).toHaveBeenNthCalledWith(2, {
      limit: 200,
      offset: 200,
    });
    expect(response.data).toHaveLength(201);
    expect(store.botProfiles).toHaveLength(201);
    expect(store.botProfilesTotal).toBe(201);
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

    expect(store.governanceByAssetId[18].governance_status).toBe("managed");
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

  it("deletes a failed draft, clears selected state, and refreshes the list", async () => {
    api.deleteFailedDraft.mockResolvedValue(undefined);
    api.list.mockResolvedValue({ data: { data: [], total: 0 } });
    const store = useOwnedGroupStore();
    const failedDraft = {
      id: 29,
      internal_name: "failed-29",
      title: "Failed 29",
      visibility: "private",
      owner_account_id: 7,
      invite_mode: "direct_invite",
      status: "needs_attention",
      telegram_chat_id: null,
      core_group_id: null,
      managed_binding_id: null,
      guardian_bot_account_id: null,
      governance_status: "disabled",
      member_count: 0,
      created_at: "2026-09-13T00:00:00Z",
      updated_at: "2026-09-13T00:00:00Z",
    } as const;
    store.list = [failedDraft];
    store.select(failedDraft);

    await store.deleteFailedDraft(29);

    expect(api.deleteFailedDraft).toHaveBeenCalledWith(29);
    expect(api.list).toHaveBeenCalledWith({ limit: 200 });
    expect(store.current).toBeNull();
    expect(store.list).toEqual([]);
  });

  it("queues dissolution, then refreshes the asset and operation read models", async () => {
    const dissolvingAsset = {
      id: 29,
      internal_name: "owned-29",
      title: "Owned 29",
      visibility: "private",
      owner_account_id: 7,
      invite_mode: "direct_invite",
      status: "dissolving",
      telegram_chat_id: -1001234567891,
      member_count: 3,
      created_at: "2026-09-15T00:00:00Z",
      updated_at: "2026-09-15T00:00:00Z",
    };
    const dissolutionOperation = {
      id: 41,
      group_asset_id: 29,
      operation_type: "dissolve",
      status: "queued",
      planned_count: 1,
      completed_count: 0,
      skipped_count: 0,
      failed_count: 0,
      message: "queued",
    };
    api.queueDissolution.mockResolvedValue({ id: 41 });
    api.getById.mockResolvedValue(dissolvingAsset);
    api.getOperation.mockResolvedValue(dissolutionOperation);
    const store = useOwnedGroupStore();

    const result = await store.queueDissolution(29, "DISSOLVE 29");

    expect(api.queueDissolution).toHaveBeenCalledWith(
      29,
      { confirmation: "DISSOLVE 29" },
      expect.stringMatching(/^owned-group-dissolution-29-/),
    );
    expect(api.getById).toHaveBeenCalledWith(29);
    expect(api.getOperation).toHaveBeenCalledWith(41);
    expect(store.current).toMatchObject({ id: 29, status: "dissolving" });
    expect(store.operation).toMatchObject({
      id: 41,
      operation_type: "dissolve",
      status: "queued",
    });
    expect(result).toMatchObject({ id: 41, operation_type: "dissolve" });
  });

  it("resolves an uncertain dissolution, then refreshes the asset and operation", async () => {
    const needsAttentionAsset = {
      id: 29,
      internal_name: "review-29",
      title: "Review 29",
      visibility: "private",
      owner_account_id: 7,
      invite_mode: "direct_invite",
      status: "needs_attention",
      pending_dissolution_review: true,
      telegram_chat_id: -1001234567891,
      member_count: 3,
      created_at: "2026-09-15T00:00:00Z",
      updated_at: "2026-09-15T00:00:00Z",
    };
    const resolutionOperation = {
      id: 41,
      group_asset_id: 29,
      operation_type: "dissolve",
      status: "failed",
      planned_count: 1,
      completed_count: 0,
      skipped_count: 0,
      failed_count: 1,
      message: "administrator_confirmed_group_still_exists",
    };
    api.resolveDissolution.mockResolvedValue({
      code: 0,
      message: "已确认群仍存在，资产恢复为可用",
      data: {
        id: 29,
        status: "ready",
        operation_id: 41,
        operation_status: "failed",
      },
    });
    api.getById.mockResolvedValue({
      ...needsAttentionAsset,
      status: "ready",
      pending_dissolution_review: false,
    });
    api.getOperation.mockResolvedValue(resolutionOperation);
    const store = useOwnedGroupStore();

    const result = await store.resolveDissolution(
      29,
      "still_exists",
      "CONFIRM EXISTS 29",
    );

    expect(api.resolveDissolution).toHaveBeenCalledWith(29, {
      outcome: "still_exists",
      confirmation: "CONFIRM EXISTS 29",
    });
    expect(api.getById).toHaveBeenCalledWith(29);
    expect(api.getOperation).toHaveBeenCalledWith(41);
    expect(store.current).toMatchObject({ id: 29, status: "ready" });
    expect(store.operation).toMatchObject({ id: 41, status: "failed" });
    expect(result.data.operation_status).toBe("failed");
  });
});
