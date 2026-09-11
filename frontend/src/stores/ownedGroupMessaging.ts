import { defineStore } from "pinia";
import { reactive, ref } from "vue";
import {
  ownedGroupMessagingApi,
  type OwnedGroupEligibleAccountsResponse,
  type OwnedGroupMessageEligibleAccount,
  type OwnedGroupMessageExecutionAccepted,
  type OwnedGroupMessageExecutionDetail,
  type OwnedGroupMessageExecutionFilters,
  type OwnedGroupMessageExecutionPage,
  type OwnedGroupMessageExecutionSummary,
  type OwnedGroupMessageManualExecutionInput,
  type OwnedGroupMessagePolicy,
  type OwnedGroupMessagePolicyCreateInput,
  type OwnedGroupMessagePolicyReplaceInput,
  type OwnedGroupMessagePreviewInput,
  type OwnedGroupMessagePreviewResult,
  type OwnedGroupMessagingAssetSummary,
  type OwnedGroupMessageTemplate,
  type OwnedGroupMessageTemplateInput,
} from "@/api/ownedGroupMessaging";
import { ownedGroupsApi, type OwnedGroupAsset } from "@/api/ownedGroups";

type LoadingKey =
  | "workspace"
  | "asset"
  | "eligible"
  | "policies"
  | "reviews"
  | "history"
  | "templates"
  | "detail"
  | "mutation"
  | "preview";

const createLoadingState = (): Record<LoadingKey, boolean> => ({
  workspace: false,
  asset: false,
  eligible: false,
  policies: false,
  reviews: false,
  history: false,
  templates: false,
  detail: false,
  mutation: false,
  preview: false,
});

