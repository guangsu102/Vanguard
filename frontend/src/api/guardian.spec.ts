import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
}));

vi.mock("./client", () => ({
  default: {
    get: mocks.get,
    post: mocks.post,
  },
}));

import { guardianApi, managedBotProvisionErrorMessage } from "./guardian";

describe("guardian API", () => {
  beforeEach(() => {
    mocks.get.mockReset();
    mocks.post.mockReset();
    vi.unstubAllGlobals();
  });

  it("returns the unwrapped GuardianBot detail DTO", async () => {
    const dto = {
      id: 9,
      account_id: 21,
      identifier: "guardian",
      account_type: "guardian_bot",
      status: "online",
      is_active: true,
      health_status: "healthy",
      sync_status: "synced",
      enabled: true,
      created_at: "x",
      updated_at: "y",
    };
    mocks.get.mockResolvedValue({ data: dto });

    await expect(guardianApi.getBot(9)).resolves.toEqual(dto);
    expect(mocks.get).toHaveBeenCalledWith("/guardian-bots/9");
  });

  it("normalizes capability aliases and drops unapproved credential fields", async () => {
    mocks.get.mockResolvedValue({
      data: {
        data: {
          supported: true,
          can_create: true,
          eligible_owner_accounts: [
            {
              id: 7,
              identifier: "+15550007",
              status: "online",
              session_string: "SESSION_MUST_NOT_ESCAPE",
            },
          ],
          manager_profiles: [
            {
              id: 8,
              username: "manager_bot",
              bot_can_manage_bots: true,
              bot_token: "TOKEN_MUST_NOT_ESCAPE",
            },
          ],
          blockers: [],
          token: "ROOT_TOKEN_MUST_NOT_ESCAPE",
        },
      },
    });

    const result = await guardianApi.getManagedProvisionCapability();

    expect(mocks.get).toHaveBeenCalledWith(
      "/guardian-bots/managed-provisions/capability",
    );
    expect(result).toEqual({
      supported: true,
      available: true,
      owner_accounts: [
        {
          account_id: 7,
          identifier: "+15550007",
          display_name: undefined,
          status: "online",
        },
      ],
      manager_bot_profiles: [
        {
          profile_id: 8,
          account_id: undefined,
          bot_user_id: undefined,
          bot_username: "manager_bot",
          display_name: undefined,
          can_manage_bots: true,
          enabled: true,
        },
      ],
      blockers: [],
    });
    expect(JSON.stringify(result)).not.toContain("MUST_NOT_ESCAPE");
  });

  it("creates an idempotent provision and only keeps operation whitelist fields", async () => {
    const randomUUID = vi.fn(() => "00000000-0000-4000-8000-000000000000");
    vi.stubGlobal("crypto", { randomUUID });
    mocks.post.mockResolvedValue({
      data: {
        data: {
          operation_id: "12",
          status: "QUEUED",
          current_step: "checking_username",
          requested_username: "new_guardian_bot",
          bot_token: "TOKEN_MUST_NOT_ESCAPE",
          last_error: "opaque TOKEN_MUST_NOT_ESCAPE",
        },
      },
    });

    const request = {
      owner_account_id: 7,
      manager_bot_profile_id: 8,
      display_name: "New Guardian",
      username: "new_guardian_bot",
    };
    const result = await guardianApi.createManagedProvision(request);

    expect(mocks.post).toHaveBeenCalledWith(
      "/guardian-bots/managed-provisions",
      request,
      {
        headers: {
          "Idempotency-Key": "00000000-0000-4000-8000-000000000000",
        },
      },
    );
    expect(result).toMatchObject({
      id: 12,
      status: "queued",
      step: "checking_username",
      username: "new_guardian_bot",
    });
    expect(result.error_code).toBeUndefined();
    expect(JSON.stringify(result)).not.toContain("MUST_NOT_ESCAPE");
  });

  it("polls the operation path and maps Telegram errors without echoing details", async () => {
    mocks.get.mockResolvedValue({
      data: {
        operation_id: 12,
        status: "FAILED",
        reason_code: "USERNAME_OCCUPIED",
        detail: "TOKEN_MUST_NOT_ESCAPE",
      },
    });

    const result = await guardianApi.getManagedProvision(12);

    expect(mocks.get).toHaveBeenCalledWith(
      "/guardian-bots/managed-provisions/12",
    );
    expect(result.error_code).toBe("USERNAME_OCCUPIED");
    expect(JSON.stringify(result)).not.toContain("MUST_NOT_ESCAPE");

    const message = managedBotProvisionErrorMessage({
      response: {
        data: {
          detail: "USERNAME_OCCUPIED; token=TOKEN_MUST_NOT_ESCAPE",
        },
      },
    });
    expect(message).toBe("该 Bot 用户名已被占用，请修改后重试");
    expect(message).not.toContain("MUST_NOT_ESCAPE");
  });
});
