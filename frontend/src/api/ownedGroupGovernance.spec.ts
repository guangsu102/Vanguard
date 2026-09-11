import { beforeEach, describe, expect, it, vi } from "vitest";

const { get, post } = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
}));

vi.mock("./client", () => ({
  default: { get, post },
}));

import {
  getOwnedGroupGovernanceFailure,
  ownedGroupsApi,
} from "./ownedGroups";

const governanceData = {
  asset_id: 18,
  asset_status: "ready",
  telegram_chat_id: -1001234567890,
  core_group_id: 311,
  managed_binding_id: 27,
  guardian_bot_account_id: 42,
  guardian_bot_profile_id: 9,
  owned_bot_profile_id: 16,
  governance_status: "managed",
  binding_status: "active",
  bot_role: "admin",
  permission_probe: {
    status: "passed",
    required_permissions: ["can_delete_messages"],
    granted_permissions: ["can_delete_messages"],
    missing_permissions: [],
    checked_at: "2026-09-09T12:00:00Z",
  },
  capabilities: { verification: true, anti_spam: true },
  failure: null,
  governance_pending_at: null,
  governance_enabled_at: "2026-09-09T12:00:00Z",
  governance_last_checked_at: "2026-09-09T12:00:00Z",
  stale_pending: false,
  reused: false,
  correlation_id: "og-gov-18-test",
};

describe("owned-group governance API", () => {
  beforeEach(() => {
    get.mockReset();
    post.mockReset();
  });

  it("loads and normalizes governance status", async () => {
    get.mockResolvedValue({
      data: { code: 0, message: "success", data: governanceData },
    });

    const result = await ownedGroupsApi.getGovernance(18);

    expect(get).toHaveBeenCalledWith("/owned-groups/18/governance");
    expect(result).toMatchObject({
      asset_id: 18,
      telegram_chat_id: -1001234567890,
      governance_status: "managed",
    });
    expect(result.capabilities.verification).toBe(true);
    expect(result.capabilities.pin_message).toBe(false);
  });

  it("loads only server-approved governance candidates", async () => {
    get.mockResolvedValue({
      data: {
        code: 0,
        message: "success",
        data: [
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
        ],
      },
    });

    const result = await ownedGroupsApi.getGovernanceCandidates(18);

    expect(get).toHaveBeenCalledWith(
      "/owned-groups/18/governance/candidates",
    );
    expect(result).toEqual([
      expect.objectContaining({
        guardian_bot_account_id: 42,
        owned_profile_status: "verified",
        local_is_admin: false,
      }),
    ]);
  });

  it("uses the bind and reconcile contracts", async () => {
    post.mockResolvedValue({
      data: { code: 0, message: "success", data: governanceData },
    });

    await ownedGroupsApi.bindGovernance(18, 42);
    await ownedGroupsApi.reconcileGovernance(18);

    expect(post).toHaveBeenNthCalledWith(
      1,
      "/owned-groups/18/governance/bind",
      { guardian_bot_account_id: 42 },
    );
    expect(post).toHaveBeenNthCalledWith(
      2,
      "/owned-groups/18/governance/reconcile",
      {},
    );
  });

  it("extracts a safe actionable error summary", () => {
    const failure = getOwnedGroupGovernanceFailure({
      response: {
        data: {
          detail: {
            reason: "guardian_permissions_missing",
            message:
              "Token=123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ failed https://t.me/+private",
            retryable: true,
            missing_permissions: ["can_restrict_members"],
            correlation_id: "og-gov-18-failed",
          },
        },
      },
    });

    expect(failure).toMatchObject({
      reason: "guardian_permissions_missing",
      retryable: true,
      missing_permissions: ["can_restrict_members"],
      correlation_id: "og-gov-18-failed",
    });
    expect(failure.message).not.toContain("ABCDEFGHIJKLMNOPQRSTUVWXYZ");
    expect(failure.message).not.toContain("t.me/+private");
  });
});
