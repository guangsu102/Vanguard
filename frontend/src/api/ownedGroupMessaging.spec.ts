import { beforeEach, describe, expect, it, vi } from "vitest";

const { get, post, put } = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
}));

vi.mock("./client", () => ({ default: { get, post, put } }));

import {
  getOwnedGroupMessagingError,
  ownedGroupMessagingApi,
  type OwnedGroupMessagePolicyCreateInput,
  type OwnedGroupMessageTemplateInput,
} from "./ownedGroupMessaging";

const policyInput: OwnedGroupMessagePolicyCreateInput = {
  account_id: 21,
  mode: "ai",
  default_template_id: null,
  trigger_config: {
    version: 1,
    scheduled: {
      enabled: false,
      timezone: "Asia/Shanghai",
      weekdays: [1, 2, 3],
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
};

describe("owned-group messaging API", () => {
  beforeEach(() => {
    get.mockReset();
    post.mockReset();
    put.mockReset();
  });

  it("normalizes eligible accounts with the extended asset summary", async () => {
    get.mockResolvedValue({
      data: {
        data: [
          {
            account_id: 21,
            display_name: "运营号 A",
            eligible: false,
            blocking_reasons: ["membership_stale"],
          },
        ],
        asset: {
          asset_id: 18,
          telegram_chat_id: -1001234567890,
          core_group_id: 311,
          governance_status: "managed",
          messaging_status: { static_enabled: false, dry_run: true },
          runtime_limits: {
            global_group_daily_limit: 20,
            global_account_daily_limit: 30,
            dedupe_window_seconds: 21600,
            max_attempts: 3,
          },
          stats: { sent_today: 2 },
        },
        correlation_id: "msg-list-18",
      },
    });

    const result = await ownedGroupMessagingApi.getEligibleAccounts(18);

    expect(get).toHaveBeenCalledWith(
      "/owned-groups/18/messages/eligible-accounts",
    );
    expect(result.asset.telegram_chat_id).toBe(-1001234567890);
    expect(result.asset.messaging_status?.static_enabled).toBe(false);
    expect(result.asset.runtime_limits?.global_group_daily_limit).toBe(20);
    expect(result.asset.stats?.sent_today).toBe(2);
    expect(result.data[0].blocking_reasons).toEqual(["membership_stale"]);
  });

  it("uses the policy CRUD and preview contracts", async () => {
    post.mockResolvedValue({ data: { data: { id: 8 } } });
    put.mockResolvedValue({ data: { data: { id: 8, revision: 2 } } });
    get.mockResolvedValue({ data: { data: { id: 8 } } });

    await ownedGroupMessagingApi.createPolicy(18, policyInput);
    await ownedGroupMessagingApi.getPolicy(18, 8);
    const { account_id: _accountId, ...replaceInput } = policyInput;
    await ownedGroupMessagingApi.replacePolicy(18, 8, {
      ...replaceInput,
      revision: 1,
    });
    await ownedGroupMessagingApi.preview(18, 8, {
      trigger_type: "manual",
      content_category: "community",
      variables: {},
    });

    expect(post).toHaveBeenNthCalledWith(
      1,
      "/owned-groups/18/messages/policies",
      policyInput,
    );
    expect(get).toHaveBeenCalledWith("/owned-groups/18/messages/policies/8");
    expect(put.mock.calls[0][1]).not.toHaveProperty("account_id");
    expect(post).toHaveBeenNthCalledWith(
      2,
      "/owned-groups/18/messages/policies/8/preview",
      expect.objectContaining({ content_category: "community" }),
    );
  });

  it("creates a manual execution with one stable idempotency header and no bypass fields", async () => {
    post.mockResolvedValue({
      data: {
        data: {
          id: 101,
          execution_id: 101,
          status: "pending_review",
        },
        correlation_id: "msg-101",
      },
    });

    const result = await ownedGroupMessagingApi.createExecution(
      18,
      8,
      {
        trigger_type: "manual",
        content_category: "promotion",
        instruction: "自然介绍",
        variables: {},
      },
      "manual-request-101",
    );

    expect(result.status).toBe("pending_review");
    expect(result.correlation_id).toBe("msg-101");
    expect(post).toHaveBeenCalledWith(
      "/owned-groups/18/messages/policies/8/executions",
      expect.not.objectContaining({
        force: expect.anything(),
        skip_review: expect.anything(),
        destination_url: expect.anything(),
        tracking_link: expect.anything(),
      }),
      { headers: { "Idempotency-Key": "manual-request-101" } },
    );
  });

  it("keeps owned-group template writes scoped by URL only", async () => {
    const template: OwnedGroupMessageTemplateInput = {
      name: "群内活动",
      content: "查看详情 {{promotion_url}}",
      message_type: "guide",
      template_variables: ["promotion_url"],
      enabled: true,
    };
    post.mockResolvedValue({ data: { data: { id: 31 } } });
    put.mockResolvedValue({ data: { data: { id: 31 } } });

    await ownedGroupMessagingApi.createTemplate(18, template);
    await ownedGroupMessagingApi.updateTemplate(18, 31, template);

    for (const payload of [post.mock.calls[0][1], put.mock.calls[0][1]]) {
      expect(payload).toEqual(template);
      expect(payload).not.toHaveProperty("scope");
      expect(payload).not.toHaveProperty("owned_group_asset_id");
      expect(payload).not.toHaveProperty("content_category");
    }
  });

  it("preserves pagination metadata when data is a sibling of total", async () => {
    get.mockResolvedValue({
      data: {
        data: [{ id: 9, status: "sent" }],
        total: 27,
        page: 2,
        page_size: 10,
      },
    });

    const result = await ownedGroupMessagingApi.listExecutions(18, {
      page: 2,
      page_size: 10,
    });

    expect(result).toMatchObject({ total: 27, page: 2, page_size: 10 });
    expect(result.items).toHaveLength(1);
  });

  it("unwraps the final nested execution page shape", async () => {
    get.mockResolvedValue({
      data: {
        data: {
          items: [{ id: 12, execution_id: 12, status: "pending_review" }],
          total: 1,
          page: 1,
          page_size: 20,
        },
        correlation_id: "msg-executions-18",
      },
    });

    const result = await ownedGroupMessagingApi.listExecutions(18);

    expect(result).toMatchObject({ total: 1, page: 1, page_size: 20 });
    expect(result.items[0]).toMatchObject({ id: 12, execution_id: 12 });
  });

  it("omits cleared execution filters instead of sending invalid empty literals", async () => {
    get.mockResolvedValue({
      data: { data: { items: [], total: 0, page: 1, page_size: 20 } },
    });

    await ownedGroupMessagingApi.listExecutions(18, {
      account_id: undefined,
      trigger_type: "",
      content_category: "",
      status: "",
      page: 1,
      page_size: 20,
    });

    expect(get).toHaveBeenCalledWith("/owned-groups/18/messages/executions", {
      params: { page: 1, page_size: 20 },
    });
  });

  it("extracts nested revision and quota errors with correlation details", () => {
    const result = getOwnedGroupMessagingError({
      response: {
        status: 429,
        data: {
          error: {
            code: "GROUP_COOLDOWN_ACTIVE",
            message: "群级冷却中",
            details: { cooldown_until: "2026-09-10T12:00:00Z" },
            retryable: false,
          },
          correlation_id: "msg-cooldown",
        },
      },
    });

    expect(result).toEqual({
      code: "GROUP_COOLDOWN_ACTIVE",
      message: "群级冷却中",
      details: { cooldown_until: "2026-09-10T12:00:00Z" },
      retryable: false,
      correlation_id: "msg-cooldown",
      status: 429,
    });
  });
});
