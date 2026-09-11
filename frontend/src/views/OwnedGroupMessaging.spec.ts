import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, shallowMount } from "@vue/test-utils";

const mocks = vi.hoisted(() => {
  const message = {
    success: vi.fn(),
    warning: vi.fn(),
    error: vi.fn(),
  };
  const asset = {
    id: 18,
    title: "Owned Group 18",
    status: "ready",
    core_group_id: 311,
    telegram_chat_id: -1001234567890,
    governance_status: "managed",
  };
  const policy = {
    id: 8,
    owned_group_asset_id: 18,
    core_group_id: 311,
    account_id: 21,
    account_display_name: "运营号 A",
    account_eligible: true,
    mode: "ai",
    default_template_id: null,
    trigger_config: {
      version: 1,
      scheduled: {
        enabled: false,
        timezone: "Asia/Shanghai",
        weekdays: [1, 2, 3, 4, 5, 6, 7],
        times: [],
        jitter_seconds: 0,
        content_category: "community",
      },
      keyword: {
        enabled: false,
        trigger_ids: [],
        reply_to_source: true,
        content_category: "community",
      },
      reply: {
        enabled: false,
        strategy: "directed",
        semantic_min_confidence: 0.75,
        context_messages: 6,
        content_category: "community",
      },
      manual: {
        enabled: true,
        allowed_content_categories: ["community", "promotion"],
      },
      dedupe_window_seconds: 21600,
    },
    promotion_config: {
      mode: "off",
      default_template_id: null,
      destination_url: null,
      cta_text: null,
    },
    daily_limit: 5,
    cooldown_seconds: 3600,
    allowed_topics: ["客户端设置"],
    require_review: true,
    enabled: false,
    revision: 1,
    persona: {
      account_id: 21,
      configured: true,
      name: "稳健顾问",
      revision: 7,
      applicable: true,
      effective_enabled: true,
    },
  };
  const store = {
    activeAssetId: 18,
    asset,
    summary: {
      asset_id: 18,
      title: "Owned Group 18",
      status: "ready",
      core_group_id: 311,
      telegram_chat_id: -1001234567890,
      governance_status: "managed",
      messaging_status: {
        static_enabled: true,
        runtime_enabled: true,
        dry_run: false,
      },
      runtime_limits: { global_max_per_group_per_day: 20 },
      stats: {
        group_sent_today: 2,
        community_sent_today: 1,
        promotion_sent_today: 1,
      },
    },
    eligibleAccounts: [
      {
        account_id: 21,
        display_name: "运营号 A",
        status: "online",
        risk_level: "normal",
        operation_mode: "growth",
        membership_status: "member_verified",
        last_verified_at: "2026-09-10T00:00:00Z",
        eligible: true,
        blocking_reasons: [],
        policy_id: 8,
      },
      {
        account_id: 22,
        display_name: "不可用账号",
        status: "error",
        risk_level: "limited",
        operation_mode: "growth",
        membership_status: "left",
        last_verified_at: null,
        eligible: false,
        blocking_reasons: ["account_error", "not_in_group"],
        policy_id: null,
      },
    ],
    policies: [policy],
    policyTotal: 1,
    reviewItems: [],
    reviewTotal: 0,
    history: [],
    historyTotal: 0,
    historyPage: 1,
    historyPageSize: 20,
    executionDetail: null,
    templates: [],
    templateTotal: 0,
    previewResult: null,
    loading: {
      workspace: false,
      policies: false,
      eligible: false,
      reviews: false,
      history: false,
      detail: false,
      mutation: false,
      preview: false,
      templates: false,
    },
    loadWorkspace: vi.fn().mockResolvedValue([]),
    reset: vi.fn(),
    fetchEligibleAccounts: vi.fn(),
    fetchPolicies: vi.fn(),
    fetchReviews: vi.fn().mockResolvedValue(undefined),
    fetchHistory: vi.fn().mockResolvedValue(undefined),
    fetchTemplates: vi.fn(),
    createPolicy: vi.fn(),
    replacePolicy: vi.fn(),
    previewPolicy: vi.fn(),
    createManualExecution: vi.fn().mockResolvedValue({
      execution_id: 101,
      status: "pending_review",
      correlation_id: "msg-101",
    }),
    fetchExecutionDetail: vi.fn().mockResolvedValue(undefined),
    approveExecution: vi.fn(),
    rejectExecution: vi.fn(),
    createTemplate: vi.fn(),
    updateTemplate: vi.fn(),
  };
  return {
    message,
    route: { params: { assetId: "18" } },
    push: vi.fn(),
    auth: { userInfo: { role: "admin" } },
    asset,
    policy,
    store,
  };
});