export const useOwnedGroupMessagingStore = defineStore(
  "ownedGroupMessaging",
  () => {
    const activeAssetId = ref<number | null>(null);
    const asset = ref<OwnedGroupAsset | null>(null);
    const summary = ref<OwnedGroupMessagingAssetSummary | null>(null);
    const eligibleAccounts = ref<OwnedGroupMessageEligibleAccount[]>([]);
    const policies = ref<OwnedGroupMessagePolicy[]>([]);
    const policyTotal = ref(0);
    const reviewItems = ref<OwnedGroupMessageExecutionSummary[]>([]);
    const reviewTotal = ref(0);
    const history = ref<OwnedGroupMessageExecutionSummary[]>([]);
    const historyTotal = ref(0);
    const historyPage = ref(1);
    const historyPageSize = ref(20);
    const executionDetail = ref<OwnedGroupMessageExecutionDetail | null>(null);
    const templates = ref<OwnedGroupMessageTemplate[]>([]);
    const templateTotal = ref(0);
    const previewResult = ref<OwnedGroupMessagePreviewResult | null>(null);
    const loading = reactive(createLoadingState());
    const errors = reactive<Partial<Record<LoadingKey, unknown>>>({});
    const requestSerial = Object.fromEntries(
      Object.keys(createLoadingState()).map((key) => [key, 0]),
    ) as Record<LoadingKey, number>;
    let requestGeneration = 0;

    const ownsRequest = (assetId: number, generation: number) =>
      activeAssetId.value === assetId && requestGeneration === generation;

    const clearAssetScopedState = () => {
      asset.value = null;
      summary.value = null;
      eligibleAccounts.value = [];
      policies.value = [];
      policyTotal.value = 0;
      reviewItems.value = [];
      reviewTotal.value = 0;
      history.value = [];
      historyTotal.value = 0;
      historyPage.value = 1;
      historyPageSize.value = 20;
      executionDetail.value = null;
      templates.value = [];
      templateTotal.value = 0;
      previewResult.value = null;
      Object.assign(loading, createLoadingState());
      for (const key of Object.keys(errors) as LoadingKey[]) delete errors[key];
    };

    const selectAsset = (assetId: number) => {
      requestGeneration += 1;
      activeAssetId.value = assetId;
      clearAssetScopedState();
      return requestGeneration;
    };

    const reset = () => {
      requestGeneration += 1;
      activeAssetId.value = null;
      clearAssetScopedState();
    };

    const runRequest = async <T>(
      key: LoadingKey,
      assetId: number,
      loader: () => Promise<T>,
      apply: (value: T) => void,
    ): Promise<T> => {
      const generation = requestGeneration;
      const serial = ++requestSerial[key];
      const ownsLatestRequest = () =>
        ownsRequest(assetId, generation) && requestSerial[key] === serial;
      if (activeAssetId.value !== assetId) {
        throw new Error("Owned group messaging asset is no longer active");
      }
      loading[key] = true;
      delete errors[key];
      try {
        const value = await loader();
        if (ownsLatestRequest()) apply(value);
        return value;
      } catch (error) {
        if (ownsLatestRequest()) errors[key] = error;
        throw error;
      } finally {
        if (ownsLatestRequest()) loading[key] = false;
      }
    };

    const fetchAsset = (assetId: number) =>
      runRequest(
        "asset",
        assetId,
        () => ownedGroupsApi.getById(assetId),
        (value) => {
          asset.value = value;
        },
      );

    const fetchEligibleAccounts = (assetId: number) =>
      runRequest<OwnedGroupEligibleAccountsResponse>(
        "eligible",
        assetId,
        () => ownedGroupMessagingApi.getEligibleAccounts(assetId),
        (value) => {
          eligibleAccounts.value = value.data;
          summary.value = value.asset;
        },
      );

    const fetchPolicies = (assetId: number) =>
      runRequest(
        "policies",
        assetId,
        () => ownedGroupMessagingApi.listPolicies(assetId),
        (value) => {
          policies.value = value.data;
          policyTotal.value = value.total;
        },
      );

    const fetchReviews = (assetId: number) =>
      runRequest<OwnedGroupMessageExecutionPage>(
        "reviews",
        assetId,
        async () => {
          const first = await ownedGroupMessagingApi.listExecutions(assetId, {
            status: "pending_review",
            page: 1,
            page_size: 100,
          });
          const pageCount = Math.ceil(first.total / 100);
          if (pageCount <= 1) return first;
          const remaining = await Promise.all(
            Array.from({ length: pageCount - 1 }, (_, index) =>
              ownedGroupMessagingApi.listExecutions(assetId, {
                status: "pending_review",
                page: index + 2,
                page_size: 100,
              }),
            ),
          );
          return {
            ...first,
            items: [first, ...remaining].flatMap((page) => page.items),
          };
        },
        (value) => {
          reviewItems.value = [...value.items].sort((left, right) =>
            left.created_at.localeCompare(right.created_at),
          );
          reviewTotal.value = value.total;
        },
      );

    const fetchHistory = (
      assetId: number,
      filters: OwnedGroupMessageExecutionFilters = {},
    ) =>
      runRequest<OwnedGroupMessageExecutionPage>(
        "history",
        assetId,
        () => ownedGroupMessagingApi.listExecutions(assetId, filters),
        (value) => {
          history.value = value.items;
          historyTotal.value = value.total;
          historyPage.value = value.page;
          historyPageSize.value = value.page_size;
        },
      );

    const fetchTemplates = (assetId: number) =>
      runRequest(
        "templates",
        assetId,
        () => ownedGroupMessagingApi.listTemplates(assetId),
        (value) => {
          templates.value = value.data;
          templateTotal.value = value.total;
        },
      );

    const loadWorkspace = async (assetId: number) => {
      selectAsset(assetId);
      const generation = requestGeneration;
      loading.workspace = true;
      const results = await Promise.allSettled([
        fetchAsset(assetId),
        fetchEligibleAccounts(assetId),
        fetchPolicies(assetId),
        fetchReviews(assetId),
        fetchHistory(assetId, { page: 1, page_size: 20 }),
        fetchTemplates(assetId),
      ]);
      if (ownsRequest(assetId, generation)) loading.workspace = false;
      const successful = results.filter((item) => item.status === "fulfilled");
      if (successful.length === 0) {
        const rejected = results.find(
          (item): item is PromiseRejectedResult => item.status === "rejected",
        );
        throw rejected?.reason ?? new Error("群内消息工作区加载失败");
      }
      return results;
    };

    const createPolicy = async (
      assetId: number,
      input: OwnedGroupMessagePolicyCreateInput,
    ) =>
      runRequest(
        "mutation",
        assetId,
        () => ownedGroupMessagingApi.createPolicy(assetId, input),
        (created) => {
          policies.value = [
            created,
            ...policies.value.filter((item) => item.id !== created.id),
          ];
          policyTotal.value = policies.value.length;
        },
      );

    const replacePolicy = async (
      assetId: number,
      policyId: number,
      input: OwnedGroupMessagePolicyReplaceInput,
    ) =>
      runRequest(
        "mutation",
        assetId,
        () => ownedGroupMessagingApi.replacePolicy(assetId, policyId, input),
        (updated) => {
          policies.value = policies.value.map((item) =>
            item.id === updated.id ? updated : item,
          );
        },
      );

    const previewPolicy = async (
      assetId: number,
      policyId: number,
      input: OwnedGroupMessagePreviewInput,
    ) =>
      runRequest(
        "preview",
        assetId,
        () => ownedGroupMessagingApi.preview(assetId, policyId, input),
        (result) => {
          previewResult.value = result;
        },
      );

    const createManualExecution = async (
      assetId: number,
      policyId: number,
      input: OwnedGroupMessageManualExecutionInput,
      idempotencyKey: string,
    ): Promise<OwnedGroupMessageExecutionAccepted> => {
      const result = await runRequest(
        "mutation",
        assetId,
        () =>
          ownedGroupMessagingApi.createExecution(
            assetId,
            policyId,
            input,
            idempotencyKey,
          ),
        () => undefined,
      );
      await Promise.allSettled([
        fetchReviews(assetId),
        fetchHistory(assetId, { page: 1, page_size: historyPageSize.value }),
        fetchPolicies(assetId),
      ]);
      return result;
    };

    const fetchExecutionDetail = (assetId: number, executionId: number) => {
      if (activeAssetId.value === assetId) executionDetail.value = null;
      return runRequest(
        "detail",
        assetId,
        () => ownedGroupMessagingApi.getExecution(assetId, executionId),
        (detail) => {
          executionDetail.value = detail;
        },
      );
    };

    const refreshAfterReview = async (assetId: number) => {
      await Promise.allSettled([
        fetchReviews(assetId),
        fetchHistory(assetId, {
          page: historyPage.value,
          page_size: historyPageSize.value,
        }),
        fetchPolicies(assetId),
      ]);
    };

    const approveExecution = async (
      assetId: number,
      executionId: number,
      revision: number,
      contentOverride?: string | null,
    ) => {
      const result = await runRequest(
        "mutation",
        assetId,
        () =>
          ownedGroupMessagingApi.approveExecution(assetId, executionId, {
            revision,
            content_override: contentOverride ?? null,
          }),
        (detail) => {
          executionDetail.value = detail;
        },
      );
      await refreshAfterReview(assetId);
      return result;
    };

    const rejectExecution = async (
      assetId: number,
      executionId: number,
      revision: number,
      reason: string,
    ) => {
      const result = await runRequest(
        "mutation",
        assetId,
        () =>
          ownedGroupMessagingApi.rejectExecution(assetId, executionId, {
            revision,
            reason,
          }),
        (detail) => {
          executionDetail.value = detail;
        },
      );
      await refreshAfterReview(assetId);
      return result;
    };

    const createTemplate = async (
      assetId: number,
      input: OwnedGroupMessageTemplateInput,
    ) => {
      const result = await runRequest(
        "mutation",
        assetId,
        () => ownedGroupMessagingApi.createTemplate(assetId, input),
        () => undefined,
      );
      await Promise.allSettled([
        fetchTemplates(assetId),
        fetchPolicies(assetId),
      ]);
      return result;
    };

    const updateTemplate = async (
      assetId: number,
      templateId: number,
      input: OwnedGroupMessageTemplateInput,
    ) => {
      const result = await runRequest(
        "mutation",
        assetId,
        () => ownedGroupMessagingApi.updateTemplate(assetId, templateId, input),
        () => undefined,
      );
      await Promise.allSettled([
        fetchTemplates(assetId),
        fetchPolicies(assetId),
      ]);
      return result;
    };

    return {
      activeAssetId,
      asset,
      summary,
      eligibleAccounts,
      policies,
      policyTotal,
      reviewItems,
      reviewTotal,
      history,
      historyTotal,
      historyPage,
      historyPageSize,
      executionDetail,
      templates,
      templateTotal,
      previewResult,
      loading,
      errors,
      selectAsset,
      reset,
      fetchAsset,
      fetchEligibleAccounts,
      fetchPolicies,
      fetchReviews,
      fetchHistory,
      fetchTemplates,
      loadWorkspace,
      createPolicy,
      replacePolicy,
      previewPolicy,
      createManualExecution,
      fetchExecutionDetail,
      approveExecution,
      rejectExecution,
      createTemplate,
      updateTemplate,
    };
  },
);
