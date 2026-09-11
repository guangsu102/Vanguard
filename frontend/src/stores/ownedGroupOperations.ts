import { defineStore } from "pinia";
import { reactive, ref } from "vue";
import {
  ownedGroupOperationsApi,
  type AuditEventItem,
  type AuditQuery,
  type Coverage,
  type MemberPage,
  type MemberQuery,
  type OperationListItem,
  type OperationQuery,
  type OperationsCenterSummary,
  type OwnedGroupMember,
} from "@/api/ownedGroupOperations";

export interface GroupOpsChannelError {
  status: number | null;
  code: string | null;
  message: string;
}

type Channel = "summary" | "members" | "operations" | "audit";

const defaultMemberQuery = (): MemberQuery => ({
  sort: "last_observed_desc",
  offset: 0,
  limit: 50,
});
const defaultOperationQuery = (): OperationQuery => ({ offset: 0, limit: 20 });
const defaultAuditQuery = (): AuditQuery => ({ offset: 0, limit: 20 });

const normalizeError = (error: unknown): GroupOpsChannelError => {
  const value = error as {
    message?: unknown;
    response?: { status?: unknown; data?: { error?: { code?: unknown; message?: unknown }; message?: unknown; detail?: unknown } };
  };
  const payload = value?.response?.data;
  const nested = payload?.error;
  const message =
    (typeof nested?.message === "string" && nested.message) ||
    (typeof payload?.message === "string" && payload.message) ||
    (typeof payload?.detail === "string" && payload.detail) ||
    (typeof value?.message === "string" && value.message) ||
    "读取失败";
  const status = Number(value?.response?.status);
  return {
    status: Number.isInteger(status) ? status : null,
    code: typeof nested?.code === "string" ? nested.code : null,
    message,
  };
};

const isAbortError = (error: unknown): boolean => {
  const value = error as { name?: unknown; code?: unknown };
  return value?.name === "AbortError" || value?.name === "CanceledError" || value?.code === "ERR_CANCELED";
};