vi.mock("element-plus", async (importOriginal) => {
  const actual = await importOriginal<typeof import("element-plus")>();
  return {
    ...actual,
    ElMessage: mocks.message,
    ElMessageBox: {
      confirm: vi.fn().mockResolvedValue("confirm"),
    },
  };
});

vi.mock("vue-router", () => ({
  useRoute: () => mocks.route,
  useRouter: () => ({ push: mocks.push }),
}));

vi.mock("@/stores/auth", () => ({
  useAuthStore: () => mocks.auth,
}));

vi.mock("@/stores/ownedGroupMessaging", () => ({
  useOwnedGroupMessagingStore: () => mocks.store,
}));

vi.mock("@/api/acquisition", () => ({
  acquisitionApi: {
    getKeywordTriggers: vi.fn().mockResolvedValue({ data: { data: [] } }),
  },
}));

import OwnedGroupMessaging from "./OwnedGroupMessaging.vue";

const stubs = {
  "el-button": { template: "<button><slot /></button>" },
  "el-alert": { template: "<div />" },
  "el-card": { template: "<section><slot /></section>" },
  "el-descriptions": { template: "<div><slot /></div>" },
  "el-descriptions-item": { template: "<div><slot /></div>" },
  "el-tag": { template: "<span><slot /></span>" },
  "el-tooltip": { template: "<span><slot /></span>" },
  "el-table": { template: "<div><slot /></div>" },
  "el-table-column": { template: "<div />" },
  "el-divider": { template: "<div><slot /></div>" },
  "el-tabs": { template: "<div><slot /></div>" },
  "el-tab-pane": { template: "<section><slot /></section>" },
  "el-form": { template: "<form><slot /></form>" },
  "el-form-item": { template: "<div><slot /></div>" },
  "el-input": { template: "<input />" },
  "el-input-number": { template: "<input />" },
  "el-select": { template: "<div><slot /></div>" },
  "el-option": { template: "<div />" },
  "el-radio-group": { template: "<div><slot /></div>" },
  "el-radio": { template: "<span><slot /></span>" },
  "el-radio-button": { template: "<span><slot /></span>" },
  "el-checkbox-group": { template: "<div><slot /></div>" },
  "el-checkbox": { template: "<span><slot /></span>" },
  "el-switch": { template: "<div />" },
  "el-date-picker": { template: "<div />" },
  "el-pagination": { template: "<div />" },
  "el-drawer": { template: "<aside><slot /><slot name='footer' /></aside>" },
  "el-dialog": { template: "<div><slot /><slot name='footer' /></div>" },
  "el-skeleton": { template: "<div />" },
  "el-timeline": { template: "<div><slot /></div>" },
  "el-timeline-item": { template: "<div><slot /></div>" },
};

const mountView = () =>
  shallowMount(OwnedGroupMessaging, {
    global: {
      directives: { loading: {} },
      stubs: {
        ...stubs,
        teleport: true,
        transition: false,
      },
    },
  });

