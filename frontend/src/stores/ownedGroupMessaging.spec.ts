import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";

const api = vi.hoisted(() => ({
  getEligibleAccounts: vi.fn(),
  listPolicies: vi.fn(),
  getPolicy: vi.fn(),
  createPolicy: vi.fn(),
  replacePolicy: vi.fn(),
  preview: vi.fn(),
  createExecution: vi.fn(),
  listExecutions: vi.fn(),
  getExecution: vi.fn(),
  approveExecution: vi.fn(),
  rejectExecution: vi.fn(),
  listTemplates: vi.fn(),
  createTemplate: vi.fn(),
  updateTemplate: vi.fn(),
}));

const ownedGroupsApi = vi.hoisted(() => ({
  getById: vi.fn(),
}));

vi.mock("@/api/ownedGroupMessaging", () => ({
  ownedGroupMessagingApi: api,
}));

vi.mock("@/api/ownedGroups", () => ({
  ownedGroupsApi,
}));

import { useOwnedGroupMessagingStore } from "./ownedGroupMessaging";

const executionPage = (items: unknown[] = []) => ({
  items,
  total: items.length,
  page: 1,
  page_size: 20,
});

describe("ownedGroupMessaging store", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.clearAllMocks();
    ownedGroupsApi.getById.mockResolvedValue({ id: 18, title: "Owned 18" });
    api.getEligibleAccounts.mockResolvedValue({
      data: [],
      asset: {
        asset_id: 18,
        core_group_id: 311,
        telegram_chat_id: -1001234567890,
        governance_status: "managed",
      },
    });
    api.listPolicies.mockResolvedValue({ data: [], total: 0 });
    api.listExecutions.mockResolvedValue(executionPage());
    api.listTemplates.mockResolvedValue({ data: [], total: 0 });
  });

  it("clears asset-scoped state immediately and ignores a stale response", async () => {
    let resolveAsset18!: (value: { data: unknown[]; total: number }) => void;
    const stale = new Promise<{ data: unknown[]; total: number }>((resolve) => {
      resolveAsset18 = resolve;
    });
    api.listTemplates.mockReturnValueOnce(stale);
    const store = useOwnedGroupMessagingStore();
    store.selectAsset(18);
    const pending = store.fetchTemplates(18);

    store.templates = [
      {
        id: 99,
        name: "temporary",
        content: "temporary",
        content_category: "community",
        message_type: "interaction",
        template_variables: [],
        enabled: true,
        created_at: "",
        updated_at: "",
      },
    ];
    store.selectAsset(19);

    expect(store.templates).toEqual([]);
    resolveAsset18({
      data: [
        {
          id: 18,
          name: "asset 18 only",
          content: "old",
          content_category: "community",
          message_type: "interaction",
          template_variables: [],
          enabled: true,
          created_at: "",
          updated_at: "",
        },
      ],
      total: 1,
    });
    await pending;

    expect(store.activeAssetId).toBe(19);
    expect(store.templates).toEqual([]);
  });

  it("keeps only the latest execution detail for repeated requests in one asset", async () => {
    let resolveFirst!: (value: any) => void;
    const first = new Promise<any>((resolve) => {
      resolveFirst = resolve;
    });
    api.getExecution
      .mockReturnValueOnce(first)
      .mockResolvedValueOnce({ id: 202, content: "new detail" });
    const store = useOwnedGroupMessagingStore();
    store.selectAsset(18);

    const staleRequest = store.fetchExecutionDetail(18, 101);
    const latestRequest = store.fetchExecutionDetail(18, 202);
    await latestRequest;
    expect(store.executionDetail).toMatchObject({
      id: 202,
      content: "new detail",
    });

    resolveFirst({ id: 101, content: "stale sensitive detail" });
    await staleRequest;

    expect(store.executionDetail).toMatchObject({
      id: 202,
      content: "new detail",
    });
  });

  it("loads the six independent workspace sections for one asset", async () => {
    const store = useOwnedGroupMessagingStore();

    const outcomes = await store.loadWorkspace(18);

    expect(outcomes).toHaveLength(6);
    expect(ownedGroupsApi.getById).toHaveBeenCalledWith(18);
    expect(api.getEligibleAccounts).toHaveBeenCalledWith(18);
    expect(api.listPolicies).toHaveBeenCalledWith(18);
    expect(api.listTemplates).toHaveBeenCalledWith(18);
    expect(api.listExecutions).toHaveBeenCalledWith(
      18,
      expect.objectContaining({ status: "pending_review", page_size: 100 }),
    );
    expect(store.summary?.telegram_chat_id).toBe(-1001234567890);
  });

  it("loads every pending-review page and exposes the queue oldest first", async () => {
    api.listExecutions.mockImplementation(
      (_assetId: number, filters: { status?: string; page?: number }) => {
        if (filters.status !== "pending_review") return executionPage();
        if (filters.page === 2) {
          return Promise.resolve({
            items: [{ id: 1, created_at: "2026-09-10T00:00:00Z" }],
            total: 101,
            page: 2,
            page_size: 100,
          });
        }
        return Promise.resolve({
          items: [
            { id: 101, created_at: "2026-09-10T02:00:00Z" },
            { id: 100, created_at: "2026-09-10T01:00:00Z" },
          ],
          total: 101,
          page: 1,
          page_size: 100,
        });
      },
    );
    const store = useOwnedGroupMessagingStore();
    store.selectAsset(18);

    await store.fetchReviews(18);

    expect(api.listExecutions).toHaveBeenCalledWith(18, {
      status: "pending_review",
      page: 2,
      page_size: 100,
    });
    expect(store.reviewItems.map((item) => item.id)).toEqual([1, 100, 101]);
    expect(store.reviewTotal).toBe(101);
  });

  it("refreshes the asset-only template list and policy dropdown after a template write", async () => {
    api.createTemplate.mockResolvedValue({ id: 31, name: "Owned template" });
    api.listTemplates.mockResolvedValue({
      data: [
        {
          id: 31,
          name: "Owned template",
          content: "Hello",
          content_category: "community",
          message_type: "interaction",
          template_variables: [],
          enabled: true,
          created_at: "",
          updated_at: "",
        },
      ],
      total: 1,
    });
    const store = useOwnedGroupMessagingStore();
    store.selectAsset(18);

    await store.createTemplate(18, {
      name: "Owned template",
      content: "Hello",
      message_type: "interaction",
      template_variables: [],
      enabled: true,
    });

    expect(api.createTemplate).toHaveBeenCalledWith(
      18,
      expect.not.objectContaining({ scope: expect.anything() }),
    );
    expect(api.listTemplates).toHaveBeenCalledWith(18);
    expect(api.listPolicies).toHaveBeenCalledWith(18);
    expect(store.templates[0].name).toBe("Owned template");
  });

  it("keeps a 202 manual result queued and refreshes review/history without claiming sent", async () => {
    api.createExecution.mockResolvedValue({
      execution_id: 101,
      status: "pending_review",
      correlation_id: "msg-101",
    });
    const store = useOwnedGroupMessagingStore();
    store.selectAsset(18);

    const result = await store.createManualExecution(
      18,
      8,
      { trigger_type: "manual", content_category: "community" },
      "manual-key-101",
    );

    expect(result.status).toBe("pending_review");
    expect(result).not.toHaveProperty("telegram_message_id");
    expect(api.listExecutions).toHaveBeenCalledWith(
      18,
      expect.objectContaining({ status: "pending_review" }),
    );
    expect(api.listPolicies).toHaveBeenCalledWith(18);
  });

  it("does not optimistically overwrite review state when revision conflicts", async () => {
    const conflict = new Error("revision conflict");
    api.approveExecution.mockRejectedValue(conflict);
    const store = useOwnedGroupMessagingStore();
    store.selectAsset(18);
    store.reviewItems = [
      {
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
        status: "pending_review",
        correlation_id: "msg-101",
        attempt_count: 0,
        revision: 2,
        created_at: "2026-09-10T00:00:00Z",
        updated_at: "2026-09-10T00:00:00Z",
      },
    ];

    await expect(store.approveExecution(18, 101, 1)).rejects.toBe(conflict);

    expect(store.reviewItems[0]).toMatchObject({
      status: "pending_review",
      revision: 2,
    });
  });
});