export const useOwnedGroupOperationsStore = defineStore("ownedGroupOperations", () => {
  const currentAssetId = ref<number | null>(null);
  const summary = ref<OperationsCenterSummary | null>(null);
  const members = ref<OwnedGroupMember[]>([]);
  const memberTotal = ref(0);
  const coverage = ref<Coverage | null>(null);
  const memberQuery = ref<MemberQuery>(defaultMemberQuery());
  const operations = ref<OperationListItem[]>([]);
  const operationTotal = ref(0);
  const operationQuery = ref<OperationQuery>(defaultOperationQuery());
  const auditEvents = ref<AuditEventItem[]>([]);
  const auditTotal = ref(0);
  const auditQuery = ref<AuditQuery>(defaultAuditQuery());
  const loading = reactive<Record<Channel, boolean>>({ summary: false, members: false, operations: false, audit: false });
  const error = reactive<Record<Channel, GroupOpsChannelError | null>>({ summary: null, members: null, operations: null, audit: null });
  const assetGeneration = ref(0);
  const requestSequence = reactive<Record<Channel, number>>({ summary: 0, members: 0, operations: 0, audit: 0 });
  const controllers: Partial<Record<Channel, AbortController>> = {};

  const abortChannel = (channel: Channel) => {
    controllers[channel]?.abort();
    delete controllers[channel];
  };

  const selectAsset = (assetId: number) => {
    if (currentAssetId.value === assetId) return;
    (Object.keys(loading) as Channel[]).forEach((channel) => {
      abortChannel(channel);
      requestSequence[channel] += 1;
      loading[channel] = false;
      error[channel] = null;
    });
    assetGeneration.value += 1;
    currentAssetId.value = assetId;
    summary.value = null;
    members.value = [];
    memberTotal.value = 0;
    coverage.value = null;
    operations.value = [];
    operationTotal.value = 0;
    auditEvents.value = [];
    auditTotal.value = 0;
    memberQuery.value = defaultMemberQuery();
    operationQuery.value = defaultOperationQuery();
    auditQuery.value = defaultAuditQuery();
  };

  const requestContext = (channel: Channel, assetId: number) => {
    if (currentAssetId.value !== assetId) selectAsset(assetId);
    abortChannel(channel);
    const controller = new AbortController();
    controllers[channel] = controller;
    requestSequence[channel] += 1;
    const sequence = requestSequence[channel];
    const generation = assetGeneration.value;
    loading[channel] = true;
    error[channel] = null;
    return { controller, sequence, generation };
  };

  const isCurrent = (channel: Channel, assetId: number, sequence: number, generation: number) =>
    currentAssetId.value === assetId && assetGeneration.value === generation && requestSequence[channel] === sequence;

  const finish = (channel: Channel, context: { controller: AbortController; sequence: number; generation: number }, assetId: number) => {
    if (isCurrent(channel, assetId, context.sequence, context.generation)) {
      loading[channel] = false;
      if (controllers[channel] === context.controller) delete controllers[channel];
    }
  };

  const fetchSummary = async (assetId = currentAssetId.value): Promise<OperationsCenterSummary | null> => {
    if (!assetId) return null;
    const context = requestContext("summary", assetId);
    try {
      const result = await ownedGroupOperationsApi.getSummary(assetId, context.controller.signal);
      const current = isCurrent("summary", assetId, context.sequence, context.generation);
      if (current) summary.value = result;
      return current ? result : null;
    } catch (reason) {
      if (!isAbortError(reason) && isCurrent("summary", assetId, context.sequence, context.generation)) error.summary = normalizeError(reason);
      if (!isAbortError(reason)) throw reason;
      return null;
    } finally {
      finish("summary", context, assetId);
    }
  };

  const fetchMembers = async (assetId = currentAssetId.value, query?: Partial<MemberQuery>): Promise<MemberPage | null> => {
    if (!assetId) return null;
    if (currentAssetId.value !== assetId) selectAsset(assetId);
    memberQuery.value = { ...memberQuery.value, ...query };
    const requestQuery = { ...memberQuery.value };
    const context = requestContext("members", assetId);
    try {
      const result = await ownedGroupOperationsApi.listMembers(assetId, requestQuery, context.controller.signal);
      const current = isCurrent("members", assetId, context.sequence, context.generation);
      if (current) {
        members.value = result.items;
        memberTotal.value = result.total;
        coverage.value = result.coverage;
      }
      return current ? result : null;
    } catch (reason) {
      if (!isAbortError(reason) && isCurrent("members", assetId, context.sequence, context.generation)) error.members = normalizeError(reason);
      if (!isAbortError(reason)) throw reason;
      return null;
    } finally {
      finish("members", context, assetId);
    }
  };

  const fetchOperations = async (assetId = currentAssetId.value, query?: Partial<OperationQuery>) => {
    if (!assetId) return null;
    if (currentAssetId.value !== assetId) selectAsset(assetId);
    operationQuery.value = { ...operationQuery.value, ...query };
    const requestQuery = { ...operationQuery.value };
    const context = requestContext("operations", assetId);
    try {
      const result = await ownedGroupOperationsApi.listOperations(assetId, requestQuery, context.controller.signal);
      const current = isCurrent("operations", assetId, context.sequence, context.generation);
      if (current) {
        operations.value = result.items;
        operationTotal.value = result.total;
      }
      return current ? result : null;
    } catch (reason) {
      if (!isAbortError(reason) && isCurrent("operations", assetId, context.sequence, context.generation)) error.operations = normalizeError(reason);
      if (!isAbortError(reason)) throw reason;
      return null;
    } finally {
      finish("operations", context, assetId);
    }
  };

  const fetchAudit = async (assetId = currentAssetId.value, query?: Partial<AuditQuery>) => {
    if (!assetId) return null;
    if (currentAssetId.value !== assetId) selectAsset(assetId);
    auditQuery.value = { ...auditQuery.value, ...query };
    const requestQuery = { ...auditQuery.value };
    const context = requestContext("audit", assetId);
    try {
      const result = await ownedGroupOperationsApi.listAuditEvents(assetId, requestQuery, context.controller.signal);
      const current = isCurrent("audit", assetId, context.sequence, context.generation);
      if (current) {
        auditEvents.value = result.items;
        auditTotal.value = result.total;
      }
      return current ? result : null;
    } catch (reason) {
      if (!isAbortError(reason) && isCurrent("audit", assetId, context.sequence, context.generation)) error.audit = normalizeError(reason);
      if (!isAbortError(reason)) throw reason;
      return null;
    } finally {
      finish("audit", context, assetId);
    }
  };

  const resetMemberFilters = () => { memberQuery.value = defaultMemberQuery(); };
  const resetAuditFilters = () => { auditQuery.value = defaultAuditQuery(); };
  const dispose = () => (Object.keys(loading) as Channel[]).forEach(abortChannel);

  return {
    currentAssetId, summary, members, memberTotal, coverage, memberQuery,
    operations, operationTotal, operationQuery, auditEvents, auditTotal, auditQuery,
    loading, error, assetGeneration, requestSequence,
    selectAsset, fetchSummary, fetchMembers, fetchOperations, fetchAudit,
    resetMemberFilters, resetAuditFilters, dispose,
  };
});