describe("OwnedGroupMessaging", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.auth.userInfo.role = "admin";
    (mocks.store.policies as any[]) = [mocks.policy];
    (mocks.store.templates as any[]) = [];
    Object.assign(mocks.store.summary.messaging_status, {
      static_enabled: true,
      runtime_enabled: true,
      dry_run: false,
    });
    mocks.store.summary.runtime_limits = {
      global_group_daily_limit: 20,
    } as any;
    mocks.store.summary.stats = {
      group_sent_today: 2,
      community_sent_today: 1,
      promotion_sent_today: 1,
    } as any;
    mocks.store.previewResult = null;
    mocks.store.executionDetail = null;
    mocks.store.replacePolicy.mockReset();
    mocks.store.fetchPolicies.mockReset();
    mocks.store.fetchPolicies.mockResolvedValue({
      data: [mocks.policy],
      total: 1,
    });
    mocks.store.approveExecution.mockReset();
    mocks.store.approveExecution.mockResolvedValue(undefined);
    mocks.store.fetchReviews.mockResolvedValue(undefined);
    mocks.store.fetchExecutionDetail.mockReset();
    mocks.store.fetchExecutionDetail.mockResolvedValue(undefined);
  });

  it("loads the asset-scoped workspace and keeps the signed Telegram chat id", async () => {
    const wrapper = mountView();
    await flushPromises();

    expect(mocks.store.loadWorkspace).toHaveBeenCalledWith(18);
    expect((wrapper.vm as any).telegramChatId).toBe(-1001234567890);
    expect((wrapper.vm as any).activeTab).toBe("policies");
    wrapper.unmount();
  });

  it("shows policy and execution Persona source state and links to account settings", async () => {
    const wrapper = mountView();
    await flushPromises();
    const vm = wrapper.vm as any;
    const execution = {
      account_id: 21,
      mode_snapshot: "ai",
      persona: {
        source: "neutral_default",
        name: "中性默认",
        revision: 0,
        hash_prefix: "abc123def456",
      },
    };

    expect(vm.policyPersonaName(mocks.policy)).toBe("稳健顾问");
    expect(vm.policyPersonaMeta(mocks.policy)).toBe("账号配置 · v7");
    expect(vm.policyPersonaStatus(mocks.policy)).toEqual({
      label: "已应用",
      type: "success",
    });
    expect(vm.executionPersonaName(execution)).toBe("中性默认");
    expect(vm.executionPersonaMeta(execution)).toBe("中性默认 · v0");
    expect(vm.executionPersonaStatus(execution)).toEqual({
      label: "已应用",
      type: "success",
    });

    vm.goToAccountPersona(21);
    expect(mocks.push).toHaveBeenCalledWith({
      name: "Accounts",
      query: { tab: "list", persona_account_id: "21" },
    });
    wrapper.unmount();
  });

  it("marks template executions as Persona-inapplicable", async () => {
    const wrapper = mountView();
    await flushPromises();
    const vm = wrapper.vm as any;
    const execution = {
      account_id: 21,
      mode_snapshot: "template",
      persona: null,
    };

    expect(vm.executionPersonaName(execution)).toBe("不适用");
    expect(vm.executionPersonaMeta(execution)).toBe("模板执行 · Persona 未使用");
    expect(vm.executionPersonaStatus(execution)).toEqual({
      label: "不适用",
      type: "info",
    });
    wrapper.unmount();
  });

  it("makes policy mutations unavailable to a non-admin while retaining preview access", async () => {
    mocks.auth.userInfo.role = "auditor";
    const wrapper = mountView();
    await flushPromises();

    expect((wrapper.vm as any).isAdmin).toBe(false);
    (wrapper.vm as any).openCreatePolicy();
    expect((wrapper.vm as any).policyDrawerVisible).toBe(false);
    (wrapper.vm as any).openMessageAction(mocks.policy, "preview");
    expect((wrapper.vm as any).actionDialogVisible).toBe(true);
    wrapper.unmount();
  });

  it("shows only the backend-provided redacted execution summary to a non-admin", async () => {
    mocks.auth.userInfo.role = "auditor";
    mocks.store.executionDetail = {
      id: 101,
      policy_id: 8,
      owned_group_asset_id: 18,
      core_group_id: 311,
      telegram_chat_id: -1001234567890,
      account_id: 21,
      trigger_type: "manual",
      message_purpose: "template",
      content_category: "community",
      mode_snapshot: "template",
      policy_revision: 1,
      status: "failed",
      correlation_id: "msg-101",
      attempt_count: 1,
      revision: 2,
      content: null,
      content_summary: "脱敏后的消息摘要",
      error_message: "脱敏后的失败原因",
      created_at: "2026-09-10T00:00:00Z",
      updated_at: "2026-09-10T00:00:00Z",
    } as any;
    const wrapper = mountView();
    await flushPromises();

    expect(wrapper.text()).toContain("脱敏后的消息摘要");
    expect(wrapper.text()).toContain("脱敏后的失败原因");
    expect(wrapper.text()).not.toContain("详细错误仅管理员可见");
    wrapper.unmount();
  });

  it("blocks AI scheduled policies without an allowed topic", async () => {
    const wrapper = mountView();
    await flushPromises();
    const vm = wrapper.vm as any;
    vm.openCreatePolicy();
    vm.policyForm.account_id = 21;
    vm.policyForm.mode = "ai";
    vm.policyForm.trigger_config.scheduled.enabled = true;
    vm.policyForm.trigger_config.scheduled.times = ["10:30"];
    vm.topicInput = "";

    expect(vm.policyValidationError()).toContain("允许主题");
    wrapper.unmount();
  });

  it("fails closed when runtime switches are absent", async () => {
    delete (mocks.store.summary.messaging_status as any).static_enabled;
    delete (mocks.store.summary.messaging_status as any).runtime_enabled;
    const wrapper = mountView();
    await flushPromises();

    expect((wrapper.vm as any).staticEnabled).toBe(false);
    expect((wrapper.vm as any).runtimeEnabled).toBe(false);
    expect((wrapper.vm as any).sendBlockers).toEqual(
      expect.arrayContaining(["静态消息开关已关闭", "运行消息开关已关闭"]),
    );
    wrapper.unmount();
  });

  it("renders the backend runtime-limit names and aggregates category totals when summary detail is absent", async () => {
    mocks.store.summary.runtime_limits = {
      global_group_daily_limit: 20,
      global_account_daily_limit: 30,
    } as any;
    mocks.store.summary.stats = { sent_today: 7 } as any;
    (mocks.store.policies as any[]) = [
      {
        ...mocks.policy,
        id: 8,
        community_sent_today: 2,
        promotion_sent_today: 1,
        group_sent_today: 7,
        last_sent_at: "2026-09-10T08:00:00Z",
      },
      {
        ...mocks.policy,
        id: 9,
        community_sent_today: 1,
        promotion_sent_today: 3,
        group_sent_today: 7,
        last_sent_at: "2026-09-10T09:00:00Z",
      },
    ];
    const wrapper = mountView();
    await flushPromises();
    const vm = wrapper.vm as any;

    expect(vm.globalGroupDailyLimit).toBe(20);
    expect(vm.groupSentToday).toBe(7);
    expect(vm.communitySentToday).toBe(3);
    expect(vm.promotionSentToday).toBe(4);
    expect(vm.lastSentAt).toBe("2026-09-10T09:00:00Z");
    wrapper.unmount();
  });

  it("rejects enabled triggers for an off category and credential-bearing URLs", async () => {
    const wrapper = mountView();
    await flushPromises();
    const vm = wrapper.vm as any;
    vm.openCreatePolicy();
    vm.policyForm.account_id = 21;
    vm.policyForm.mode = "off";
    vm.policyForm.trigger_config.scheduled.enabled = true;
    vm.policyForm.trigger_config.scheduled.times = ["10:30"];

    expect(vm.policyValidationError()).toContain("普通消息模式不能关闭");

    vm.policyForm.mode = "ai";
    vm.topicInput = "客户端设置";
    vm.policyForm.promotion_config.destination_url =
      "https://operator:secret@example.com/promo";
    expect(vm.policyValidationError()).toContain("不含账号密码");
    wrapper.unmount();
  });

  it("reports a 202 result as queued for review and never as sent", async () => {
    const wrapper = mountView();
    await flushPromises();
    const vm = wrapper.vm as any;
    vm.openMessageAction(mocks.policy, "manual");

    await vm.createManualExecution();

    expect(mocks.store.createManualExecution).toHaveBeenCalled();
    expect(mocks.message.success).toHaveBeenCalledWith("已进入待审核队列");
    expect(mocks.message.success).not.toHaveBeenCalledWith(
      expect.stringContaining("发送成功"),
    );
    wrapper.unmount();
  });

  it("refreshes authoritative review state on a revision conflict", async () => {
    const conflict = {
      response: {
        status: 409,
        data: {
          error: {
            code: "EXECUTION_REVISION_CONFLICT",
            message: "审核版本冲突",
            details: {},
            retryable: false,
          },
          correlation_id: "msg-conflict",
        },
      },
    };
    mocks.store.approveExecution.mockRejectedValue(conflict);
    const wrapper = mountView();
    await flushPromises();
    const vm = wrapper.vm as any;
    vm.reviewItem = {
      id: 101,
      revision: 2,
      status: "pending_review",
      content: "待审核内容",
    };
    vm.reviewMode = "approve";

    await vm.submitReview();

    expect(mocks.store.fetchReviews).toHaveBeenCalledWith(18);
    expect(mocks.store.fetchExecutionDetail).toHaveBeenCalledWith(18, 101);
    expect(mocks.message.warning).toHaveBeenCalledWith(
      "内容已被其他管理员处理，已刷新最新状态",
    );
    wrapper.unmount();
  });

  it("loads authoritative full content and revision before allowing review", async () => {
    const detail = {
      ...mocks.policy,
      id: 101,
      policy_id: 8,
      owned_group_asset_id: 18,
      core_group_id: 311,
      telegram_chat_id: -1001234567890,
      account_id: 21,
      trigger_type: "manual",
      message_purpose: "template",
      content_category: "promotion",
      mode_snapshot: "template",
      policy_revision: 1,
      status: "pending_review",
      correlation_id: "msg-101",
      attempt_count: 0,
      revision: 4,
      content: "完整待审核内容 https://example.com/promo",
      review_expires_at: "2099-09-10T12:00:00Z",
      created_at: "2026-09-10T00:00:00Z",
      updated_at: "2026-09-10T00:00:00Z",
    };
    mocks.store.fetchExecutionDetail.mockResolvedValue(detail);
    const wrapper = mountView();
    await flushPromises();
    const vm = wrapper.vm as any;

    await vm.openReview(
      { ...detail, revision: 1, content: null, content_summary: "摘要" },
      "edit",
    );

    expect(mocks.store.fetchExecutionDetail).toHaveBeenCalledWith(18, 101);
    expect(vm.reviewItem.revision).toBe(4);
    expect(vm.reviewContent).toBe("完整待审核内容 https://example.com/promo");
    expect(vm.reviewLoading).toBe(false);
    wrapper.unmount();
  });

  it("replaces a stale policy form with the authoritative revision after conflict", async () => {
    const conflict = {
      response: {
        status: 409,
        data: {
          error: {
            code: "POLICY_REVISION_CONFLICT",
            message: "策略版本冲突",
            details: { revision: 2 },
            retryable: false,
          },
        },
      },
    };
    const latest = { ...mocks.policy, revision: 2, daily_limit: 9 };
    mocks.store.replacePolicy.mockRejectedValue(conflict);
    mocks.store.fetchPolicies.mockImplementation(async () => {
      (mocks.store.policies as any[]) = [latest];
      return { data: [latest], total: 1 };
    });
    const wrapper = mountView();
    await flushPromises();
    const vm = wrapper.vm as any;
    vm.openEditPolicy(mocks.policy);
    vm.policyForm.daily_limit = 8;

    await vm.savePolicy();

    expect(mocks.store.fetchPolicies).toHaveBeenCalledWith(18);
    expect(vm.policyForm.revision).toBe(2);
    expect(vm.policyForm.daily_limit).toBe(9);
    expect(vm.policyDirty).toBe(false);
    wrapper.unmount();
  });

  it("shows quota context for nested 429 errors", async () => {
    const wrapper = mountView();
    await flushPromises();
    const vm = wrapper.vm as any;

    vm.showError(
      {
        response: {
          status: 429,
          data: {
            error: {
              code: "DAILY_LIMIT_REACHED",
              message: "群级日额度已用尽",
              details: { sent_today: 5, daily_limit: 5 },
              retryable: false,
            },
            correlation_id: "msg-limit",
          },
        },
      },
      "创建失败",
    );

    expect(mocks.message.warning).toHaveBeenCalledWith(
      "群级日额度已用尽，已发送 5/5",
    );
    wrapper.unmount();
  });

  it("rejects unknown and community promotion template variables before write", async () => {
    const wrapper = mountView();
    await flushPromises();
    const vm = wrapper.vm as any;
    vm.templateForm.name = "普通模板";
    vm.templateForm.content_category = "community";
    vm.templateForm.message_type = "interaction";
    vm.templateForm.content = "你好 {{promotion_url}} {{unknown_value}}";

    expect(vm.templateValidationError()).toContain("不支持的模板变量");
    vm.templateForm.content = "你好 {{promotion_url}}";
    expect(vm.templateValidationError()).toContain("普通消息模板不能使用");
    vm.templateForm.content = "你好 {{USER_NAME}}";
    expect(vm.templateValidationError()).toContain("非法占位符");
    wrapper.unmount();
  });

  it("blocks manually supplied promotion variables and scheduled user_name templates", async () => {
    (mocks.store.templates as any[]) = [
      {
        id: 44,
        name: "用户问候",
        content: "你好 {{user_name}}",
        content_category: "community",
        message_type: "interaction",
        template_variables: ["user_name"],
        enabled: true,
      },
    ];
    const wrapper = mountView();
    await flushPromises();
    const vm = wrapper.vm as any;
    vm.actionForm.variables_json = JSON.stringify({
      promotion_url: "https://attacker.example/path",
    });
    expect(vm.parseVariables()).toBeNull();
    expect(mocks.message.warning).toHaveBeenCalledWith(
      "promotion_url 只能由当前策略的群内广告配置注入",
    );

    vm.openCreatePolicy();
    vm.policyForm.account_id = 21;
    vm.policyForm.mode = "template";
    vm.policyForm.default_template_id = 44;
    vm.policyForm.trigger_config.scheduled.enabled = true;
    vm.policyForm.trigger_config.scheduled.times = ["10:30"];
    expect(vm.policyValidationError()).toContain("定时触发没有 user_name");
    wrapper.unmount();
  });

  it("keeps a referenced disabled template visible but invalid for saving", async () => {
    (mocks.store.templates as any[]) = [
      {
        id: 44,
        name: "已停用普通模板",
        content: "你好",
        content_category: "community",
        message_type: "interaction",
        template_variables: [],
        enabled: false,
      },
    ];
    const wrapper = mountView();
    await flushPromises();
    const vm = wrapper.vm as any;
    vm.openEditPolicy({
      ...mocks.policy,
      mode: "template",
      default_template_id: 44,
    });

    expect(vm.communityTemplateOptions.map((item: any) => item.id)).toEqual([
      44,
    ]);
    expect(vm.policyValidationError()).toContain("已启用");
    wrapper.unmount();
  });
});
