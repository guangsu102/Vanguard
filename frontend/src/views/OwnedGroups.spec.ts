import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import OwnedGroups from "./OwnedGroups.vue";

const mocks = vi.hoisted(() => {
  const asset = {
    id: 18,
    internal_name: "owned-18",
    title: "Owned Group 18",
    visibility: "private",
    owner_account_id: 7,
    invite_mode: "direct_invite",
    status: "ready",
    telegram_chat_id: -1001234567890,
    core_group_id: 311,
    governance_status: "managed",
    guardian_bot_account_id: 42,
    member_count: 3,
    created_at: "2026-09-09T00:00:00Z",
    updated_at: "2026-09-09T00:00:00Z",
  };
  const governance = {
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
    capabilities: {
      verification: true,
      sensitive_keywords: true,
      anti_spam: true,
      warn: true,
      mute: true,
      ban: true,
      announcement: true,
      pin_message: true,
      activity: true,
    },
    failure: null,
    governance_pending_at: null,
    governance_enabled_at: "2026-09-09T12:00:00Z",
    governance_last_checked_at: "2026-09-09T12:00:00Z",
    stale_pending: false,
    reused: false,
    correlation_id: "og-gov-18-test",
  };
  const select = vi.fn();
  const fetchGovernance = vi.fn().mockResolvedValue(governance);
  const fetchGovernanceCandidates = vi.fn().mockResolvedValue([
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
  const push = vi.fn();
  const listAccounts = vi.fn().mockResolvedValue({ list: [], total: 0 });
  const elementMessage = {
    info: vi.fn(),
    success: vi.fn(),
    warning: vi.fn(),
  };
  const elementMessageBox = {
    alert: vi.fn().mockResolvedValue(undefined),
    confirm: vi.fn().mockResolvedValue(undefined),
    prompt: vi.fn().mockResolvedValue({ value: "DISSOLVE 18" }),
  };
  const route = { query: { assetId: "18" } };
  const store = {
    list: [asset],
    total: 1,
    current: null,
    operation: null,
    loading: false,
    botProfiles: [],
    botProfilesTotal: 0,
    botProfilesLoading: false,
    inviteLinks: [],
    inviteLinksTotal: 0,
    inviteLinksLoading: false,
    inviteLinksAssetId: null,
    governanceByAssetId: { 18: governance },
    governanceLoadingByAssetId: {},
    governanceCandidatesByAssetId: {},
    governanceCandidatesLoadingByAssetId: {},
    fetchList: vi.fn().mockResolvedValue([asset]),
    fetchAsset: vi.fn().mockResolvedValue(asset),
    fetchInviteLinks: vi.fn().mockResolvedValue({ data: [], total: 0 }),
    revokeInviteLink: vi.fn(),
    regenerateInviteLink: vi.fn(),
    clearInviteLinks: vi.fn(),
    createDraft: vi.fn(),
    deleteFailedDraft: vi.fn(),
    queueDissolution: vi.fn(),
    resolveDissolution: vi.fn(),
    precheck: vi.fn(),
    reconcileAsset: vi.fn(),
    precheckOperation: vi.fn(),
    fetchBotProfiles: vi.fn().mockResolvedValue({ data: [], total: 0 }),
    registerBotProfile: vi.fn(),
    verifyBotProfile: vi.fn(),
    setBotProfileEnabled: vi.fn(),
    submitOperation: vi.fn(),
    refreshOperation: vi.fn(),
    controlOperation: vi.fn(),
    fetchGovernance,
    fetchGovernanceCandidates,
    bindGovernance: vi.fn(),
    reconcileGovernance: vi.fn(),
    select,
  };
  return {
    asset,
    governance,
    elementMessage,
    elementMessageBox,
    fetchGovernance,
    fetchGovernanceCandidates,
    listAccounts,
    push,
    route,
    select,
    store,
  };
});

vi.mock("vue-router", () => ({
  useRoute: () => mocks.route,
  useRouter: () => ({ push: mocks.push }),
}));

vi.mock("@/stores/ownedGroup", () => ({
  useOwnedGroupStore: () => mocks.store,
}));

vi.mock("@/stores/auth", () => ({
  useAuthStore: () => ({ userInfo: { role: "admin" } }),
}));

vi.mock("element-plus", () => ({
  ElMessage: mocks.elementMessage,
  ElMessageBox: mocks.elementMessageBox,
}));

vi.mock("@/api/accounts", () => ({
  accountsApi: {
    list: mocks.listAccounts,
  },
}));

vi.mock("@/api/guardian", () => ({
  guardianApi: {
    listBots: vi.fn().mockResolvedValue({
      data: {
        data: [
          {
            id: 9,
            account_id: 42,
            identifier: "guardian-a",
            display_name: "Guardian A",
            bot_username: "guardian_a",
          },
        ],
      },
    }),
  },
}));

const stubs = {
  "el-card": {
    template:
      "<section><header><slot name='header' /></header><slot /></section>",
  },
  "el-table": { template: "<div />" },
  "el-button": { template: "<button><slot /></button>" },
  "el-button-group": { template: "<div><slot /></div>" },
  "el-tag": { template: "<span><slot /></span>" },
  "el-tooltip": { template: "<span><slot /></span>" },
  "el-descriptions": { template: "<div><slot /></div>" },
  "el-descriptions-item": { template: "<div><slot /></div>" },
  "el-alert": { template: "<div />" },
  "el-skeleton": { template: "<div />" },
  "el-empty": { template: "<div />" },
  "el-form": { template: "<div />" },
  "el-form-item": { template: "<div />" },
  "el-input": { template: "<input />" },
  "el-select": { template: "<div />" },
  "el-option": { template: "<div />" },
  "el-option-group": { template: "<div />" },
  "el-radio-group": { template: "<div />" },
  "el-radio-button": { template: "<div />" },
  "el-table-column": { template: "<div />" },
  "el-switch": { template: "<div />" },
  "el-checkbox": { template: "<div />" },
  "el-dialog": { template: "<div />" },
  "el-progress": { template: "<div />" },
};

describe("OwnedGroups governance view", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.listAccounts.mockReset().mockImplementation((params: any) =>
      Promise.resolve({
        list:
          params?.account_type === "promoter"
            ? [
                {
                  id: 7,
                  phone: "+10000000007",
                  identifier: "owner-7",
                  account_type: "promoter",
                  is_active: true,
                  status: "online",
                  spam_check_status: "clear",
                  risk_score: 0,
                  risk_level: "normal",
                },
              ]
            : [],
        total: params?.account_type === "promoter" ? 1 : 0,
        nextCursor: null,
        hasMore: false,
      }),
    );
    mocks.store.submitOperation.mockReset();
    mocks.store.deleteFailedDraft.mockReset().mockResolvedValue(undefined);
    mocks.store.queueDissolution.mockReset().mockResolvedValue(undefined);
    mocks.store.resolveDissolution.mockReset().mockResolvedValue({
      code: 0,
      message: "ok",
      data: { id: 18, status: "ready", operation_id: 41, operation_status: "failed" },
    });
    mocks.elementMessageBox.prompt.mockReset().mockResolvedValue({ value: "DISSOLVE 18" });
    (mocks.store as any).operation = null;
    mocks.route.query.assetId = "18";
    mocks.store.governanceByAssetId[18] = mocks.governance;
  });

  it("selects the asset from the assetId query and loads governance", async () => {
    const wrapper = mount(OwnedGroups, {
      global: { stubs, directives: { loading: {} } },
    });

    await flushPromises();

    expect(mocks.select).toHaveBeenCalledWith(mocks.asset);
    expect(mocks.fetchGovernance).toHaveBeenCalledWith(18);
    expect(wrapper.text()).toContain("Guardian 治理");
    wrapper.unmount();
  });

  it("opens policies with Telegram Chat ID and owned-group context", async () => {
    const wrapper = mount(OwnedGroups, {
      global: { stubs, directives: { loading: {} } },
    });
    await flushPromises();

    (wrapper.vm as any).openGovernancePolicies();

    expect(mocks.push).toHaveBeenCalledWith({
      path: "/guardian/policies",
      query: {
        groupId: "-1001234567890",
        title: "Owned Group 18",
        botId: "42",
        source: "owned_group",
        assetId: "18",
      },
    });
    wrapper.unmount();
  });

  it("loads backend-filtered candidates before opening the bind dialog", async () => {
    mocks.store.governanceByAssetId[18] = {
      ...mocks.governance,
      governance_status: "disabled",
    };
    const wrapper = mount(OwnedGroups, {
      global: { stubs, directives: { loading: {} } },
    });
    await flushPromises();

    await (wrapper.vm as any).openGovernanceDialog();

    expect(mocks.fetchGovernanceCandidates).toHaveBeenCalledWith(18);
    wrapper.unmount();
  });

  it("opens the isolated messaging workspace for a ready mapped asset", async () => {
    const wrapper = mount(OwnedGroups, {
      global: { stubs, directives: { loading: {} } },
    });
    await flushPromises();

    (wrapper.vm as any).openOwnedGroupMessaging(mocks.asset);

    expect(mocks.push).toHaveBeenCalledWith("/owned-groups/18/messaging");
    wrapper.unmount();
  });

  it("does not open messaging for an archived asset", async () => {
    const wrapper = mount(OwnedGroups, {
      global: { stubs, directives: { loading: {} } },
    });
    await flushPromises();
    mocks.push.mockClear();

    (wrapper.vm as any).openOwnedGroupMessaging({
      ...mocks.asset,
      status: "archived",
    });

    expect(mocks.push).not.toHaveBeenCalled();
    wrapper.unmount();
  });

  it("loads up to 2,000 promoter and guardian Bot accounts", async () => {
    const wrapper = mount(OwnedGroups, {
      global: { stubs, directives: { loading: {} } },
    });
    await flushPromises();

    expect(mocks.listAccounts).toHaveBeenCalledWith({
      limit: 2000,
      account_type: "promoter",
    });
    expect(mocks.listAccounts).toHaveBeenCalledWith({
      limit: 2000,
      account_type: "guardian_bot",
    });
    wrapper.unmount();
  });

  it("loads later account pages and stops on a non-advancing cursor", async () => {
    mocks.listAccounts.mockImplementation((params: any) => {
      if (params.account_type === "guardian_bot") {
        return Promise.resolve({
          list: [],
          total: 0,
          nextCursor: null,
          hasMore: false,
        });
      }
      if (!params.cursor) {
        return Promise.resolve({
          list: [{ id: 3001 }],
          total: 4000,
          nextCursor: "3001",
          hasMore: true,
        });
      }
      return Promise.resolve({
        list: [{ id: 3001 }, { id: 3000 }],
        total: 4000,
        nextCursor: "3001",
        hasMore: true,
      });
    });
    const wrapper = mount(OwnedGroups, {
      global: { stubs, directives: { loading: {} } },
    });
    await flushPromises();

    expect(mocks.listAccounts).toHaveBeenCalledWith({
      limit: 2000,
      account_type: "promoter",
      cursor: "3001",
    });
    expect(
      (wrapper.vm as any).accounts.map((account: any) => account.id),
    ).toEqual([3001, 3000]);
    expect(
      mocks.listAccounts.mock.calls.filter(
        (args: any[]) => args[0]?.account_type === "promoter",
      ),
    ).toHaveLength(2);
    wrapper.unmount();
  });

  it("counts an unselected owner without blocking an explicit owner at the limit", async () => {
    const wrapper = mount(OwnedGroups, {
      global: { stubs, directives: { loading: {} } },
    });
    await flushPromises();

    const vm = wrapper.vm as any;
    const nonOwnerResources = Array.from(
      { length: 1999 },
      (_, index) => "bot:" + (index + 1000),
    );
    vm.selectedResources = nonOwnerResources;
    await wrapper.vm.$nextTick();

    expect(vm.implicitOwnerCount).toBe(1);
    expect(vm.finalPlannedResourceCount).toBe(2000);
    expect(vm.isResourceOptionDisabled("user:7")).toBe(false);
    expect(vm.isResourceOptionDisabled("bot:999999")).toBe(true);

    vm.selectedResources = [...nonOwnerResources, "user:7"];
    await wrapper.vm.$nextTick();

    expect(vm.ownerExplicitlySelected).toBe(true);
    expect(vm.implicitOwnerCount).toBe(0);
    expect(vm.finalPlannedResourceCount).toBe(2000);
    expect(vm.validatePlannedResourceLimit()).toBe(true);
    wrapper.unmount();
  });

  it("blocks precheck and submit when the final plan exceeds 2,000", async () => {
    const wrapper = mount(OwnedGroups, {
      global: { stubs, directives: { loading: {} } },
    });
    await flushPromises();
    mocks.store.precheckOperation.mockClear();
    mocks.store.submitOperation.mockClear();
    mocks.elementMessage.warning.mockClear();

    const vm = wrapper.vm as any;
    vm.selectedResources = Array.from(
      { length: 2000 },
      (_, index) => "bot:" + (index + 1000),
    );
    await wrapper.vm.$nextTick();

    expect(vm.finalPlannedResourceCount).toBe(2001);
    expect(vm.plannedResourceLimitExceeded).toBe(true);

    await vm.precheck();
    await vm.submit();

    expect(mocks.store.precheckOperation).not.toHaveBeenCalled();
    expect(mocks.store.submitOperation).not.toHaveBeenCalled();
    expect(mocks.elementMessage.warning).toHaveBeenCalledTimes(2);
    expect(mocks.elementMessage.warning).toHaveBeenLastCalledWith(
      "最终计划数（含群主）不能超过 2000；当前为 2001。请移除至少一个非群主资源，或显式选择群主替代自动计数。",
    );
    wrapper.unmount();
  });

  it("blocks a second batch while the current operation is active", async () => {
    (mocks.store as any).operation = { id: 30, status: "running" };
    const wrapper = mount(OwnedGroups, {
      global: { stubs, directives: { loading: {} } },
    });
    await flushPromises();
    const vm = wrapper.vm as any;
    vm.selectedResources = ["user:7"];
    vm.precheckResult = { allowed: true };
    mocks.store.precheckOperation.mockClear();
    mocks.store.submitOperation.mockClear();

    await vm.precheck();
    await vm.submit();

    expect(mocks.store.precheckOperation).not.toHaveBeenCalled();
    expect(mocks.store.submitOperation).not.toHaveBeenCalled();
    expect(mocks.elementMessage.warning).toHaveBeenCalledWith(
      "当前成员编排仍在执行，请完成或停止后再提交下一批",
    );
    wrapper.unmount();
  });

  it("clears a successful batch and preserves a failed batch for retry", async () => {
    const wrapper = mount(OwnedGroups, {
      global: { stubs, directives: { loading: {} } },
    });
    await flushPromises();
    const vm = wrapper.vm as any;

    vm.selectedResources = ["user:7", "bot:99"];
    await wrapper.vm.$nextTick();
    vm.setAdminRequired("bot:99", true);
    vm.toggleResourceConfig("bot:99");
    vm.precheckResult = { allowed: true };
    mocks.store.submitOperation.mockResolvedValueOnce({
      id: 31,
      status: "queued",
    });

    await vm.submit();

    expect(vm.selectedResources).toEqual([]);
    expect(vm.expandedResourceKeys).toEqual([]);
    expect(Object.keys(vm.resourceConfigs)).toEqual([]);
    expect(vm.precheckResult).toBeNull();

    vm.selectedResources = ["user:7", "bot:100"];
    await wrapper.vm.$nextTick();
    vm.precheckResult = { allowed: true };
    mocks.store.submitOperation.mockRejectedValueOnce(
      new Error("submit failed"),
    );

    await vm.submit();

    expect(vm.selectedResources).toEqual(["user:7", "bot:100"]);
    expect(vm.precheckResult).toEqual({ allowed: true });
    wrapper.unmount();
  });

  it("renders administrator permission controls only after expansion", async () => {
    const wrapper = mount(OwnedGroups, {
      global: {
        stubs: {
          ...stubs,
          "el-form": { template: "<div><slot /></div>" },
          "el-form-item": { template: "<div><slot /></div>" },
        },
        directives: { loading: {} },
      },
    });
    await flushPromises();

    const vm = wrapper.vm as any;
    vm.selectedResources = ["bot:99"];
    await wrapper.vm.$nextTick();
    vm.setAdminRequired("bot:99", true);
    await wrapper.vm.$nextTick();

    expect(vm.isResourceConfigExpanded("bot:99")).toBe(false);
    expect(wrapper.find(".permission-grid").exists()).toBe(false);

    vm.toggleResourceConfig("bot:99");
    await wrapper.vm.$nextTick();

    expect(vm.isResourceConfigExpanded("bot:99")).toBe(true);
    expect(wrapper.find(".permission-grid").exists()).toBe(true);
    wrapper.unmount();
  });

  it("only allows deleting an unlinked needs_attention local draft", async () => {
    const wrapper = mount(OwnedGroups, {
      global: { stubs, directives: { loading: {} } },
    });
    await flushPromises();
    const vm = wrapper.vm as any;
    const failedDraft = {
      ...mocks.asset,
      id: 29,
      status: "needs_attention",
      telegram_chat_id: null,
      core_group_id: null,
      managed_binding_id: null,
      guardian_bot_account_id: null,
      governance_status: "disabled",
    };

    expect(vm.canDeleteFailedDraft(failedDraft)).toBe(true);
    expect(
      vm.canDeleteFailedDraft({ ...failedDraft, status: "create_failed" }),
    ).toBe(false);
    expect(
      vm.canDeleteFailedDraft({ ...failedDraft, telegram_chat_id: -10029 }),
    ).toBe(false);
    expect(vm.canDeleteFailedDraft({ ...failedDraft, core_group_id: 29 })).toBe(
      false,
    );
    expect(
      vm.canDeleteFailedDraft({ ...failedDraft, managed_binding_id: 29 }),
    ).toBe(false);
    expect(
      vm.canDeleteFailedDraft({ ...failedDraft, guardian_bot_account_id: 29 }),
    ).toBe(false);
    expect(
      vm.canDeleteFailedDraft({ ...failedDraft, governance_status: "managed" }),
    ).toBe(false);

    await vm.deleteFailedDraft(failedDraft);

    expect(mocks.elementMessageBox.confirm).toHaveBeenCalledWith(
      expect.stringContaining("Telegram 中没有创建该群"),
      "删除失败草稿",
      expect.objectContaining({
        confirmButtonText: "已核实未建群，删除草稿",
      }),
    );
    expect(mocks.store.deleteFailedDraft).toHaveBeenCalledWith(29);
    expect(mocks.elementMessage.success).toHaveBeenCalledWith(
      "本地失败草稿已删除",
    );
    wrapper.unmount();
  });
  it("queues a typed-confirmation dissolution without touching the local draft delete flow", async () => {
    const wrapper = mount(OwnedGroups, {
      global: { stubs, directives: { loading: {} } },
    });
    await flushPromises();
    const vm = wrapper.vm as any;

    expect(vm.canDissolveGroup(mocks.asset)).toBe(true);
    expect(
      vm.canDissolveGroup({ ...mocks.asset, status: "needs_attention" }),
    ).toBe(false);
    expect(
      vm.canDissolveGroup({ ...mocks.asset, telegram_chat_id: null }),
    ).toBe(false);

    mocks.elementMessageBox.prompt.mockResolvedValueOnce({
      value: " dissolve 18 ",
    });

    await vm.queueDissolution(mocks.asset);

    expect(mocks.elementMessageBox.prompt).toHaveBeenCalledWith(
      expect.stringContaining("后台单线程任务"),
      "解散 Telegram 群",
      expect.objectContaining({
        confirmButtonText: "确认排队解散",
        inputPlaceholder: "DISSOLVE 18",
      }),
    );
    expect(mocks.store.queueDissolution).toHaveBeenCalledWith(
      18,
      "DISSOLVE 18",
    );
    expect(mocks.store.deleteFailedDraft).not.toHaveBeenCalled();
    expect(mocks.elementMessage.success).toHaveBeenCalledWith("解散群任务已入队");
    wrapper.unmount();
  });

  it("resolves an uncertain dissolution with a typed verdict without touching other flows", async () => {
    const wrapper = mount(OwnedGroups, {
      global: { stubs, directives: { loading: {} } },
    });
    await flushPromises();
    const vm = wrapper.vm as any;

    const reviewAsset = {
      ...mocks.asset,
      status: "needs_attention",
      pending_dissolution_review: true,
    };
    expect(vm.canResolveDissolution(reviewAsset)).toBe(true);
    expect(vm.canResolveDissolution(mocks.asset)).toBe(false);
    expect(
      vm.canResolveDissolution({
        ...reviewAsset,
        pending_dissolution_review: false,
      }),
    ).toBe(false);

    mocks.elementMessageBox.prompt.mockResolvedValueOnce({
      value: " confirm exists 18 ",
    });

    await vm.resolveDissolution(reviewAsset);

    expect(mocks.elementMessageBox.prompt).toHaveBeenCalledWith(
      expect.stringContaining("CONFIRM DISSOLVED 18"),
      "人工核验解散结果",
      expect.objectContaining({
        confirmButtonText: "提交核验结论",
      }),
    );
    expect(mocks.store.resolveDissolution).toHaveBeenCalledWith(
      18,
      "still_exists",
      "CONFIRM EXISTS 18",
    );
    expect(mocks.store.deleteFailedDraft).not.toHaveBeenCalled();
    expect(mocks.store.queueDissolution).not.toHaveBeenCalled();
    expect(mocks.elementMessage.success).toHaveBeenCalled();
    wrapper.unmount();
  });

  it("shows every promoter with phone and health while only enabling healthy accounts", async () => {
    mocks.listAccounts.mockImplementation((params: any) =>
      Promise.resolve({
        list:
          params.account_type === "promoter"
            ? [
                {
                  id: 1,
                  phone: "+10000000001",
                  display_name: "Healthy",
                  identifier: "promoter-1",
                  account_type: "promoter",
                  operation_mode: "ad_only",
                  owned_group_created_count: 3,
                  is_active: true,
                  status: "online",
                  spam_check_status: "clear",
                  risk_level: "normal",
                  risk_score: 0,
                },
                {
                  id: 2,
                  phone: "+10000000002",
                  identifier: "promoter-2",
                  account_type: "promoter",
                  is_active: true,
                  status: "restricted",
                  spam_check_status: "clear",
                  risk_level: "normal",
                },
                {
                  id: 3,
                  phone: "+10000000003",
                  identifier: "promoter-3",
                  account_type: "promoter",
                  is_active: true,
                  status: "online",
                  spam_check_status: "restricted",
                  risk_level: "normal",
                },
                {
                  id: 4,
                  phone: "+10000000004",
                  identifier: "promoter-4",
                  account_type: "promoter",
                  is_active: true,
                  status: "banned",
                  spam_check_status: "clear",
                  risk_level: "normal",
                },
                {
                  id: 5,
                  phone: "+10000000005",
                  identifier: "promoter-5",
                  account_type: "promoter",
                  is_active: true,
                  status: "online",
                  spam_check_status: "clear",
                  risk_level: "frozen",
                },
                {
                  id: 6,
                  phone: "+10000000006",
                  identifier: "promoter-6",
                  account_type: "promoter",
                  is_active: true,
                  status: "offline",
                  spam_check_status: "unknown",
                  risk_level: "normal",
                },
              ]
            : [],
        total: params.account_type === "promoter" ? 6 : 0,
        nextCursor: null,
        hasMore: false,
      }),
    );
    const wrapper = mount(OwnedGroups, {
      global: { stubs, directives: { loading: {} } },
    });
    await flushPromises();
    const vm = wrapper.vm as any;

    expect(vm.promoterOptions.map((account: any) => account.id)).toEqual([
      1, 2, 3, 4, 5, 6,
    ]);
    expect(vm.ownerOptions.map((account: any) => account.id)).toEqual([1]);
    expect(vm.accountOptionLabel(vm.promoterOptions[0])).toContain(
      "+10000000001（Healthy） · 在线 · SpamBot正常 · 风控正常",
    );
    expect(vm.ownedGroupOwnerOptionLabel(vm.promoterOptions[0])).toContain(
      "账号类型 ad_only · 已创建 3 个群",
    );
    expect(vm.accountEligibilityReason(vm.promoterOptions[1])).toContain(
      "受限",
    );
    expect(vm.accountEligibilityReason(vm.promoterOptions[2])).toContain(
      "SpamBot",
    );
    expect(vm.accountEligibilityReason(vm.promoterOptions[3])).toContain(
      "封禁",
    );
    expect(vm.accountEligibilityReason(vm.promoterOptions[4])).toContain(
      "风控",
    );
    expect(vm.accountEligibilityReason(vm.promoterOptions[5])).toContain(
      "未就绪",
    );
    expect(
      vm.isResourceOptionDisabled(
        "user:2",
        Boolean(vm.accountEligibilityReason(vm.promoterOptions[1])),
      ),
    ).toBe(true);
    wrapper.unmount();
  });

  it("rechecks account health before draft creation and member submission", async () => {
    const wrapper = mount(OwnedGroups, {
      global: { stubs, directives: { loading: {} } },
    });
    await flushPromises();
    const vm = wrapper.vm as any;
    const owner = vm.promoterOptions[0];

    owner.status = "restricted";
    vm.draft.internal_name = "blocked-draft";
    vm.draft.title = "Blocked Draft";
    vm.draft.owner_account_id = owner.id;
    mocks.store.createDraft.mockClear();
    await vm.createDraft();

    expect(mocks.store.createDraft).not.toHaveBeenCalled();

    vm.selectedResources = ["user:7"];
    vm.precheckResult = { allowed: true };
    mocks.store.submitOperation.mockClear();
    await vm.submit();

    expect(mocks.store.submitOperation).not.toHaveBeenCalled();
    expect(mocks.elementMessage.warning).toHaveBeenCalledWith(
      expect.stringContaining("Telegram 账号受限"),
    );
    wrapper.unmount();
  });
});
