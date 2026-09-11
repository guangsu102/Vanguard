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
    fetchGovernance,
    fetchGovernanceCandidates,
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

vi.mock("@/api/accounts", () => ({
  accountsApi: {
    list: vi.fn().mockResolvedValue({ list: [], total: 0 }),
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
});
