import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, shallowMount } from "@vue/test-utils";

const mocks = vi.hoisted(() => ({ route: { query: { profileId: "9", assetId: "18" } }, push: vi.fn(), listBots: vi.fn(), getBot: vi.fn(), createBot: vi.fn(), updateBot: vi.fn() }));
vi.mock("vue-router", () => ({ useRoute: () => mocks.route, useRouter: () => ({ push: mocks.push }) }));
vi.mock("@/api/guardian", () => ({ guardianApi: { listBots: mocks.listBots, getBot: mocks.getBot, createBot: mocks.createBot, updateBot: mocks.updateBot } }));
vi.mock("@/components/ClientListPagination.vue", () => ({ default: { template: "<div />" } }));
import GuardianBots from "./GuardianBots.vue";

const bot = { id: 9, account_id: 21, identifier: "guardian", account_type: "guardian_bot", status: "online", is_active: true, health_status: "healthy", sync_status: "synced", enabled: true, created_at: "x", updated_at: "y" };
const mountView = () => shallowMount(GuardianBots, { global: { directives: { loading: {} }, stubs: { "el-button": true, "el-alert": true, "el-card": { template: "<section><slot name='header'/><slot /></section>" }, "el-descriptions": true, "el-descriptions-item": true, "el-table": true, "el-table-column": true, "el-dialog": true, "el-form": true, "el-form-item": true, "el-input": true, "el-switch": true, "el-tag": true } } });

describe("GuardianBots stage5 location", () => {
  beforeEach(() => { vi.clearAllMocks(); mocks.route.query = { profileId: "9", assetId: "18" }; mocks.listBots.mockResolvedValue({ data: { data: [], total: 0 } }); mocks.getBot.mockResolvedValue(bot); });

  it("keeps a fetched profile in a local location card without changing the list", async () => {
    const wrapper = mountView(); await flushPromises();
    expect(mocks.getBot).toHaveBeenCalledWith(9, expect.any(AbortSignal));
    expect((wrapper.vm as any).bots).toEqual([]);
    expect((wrapper.vm as any).focusedGuardianBot).toEqual(bot);
    expect((wrapper.vm as any).returnAssetId).toBe(18);
    wrapper.unmount();
  });

  it("shows a precise error and never selects a fallback row", async () => {
    mocks.listBots.mockResolvedValue({ data: { data: [{ ...bot, id: 1 }], total: 1 } });
    mocks.getBot.mockRejectedValue({ response: { status: 404 } });
    const wrapper = mountView(); await flushPromises();
    expect((wrapper.vm as any).focusedGuardianBotId).toBeNull();
    expect((wrapper.vm as any).focusedGuardianBot).toBeNull();
    expect((wrapper.vm as any).focusedGuardianBotError).toBe("Bot 配置不存在或无权限");
    wrapper.unmount();
  });
});
