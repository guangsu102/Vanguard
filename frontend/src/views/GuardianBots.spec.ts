import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, shallowMount } from "@vue/test-utils";

const mocks = vi.hoisted(() => ({
  route: { query: { profileId: "9", assetId: "18" } as Record<string, string> },
  push: vi.fn(),
  auth: { userInfo: { role: "admin" } },
  listBots: vi.fn(),
  getBot: vi.fn(),
  createBot: vi.fn(),
  updateBot: vi.fn(),
  getManagedProvisionCapability: vi.fn(),
  createManagedProvision: vi.fn(),
  getManagedProvision: vi.fn(),
  managedBotProvisionErrorMessage: vi.fn(() => "一键创建 Bot 失败，请稍后重试"),
}));

vi.mock("vue-router", () => ({
  useRoute: () => mocks.route,
  useRouter: () => ({ push: mocks.push }),
}));
vi.mock("@/stores/auth", () => ({
  useAuthStore: () => mocks.auth,
}));
vi.mock("@/api/guardian", () => ({
  guardianApi: {
    listBots: mocks.listBots,
    getBot: mocks.getBot,
    createBot: mocks.createBot,
    updateBot: mocks.updateBot,
    getManagedProvisionCapability: mocks.getManagedProvisionCapability,
    createManagedProvision: mocks.createManagedProvision,
    getManagedProvision: mocks.getManagedProvision,
  },
  managedBotProvisionErrorMessage: mocks.managedBotProvisionErrorMessage,
}));
vi.mock("@/components/ClientListPagination.vue", () => ({
  default: { template: "<div />" },
}));

import GuardianBots from "./GuardianBots.vue";

