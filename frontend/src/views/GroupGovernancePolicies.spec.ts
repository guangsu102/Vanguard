import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import GroupGovernancePolicies from "./GroupGovernancePolicies.vue";

const api = vi.hoisted(() => ({
  listManagedGroups: vi.fn(),
  getVerificationPolicy: vi.fn(),
  getModerationPolicy: vi.fn(),
  getPunishmentPolicy: vi.fn(),
  saveVerificationPolicy: vi.fn(),
  saveModerationPolicy: vi.fn(),
  savePunishmentPolicy: vi.fn(),
}));

const route = vi.hoisted(() => ({
  query: {
    groupId: "-1001234567890",
    title: "Owned Group 18",
    source: "owned_group",
    assetId: "18",
  },
}));

vi.mock("vue-router", () => ({
  useRoute: () => route,
  useRouter: () => ({ push: vi.fn() }),
}));

vi.mock("@/api/guardian", () => ({
  guardianApi: api,
}));

const stubs = {
  "el-card": {
    template:
      "<section><header><slot name='header' /></header><slot /></section>",
  },
  "el-form": { template: "<div><slot /></div>" },
  "el-form-item": { template: "<div><slot /></div>" },
  "el-select": { template: "<div><slot /></div>" },
  "el-option": { template: "<div />" },
  "el-button": { template: "<button><slot /></button>" },
  "el-input": { template: "<input />" },
  "el-input-number": { template: "<input />" },
  "el-switch": { template: "<input type='checkbox' />" },
};

describe("GroupGovernancePolicies route", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.listManagedGroups.mockResolvedValue({
      data: {
        data: [
          {
            id: 27,
            telegram_group_id: -1001234567890,
            title: "Owned Group 18",
          },
        ],
      },
    });
    api.getVerificationPolicy.mockResolvedValue({ data: { data: {} } });
    api.getModerationPolicy.mockResolvedValue({ data: { data: {} } });
    api.getPunishmentPolicy.mockResolvedValue({ data: { data: {} } });
  });

  it("accepts a negative Telegram supergroup ID from the route", async () => {
    const wrapper = mount(GroupGovernancePolicies, {
      global: {
        stubs,
        directives: { loading: {} },
      },
    });
    await flushPromises();

    expect(api.getVerificationPolicy).toHaveBeenCalledWith(-1001234567890);
    expect(api.getModerationPolicy).toHaveBeenCalledWith(-1001234567890);
    expect(api.getPunishmentPolicy).toHaveBeenCalledWith(-1001234567890);
    expect(wrapper.text()).toContain("Owned Group 18");
    wrapper.unmount();
  });
});
