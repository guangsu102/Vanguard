import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, shallowMount } from "@vue/test-utils";

const mocks = vi.hoisted(() => ({
  route: { path: "/owned-groups/18/operations", params: { assetId: "18" }, query: {} as Record<string, string> },
  push: vi.fn(), replace: vi.fn().mockResolvedValue(undefined),
  auth: { userInfo: { role: "admin" } },
  store: {
    currentAssetId: null as number | null, summary: null as any, members: [], memberTotal: 0, coverage: null,
    memberQuery: { offset: 0, limit: 50 }, operations: [], operationTotal: 0, operationQuery: { offset: 0, limit: 20 },
    auditEvents: [], auditTotal: 0, auditQuery: { offset: 0, limit: 20 },
    loading: { summary: false, members: false, operations: false, audit: false },
    error: { summary: null as any, members: null, operations: null, audit: null },
    selectAsset: vi.fn(), fetchSummary: vi.fn(), fetchMembers: vi.fn(), fetchOperations: vi.fn(), fetchAudit: vi.fn(),
    resetMemberFilters: vi.fn(), dispose: vi.fn(),
  },
  message: { warning: vi.fn(), success: vi.fn() },
}));

vi.mock("vue-router", () => ({ useRoute: () => mocks.route, useRouter: () => ({ push: mocks.push, replace: mocks.replace }) }));
vi.mock("@/stores/auth", () => ({ useAuthStore: () => mocks.auth }));
vi.mock("@/stores/ownedGroupOperations", () => ({ useOwnedGroupOperationsStore: () => mocks.store }));
vi.mock("element-plus", async (importOriginal) => ({ ...(await importOriginal<typeof import("element-plus")>()), ElMessage: mocks.message }));

import OwnedGroupOperationsCenter from "./OwnedGroupOperationsCenter.vue";

const summary = () => ({
  asset: { asset_id: 18, internal_name: "owned", title: "测试群", visibility: "private", asset_status: "ready", telegram_chat_id: -1001234567890, telegram_chat_id_raw: "-1001234567890", core_group_id: 3, managed_binding_id: 4, guardian_bot_account_id: 5, owner_account_id: 6, managed_resource_count: 3, updated_at: "x" },
  sections: Object.fromEntries(["orchestration", "governance", "members", "messaging", "activities", "audit"].map((key) => [key, { state: "ready", can_view: true, can_manage: true, can_execute: true, blocking_reasons: key === "members" ? ["member_data_scope_partial"] : [] }])),
  governance: { status: "managed", bot_account_id: 5, bot_role: "admin", health_status: "healthy", last_checked_at: null },
  messaging: { static_enabled: true, runtime_enabled: true, dry_run: false, can_send: true, policy_count: 1, enabled_policy_count: 1, pending_review_count: 0, sent_today: 0 },
  member_summary: { managed_resource_count: 3, observed_real_user_count: 2, counts_by_kind: { real_user: 2, system_ad_account: 2, system_bot: 1 }, unresolved_count: 0, conflict_count: 0, coverage: { data_scope: "managed_and_observed", is_complete: false, full_roster_supported: false, coverage_status: "collecting", observation_started_at: null, last_observed_at: null, blocking_reasons: ["telegram_full_roster_not_loaded"] } },
  latest_operation: null,
  permissions: { manage_orchestration: true, manage_governance: true, manage_messaging: true, manage_persona: true, manage_activities: true, view_members: true, view_audit: true }, snapshot_at: "x",
});

const mountView = () => shallowMount(OwnedGroupOperationsCenter, { global: { stubs: { "el-result": { props: ["title", "subTitle"], template: "<div>{{ title }} {{ subTitle }}<slot name='extra'/></div>" }, "el-skeleton": true, "el-breadcrumb": true, "el-breadcrumb-item": true, "el-button": { template: "<button><slot /></button>" }, "el-alert": { props: ["title"], template: "<div>{{ title }}</div>" }, "el-card": { template: "<section><slot name='header'/><slot /></section>" }, "el-tag": true, "el-tabs": { template: "<div><slot /></div>" }, "el-tab-pane": { template: "<section><slot /></section>" }, "el-drawer": true, OwnedGroupCapabilityCards: true, OwnedGroupMemberTable: true, OwnedGroupOperationTable: true, OwnedGroupAuditTable: true } } });

describe("OwnedGroupOperationsCenter", () => {
  beforeEach(() => {
    vi.clearAllMocks(); mocks.route.params.assetId = "18"; mocks.route.query = {}; mocks.auth.userInfo.role = "admin";
    mocks.store.summary = summary(); mocks.store.error.summary = null; mocks.store.error.members = null; mocks.store.error.operations = null; mocks.store.error.audit = null;
    mocks.store.fetchSummary.mockResolvedValue(mocks.store.summary); mocks.store.fetchMembers.mockResolvedValue({ items: [] }); mocks.store.fetchOperations.mockResolvedValue({ items: [] }); mocks.store.fetchAudit.mockResolvedValue({ items: [] });
  });

  it("loads the summary only for the default tab and renders the partial-coverage warning", async () => {
    const wrapper = mountView(); await flushPromises();
    expect(mocks.store.selectAsset).toHaveBeenCalledWith(18);
    expect(mocks.store.fetchSummary).toHaveBeenCalledWith(18);
    expect(mocks.store.fetchMembers).not.toHaveBeenCalled();
    expect(wrapper.text()).toContain("不代表 Telegram 全量实时成员名单");
    wrapper.unmount();
  });

  it("uses signed telegram_group_id and distinct asset context in governance deep link", async () => {
    const wrapper = mountView(); await flushPromises();
    await (wrapper.vm as any).capabilityAction("governance");
    expect(mocks.push).toHaveBeenCalledWith({ path: "/guardian/policies", query: { groupId: "-1001234567890", title: "测试群", botId: "5", assetId: "18" } });
    wrapper.unmount();
  });

  it("stops child requests after a summary 403", async () => {
    mocks.store.summary = null; mocks.store.fetchSummary.mockImplementation(async () => { mocks.store.error.summary = { status: 403, message: "forbidden" }; throw new Error("forbidden") });
    const wrapper = mountView(); await flushPromises();
    expect(wrapper.text()).toContain("无权查看群运营中心");
    expect(mocks.store.fetchMembers).not.toHaveBeenCalled(); expect(mocks.store.fetchOperations).not.toHaveBeenCalled(); expect(mocks.store.fetchAudit).not.toHaveBeenCalled();
    wrapper.unmount();
  });

  it("normalizes an invalid tab with router.replace", async () => {
    mocks.route.query = { tab: "unsafe" };
    const wrapper = mountView(); await flushPromises();
    expect(mocks.replace).toHaveBeenCalledWith({ path: mocks.route.path, query: { tab: "overview" } });
    wrapper.unmount();
  });

  it("does not fall back to account_id when a Telegram ID is unsafe", async () => {
    const wrapper = mountView(); await flushPromises();
    await (wrapper.vm as any).copyId({ telegram_user_id: null, telegram_user_id_raw: "9007199254740993", account_id: 21, guardian_bot_profile_id: null });
    expect(mocks.message.warning).toHaveBeenCalledWith(expect.stringContaining("Telegram ID 超出"));
    wrapper.unmount();
  });
});
