import { beforeEach, describe, expect, it, vi } from "vitest";

const { get, post, patch, deleteRequest } = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  patch: vi.fn(),
  deleteRequest: vi.fn(),
}));

vi.mock("./client", () => ({
  default: { get, post, patch, delete: deleteRequest },
}));

import { ownedGroupsApi, redactOwnedGroupError } from "./ownedGroups";

describe("ownedGroupsApi Bot profile contract", () => {
  beforeEach(() => {
    get.mockReset();
    post.mockReset();
    patch.mockReset();
    deleteRequest.mockReset();
  });

  it("lists token-free Bot profile metadata", async () => {
    get.mockResolvedValue({
      data: {
        code: 0,
        data: [
          {
            id: 7,
            owner_account_id: 11,
            account_id: 13,
            bot_username: "owned_bot",
            status: "verified",
            enabled: true,
            bot_token: "must-not-be-exposed",
          },
        ],
        total: 1,
      },
    });

    const result = await ownedGroupsApi.listBotProfiles({ limit: 200 });

    expect(get).toHaveBeenCalledWith("/owned-groups/bot-profiles", {
      params: { limit: 200 },
    });
    expect(result.total).toBe(1);
    expect(result.data[0]).toMatchObject({
      id: 7,
      status: "verified",
      enabled: true,
    });
    expect("bot_token" in result.data[0]).toBe(false);
  });

  it("sends a registration token once and returns only safe metadata", async () => {
    post.mockResolvedValue({
      data: {
        code: 0,
        data: {
          id: 8,
          owner_account_id: 11,
          account_id: 13,
          status: "pending_verification",
          enabled: true,
        },
      },
    });

    const result = await ownedGroupsApi.registerBotProfile({
      owner_account_id: 11,
      account_id: 13,
      bot_token: "  123456:secret-token  ",
    });

    expect(post).toHaveBeenCalledWith("/owned-groups/bot-profiles", {
      owner_account_id: 11,
      account_id: 13,
      bot_token: "123456:secret-token",
    });
    expect(result).toMatchObject({ id: 8, status: "pending_verification" });
    expect("bot_token" in result).toBe(false);
  });

  it("uses the strict resource precheck and profile controls", async () => {
    post
      .mockResolvedValueOnce({
        data: { asset_id: 3, allowed: true, reason: "eligible" },
      })
      .mockResolvedValueOnce({ data: { id: 8, status: "verified" } });
    patch.mockResolvedValue({
      data: { id: 8, status: "disabled", enabled: false },
    });

    await ownedGroupsApi.precheckOperation(3, {
      resources: [{ resource_type: "bot", resource_id: 8 }],
    });
    await ownedGroupsApi.verifyBotProfile(8);
    await ownedGroupsApi.setBotProfileEnabled(8, false);

    expect(post).toHaveBeenNthCalledWith(
      1,
      "/owned-groups/3/operations/precheck",
      {
        resources: [{ resource_type: "bot", resource_id: 8 }],
      },
    );
    expect(post).toHaveBeenNthCalledWith(
      2,
      "/owned-groups/bot-profiles/8/verify",
    );
    expect(patch).toHaveBeenCalledWith("/owned-groups/bot-profiles/8", {
      enabled: false,
    });
  });

  it("deletes only a confirmed local failed draft through the guarded endpoint", async () => {
    deleteRequest.mockResolvedValue({ data: { deleted: true } });

    await ownedGroupsApi.deleteFailedDraft(29);

    expect(deleteRequest).toHaveBeenCalledWith("/owned-groups/29", {
      params: { confirm_no_telegram_group: true },
    });
  });
  it("queues a confirmed dissolution with an idempotency key rather than calling Telegram", async () => {
    post.mockResolvedValue({
      data: {
        id: 29,
        group_asset_id: 29,
        operation_type: "dissolve",
        status: "queued",
        planned_count: 1,
        selection_snapshot_hash: "selection",
        config_snapshot_hash: "config",
        idempotency_key: "dissolve-29-test",
        created_at: "2026-09-15T00:00:00Z",
      },
    });

    const result = await ownedGroupsApi.queueDissolution(
      29,
      { confirmation: "DISSOLVE 29" },
      "dissolve-29-test",
    );

    expect(post).toHaveBeenCalledWith(
      "/owned-groups/29/dissolution",
      { confirmation: "DISSOLVE 29" },
      { headers: { "Idempotency-Key": "dissolve-29-test" } },
    );
    expect(result).toMatchObject({
      id: 29,
      operation_type: "dissolve",
      status: "queued",
    });
  });

  it("submits an administrator dissolution verdict without contacting Telegram", async () => {
    post.mockResolvedValue({
      data: {
        code: 0,
        message: "已确认群组解散，资产归档",
        data: {
          id: 29,
          status: "archived",
          operation_id: 41,
          operation_status: "completed",
        },
      },
    });

    const result = await ownedGroupsApi.resolveDissolution(29, {
      outcome: "archived",
      confirmation: "CONFIRM DISSOLVED 29",
    });

    expect(post).toHaveBeenCalledWith("/owned-groups/29/dissolution/resolution", {
      outcome: "archived",
      confirmation: "CONFIRM DISSOLVED 29",
    });
    expect(result.data).toMatchObject({
      id: 29,
      status: "archived",
      operation_status: "completed",
    });
  });

  it("sends a known chat and replacement public username for safe recovery", async () => {
    post.mockResolvedValue({
      data: { id: 3, group_asset_id: 3, status: "ready", message: "ok" },
    });

    await ownedGroupsApi.reconcileAsset(3, -100777000111, "  available_name  ");

    expect(post).toHaveBeenCalledWith("/owned-groups/3/reconcile", {
      telegram_chat_id: -100777000111,
      telegram_username: "available_name",
    });
  });
});

describe("redactOwnedGroupError", () => {
  it("removes Bot tokens and invite URLs from messages", () => {
    const result = redactOwnedGroupError(
      "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcd failed invite=https://t.me/+privateHash",
    );

    expect(result).not.toContain("ABCDEFGHIJKLMNOPQRSTUVWXYZabcd");
    expect(result).not.toContain("t.me/+privateHash");
    expect(result).toContain("<invite-redacted>");
  });
});