const bot = {
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

const capability = {
  supported: true,
  available: true,
  owner_accounts: [
    {
      account_id: 7,
      identifier: "+15550007",
      status: "online",
    },
  ],
  manager_bot_profiles: [
    {
      profile_id: 8,
      bot_username: "manager_bot",
      can_manage_bots: true,
      enabled: true,
    },
  ],
  blockers: [],
};

const mountView = () =>
  shallowMount(GuardianBots, {
    global: {
      directives: { loading: {} },
      stubs: {
        "el-button": true,
        "el-alert": true,
        "el-card": {
          template: "<section><slot name='header'/><slot /></section>",
        },
        "el-descriptions": true,
        "el-descriptions-item": true,
        "el-table": true,
        "el-table-column": true,
        "el-dialog": true,
        "el-form": true,
        "el-form-item": true,
        "el-input": true,
        "el-select": true,
        "el-option": true,
        "el-switch": true,
        "el-tag": true,
      },
    },
  });

describe("GuardianBots stage5 location and managed provisioning", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.route.query = { profileId: "9", assetId: "18" };
    mocks.auth.userInfo.role = "admin";
    mocks.listBots.mockResolvedValue({ data: { data: [], total: 0 } });
    mocks.getBot.mockResolvedValue(bot);
    mocks.getManagedProvisionCapability.mockResolvedValue(capability);
  });

  it("keeps a fetched profile in a local location card without changing the list", async () => {
    const wrapper = mountView();
    await flushPromises();

    expect(mocks.getBot).toHaveBeenCalledWith(9, expect.any(AbortSignal));
    expect((wrapper.vm as any).bots).toEqual([]);
    expect((wrapper.vm as any).focusedGuardianBot).toEqual(bot);
    expect((wrapper.vm as any).returnAssetId).toBe(18);
    wrapper.unmount();
  });

  it("shows a precise error and never selects a fallback row", async () => {
    mocks.listBots.mockResolvedValue({
      data: { data: [{ ...bot, id: 1 }], total: 1 },
    });
    mocks.getBot.mockRejectedValue({ response: { status: 404 } });
    const wrapper = mountView();
    await flushPromises();

    expect((wrapper.vm as any).focusedGuardianBotId).toBeNull();
    expect((wrapper.vm as any).focusedGuardianBot).toBeNull();
    expect((wrapper.vm as any).focusedGuardianBotError).toBe(
      "Bot 配置不存在或无权限",
    );
    wrapper.unmount();
  });

  it("only opens managed provisioning for administrators", async () => {
    mocks.route.query = {};
    mocks.auth.userInfo.role = "operator";
    const wrapper = mountView();
    await flushPromises();

    await (wrapper.vm as any).openManagedCreation();

    expect((wrapper.vm as any).managedDialogVisible).toBe(false);
    expect(mocks.getManagedProvisionCapability).not.toHaveBeenCalled();
    wrapper.unmount();
  });

  it("keeps manual import and enabled-state changes read-only for non-admins", async () => {
    mocks.route.query = {};
    mocks.auth.userInfo.role = "operator";
    const wrapper = mountView();
    await flushPromises();

    expect(wrapper.find('[data-testid="import-existing-bot"]').exists()).toBe(
      false,
    );

    (wrapper.vm as any).form.identifier = "existing_guardian_bot";
    (wrapper.vm as any).form.bot_token = "123456:test-token";
    await (wrapper.vm as any).createBot();
    await (wrapper.vm as any).toggleBot(bot);

    expect(mocks.createBot).not.toHaveBeenCalled();
    expect(mocks.updateBot).not.toHaveBeenCalled();
    wrapper.unmount();
  });

  it("creates, polls every two seconds, drops token fields, and refreshes the list", async () => {
    vi.useFakeTimers();
    mocks.route.query = {};
    mocks.createManagedProvision.mockResolvedValue({
      id: 31,
      status: "queued",
      step: "queued",
      username: "new_guardian_bot",
      bot_token: "TOKEN_MUST_NOT_ESCAPE",
    });
    mocks.getManagedProvision.mockResolvedValue({
      id: 31,
      status: "succeeded",
      step: "completed",
      username: "new_guardian_bot",
      guardian_bot_profile_id: 41,
      bot_token: "TOKEN_MUST_NOT_ESCAPE",
    });
    const consoleLog = vi
      .spyOn(console, "log")
      .mockImplementation(() => undefined);
    const wrapper = mountView();
    await flushPromises();

    await (wrapper.vm as any).openManagedCreation();
    expect((wrapper.vm as any).managedForm.owner_account_id).toBe(7);
    expect((wrapper.vm as any).managedForm.manager_bot_profile_id).toBe(8);
    (wrapper.vm as any).managedForm.display_name = "New Guardian";
    (wrapper.vm as any).managedForm.username = "new_guardian_bot";

    await (wrapper.vm as any).submitManagedProvision();

    expect(mocks.createManagedProvision).toHaveBeenCalledWith({
      owner_account_id: 7,
      manager_bot_profile_id: 8,
      display_name: "New Guardian",
      username: "new_guardian_bot",
    });
    expect(JSON.stringify((wrapper.vm as any).managedOperation)).not.toContain(
      "MUST_NOT_ESCAPE",
    );
    expect(mocks.getManagedProvision).not.toHaveBeenCalled();

    (wrapper.vm as any).hideManagedDialog();
    expect((wrapper.vm as any).managedDialogVisible).toBe(false);
    (wrapper.vm as any).stopManagedPolling();
    await (wrapper.vm as any).openManagedCreation();

    expect((wrapper.vm as any).managedDialogVisible).toBe(true);
    expect((wrapper.vm as any).managedOperation.id).toBe(31);
    expect(mocks.getManagedProvisionCapability).toHaveBeenCalledTimes(1);
    expect(mocks.createManagedProvision).toHaveBeenCalledTimes(1);

    await vi.advanceTimersByTimeAsync(1999);
    expect(mocks.getManagedProvision).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    await flushPromises();

    expect(mocks.getManagedProvision).toHaveBeenCalledWith(31);
    expect((wrapper.vm as any).managedOperation.status).toBe("succeeded");
    expect(mocks.listBots).toHaveBeenCalledTimes(2);
    expect(JSON.stringify((wrapper.vm as any).managedOperation)).not.toContain(
      "MUST_NOT_ESCAPE",
    );
    expect(consoleLog).not.toHaveBeenCalled();

    wrapper.unmount();
    consoleLog.mockRestore();
    vi.useRealTimers();
  });

  it("rejects an invalid managed username before creating an operation", async () => {
    mocks.route.query = {};
    const wrapper = mountView();
    await flushPromises();
    await (wrapper.vm as any).openManagedCreation();

    (wrapper.vm as any).managedForm.display_name = "New Guardian";
    (wrapper.vm as any).managedForm.username = "not-valid!";

    await (wrapper.vm as any).submitManagedProvision();

    expect(mocks.createManagedProvision).not.toHaveBeenCalled();
    expect((wrapper.vm as any).managedError).toContain("5–32");
    wrapper.unmount();
  });
});
