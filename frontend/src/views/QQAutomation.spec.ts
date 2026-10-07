import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import QQAutomation from "./QQAutomation.vue";

const api = vi.hoisted(() => ({
  accounts: vi.fn(),
  campaigns: vi.fn(),
  bindings: vi.fn(),
  joinTasks: vi.fn(),
  schedules: vi.fn(),
  logs: vi.fn(),
  createBindings: vi.fn(),
  getCreatives: vi.fn(),
}));

vi.mock("@/api/qqAutomation", () => ({ qqAutomationApi: api }));
vi.mock("@/api/automation", () => ({
  automationApi: { getCreatives: api.getCreatives },
}));

const stubs = {
  ElTabs: { template: "<div><slot /></div>" },
  ElTabPane: { template: "<section><slot /></section>" },
  ElAlert: { props: ["title"], template: "<p>{{ title }}</p>" },
  ElButton: { template: "<button><slot /></button>" },
  ElDialog: {
    props: ["modelValue"],
    template: '<div v-if="modelValue"><slot /><slot name="footer" /></div>',
  },
  ElForm: { template: "<form @submit.prevent><slot /></form>" },
  ElFormItem: { template: "<div><slot /></div>" },
  ElSelect: {
    props: { modelValue: {}, multiple: Boolean },
    emits: ["update:modelValue"],
    template: `<select :value="modelValue" :multiple="multiple" @change="$emit('update:modelValue', multiple
      ? Array.from($event.target.selectedOptions).map(option => Number(option.value))
      : Number($event.target.value))"><option v-if="!multiple" value="">请选择</option><slot /></select>`,
  },
  ElOption: {
    props: ["value", "label"],
    template: '<option :value="value">{{ label }}</option>',
  },
  ElInputNumber: true,
  ElInput: true,
  ElSwitch: true,
  ElDivider: true,
  ElTable: true,
  ElTableColumn: true,
  ElPagination: true,
};

describe("QQ automation workspace", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    api.accounts.mockResolvedValue({
      data: {
        data: [
          {
            id: 7,
            app_id: "10001",
            display_name: "QQ sender",
            join_configured: false,
          },
        ],
      },
    });
    api.campaigns.mockResolvedValue({
      data: { data: [{ id: 8, name: "QQ plan", targets: [] }] },
    });
    api.bindings.mockResolvedValue({ data: { data: [] } });
    api.getCreatives.mockResolvedValue({
      data: { data: [{ id: 9, name: "Shared ad", enabled: true }], total: 1 },
    });
    for (const fn of [api.joinTasks, api.schedules, api.logs])
      fn.mockResolvedValue({ data: { data: [], total: 0 } });
    api.createBindings.mockResolvedValue({ data: { data: { created: 1 } } });
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.clearAllMocks();
  });

  it("shows the active-join limitation and loads QQ execution state", async () => {
    const wrapper = mount(QQAutomation, {
      global: { stubs, directives: { loading: {} } },
    });
    await flushPromises();
    expect(wrapper.text()).toContain("当前 NapCat 标准接口不支持主动申请入群");
    expect(api.accounts).toHaveBeenCalledOnce();
    expect(api.joinTasks).toHaveBeenCalledWith(0, 50);
    wrapper.unmount();
  });

  it("binds shared creatives to the selected QQ account and QQ plan", async () => {
    const wrapper = mount(QQAutomation, {
      global: { stubs, directives: { loading: {} } },
    });
    await flushPromises();
    await wrapper
      .findAll("button")
      .find((button) => button.text() === "绑定素材")!
      .trigger("click");
    const selects = wrapper.findAll("select");
    await selects[0].setValue("7");
    await selects[1].setValue("8");
    await selects[2].setValue(["9"]);
    await wrapper
      .findAll("button")
      .filter((button) => button.text() === "绑定素材")
      .at(-1)!
      .trigger("click");
    await flushPromises();
    expect(api.createBindings).toHaveBeenCalledWith({
      connection_id: 7,
      campaign_id: 8,
      creative_ids: [9],
      priority: 100,
    });
    wrapper.unmount();
  });
});
