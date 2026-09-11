import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import ManagedGroups from "./ManagedGroups.vue";

const { push, listManagedGroups, listBots, getPinnedMessageConfig, route } = vi.hoisted(() => ({
  push: vi.fn(),
  listManagedGroups: vi.fn().mockResolvedValue({
    data: { data: [], total: 0 },
  }),
  listBots: vi.fn().mockResolvedValue({
    data: { data: [], total: 0 },
  }),
  getPinnedMessageConfig: vi.fn().mockResolvedValue({ data: { data: { enabled: true, content: "", parse_mode: "", disable_web_page_preview: false, disable_notification: true } } }),
  route: { query: {} as Record<string, string> },
}));

vi.mock("vue-router", () => ({
  useRouter: () => ({ push }),
  useRoute: () => route,
}));

vi.mock("@/api/guardian", () => ({
  guardianApi: {
    listManagedGroups,
    listBots,
    getPinnedMessageConfig,
  },
}));

vi.mock("@/api/accounts", () => ({
  accountsApi: {
    list: vi.fn().mockResolvedValue({ list: [], total: 0 }),
  },
}));

vi.mock("@/components/ClientListPagination.vue", () => ({
  default: { template: "<div />" },
}));

const stubs = {
  "el-table": { template: "<div />" },
  "el-table-column": { template: "<div />" },
  "el-dialog": { template: "<div />" },
  "el-select": { template: "<div />" },
  "el-option": { template: "<div />" },
  "el-radio-group": { template: "<div />" },
  "el-radio-button": { template: "<div />" },
  "el-button": { template: "<button><slot /></button>" },
};

describe("ManagedGroups source navigation", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    route.query = {};
    listManagedGroups.mockResolvedValue({ data: { data: [], total: 0 } });
  });

  it("returns an owned binding to its owned-group asset", async () => {
    const wrapper = mount(ManagedGroups, {
      global: { stubs, directives: { loading: {} } },
    });
    await flushPromises();

    (wrapper.vm as any).openOwnedGroup({
      source_type: "owned_group",
      owned_group_asset_id: 18,
    });

    expect(push).toHaveBeenCalledWith({
      path: "/owned-groups",
      query: { assetId: "18" },
    });
    wrapper.unmount();
  });

  it("opens the pinned-message dialog only for the binding matching assetId", async () => {
    route.query = { assetId: "18", action: "pinned-message" };
    listManagedGroups.mockResolvedValue({ data: { data: [{ id: 7, owned_group_asset_id: 18, telegram_group_id: -1001, source_type: "owned_group", chat_type: "supergroup", username: null, title: "Owned", bot_account_id: 3 }], total: 1 } });
    const wrapper = mount(ManagedGroups, { global: { stubs, directives: { loading: {} } } });
    await flushPromises();
    expect(getPinnedMessageConfig).toHaveBeenCalledWith(7);
    expect((wrapper.vm as any).currentPinnedGroup.id).toBe(7);
    expect((wrapper.vm as any).pinnedDialogVisible).toBe(true);
    wrapper.unmount();
  });
});
