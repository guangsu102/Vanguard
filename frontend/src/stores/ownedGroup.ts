import { defineStore } from "pinia";
import { ref } from "vue";
import {
  ownedGroupsApi,
  type OwnedBotProfile,
  type OwnedBotProfileListResponse,
  type OwnedBotProfileRegisterInput,
  type OwnedGroupAsset,
  type OwnedGroupDraftInput,
  type OwnedGroupGovernanceCandidate,
  type OwnedGroupGovernanceStatus,
  type OwnedGroupInviteLink,
  type OwnedGroupInviteLinkListResponse,
  type OwnedGroupInviteLinkMutationResponse,
  type OwnedGroupOperationDetail,
  type OwnedGroupOperationInput,
  type OwnedGroupPrecheckResult,
} from "@/api/ownedGroups";

export const useOwnedGroupStore = defineStore("ownedGroup", () => {
  const list = ref<OwnedGroupAsset[]>([]);
  const total = ref(0);
  const current = ref<OwnedGroupAsset | null>(null);
  const operation = ref<OwnedGroupOperationDetail | null>(null);
  const loading = ref(false);
  // Bot profile state intentionally contains metadata only.  Tokens are
  // accepted by registerBotProfile and are never assigned to a ref.
  const botProfiles = ref<OwnedBotProfile[]>([]);
  const botProfilesTotal = ref(0);
  const botProfilesLoading = ref(false);
  // Invite URLs are bearer credentials. Keep them only in volatile Pinia
  // memory for the explicit reveal screen; never persist or log this state.
  const inviteLinks = ref<OwnedGroupInviteLink[]>([]);
  const inviteLinksTotal = ref(0);
  const inviteLinksLoading = ref(false);
  const inviteLinksAssetId = ref<number | null>(null);
  let inviteLinksRequestId = 0;
  const governanceByAssetId = ref<
    Record<number, OwnedGroupGovernanceStatus>
  >({});
  const governanceLoadingByAssetId = ref<Record<number, boolean>>({});
  const governanceCandidatesByAssetId = ref<
    Record<number, OwnedGroupGovernanceCandidate[]>
  >({});
  const governanceCandidatesLoadingByAssetId = ref<Record<number, boolean>>(
    {},
  );

  const fetchList = async () => {
    loading.value = true;
    try {
      const response = await ownedGroupsApi.list({ limit: 200 });
      list.value = response.data.data || [];
      total.value = Number(response.data.total || 0);
      return list.value;
    } finally {
      loading.value = false;
    }
  };

  const fetchAsset = async (id: number) => {
    const asset = await ownedGroupsApi.getById(id);
    current.value = asset;
    const index = list.value.findIndex((item) => item.id === id);
    if (index >= 0) list.value[index] = asset;
    return asset;
  };

  const cacheGovernance = (result: OwnedGroupGovernanceStatus) => {
    governanceByAssetId.value = {
      ...governanceByAssetId.value,
      [result.asset_id]: result,
    };
    const patchAsset = (asset: OwnedGroupAsset): OwnedGroupAsset => ({
      ...asset,
      status: result.asset_status,
      telegram_chat_id: result.telegram_chat_id,
      core_group_id: result.core_group_id,
      managed_binding_id: result.managed_binding_id,
      guardian_bot_account_id: result.guardian_bot_account_id,
      governance_status: result.governance_status,
      governance_pending_at: result.governance_pending_at,
      governance_enabled_at: result.governance_enabled_at,
      governance_last_checked_at: result.governance_last_checked_at,
      governance_last_error_code: result.failure?.reason ?? null,
      governance_last_error_message: result.failure?.message ?? null,
    });
    list.value = list.value.map((asset) =>
      asset.id === result.asset_id ? patchAsset(asset) : asset,
    );
    if (current.value?.id === result.asset_id) {
      current.value = patchAsset(current.value);
    }
    return result;
  };

  const setGovernanceLoading = (assetId: number, loadingValue: boolean) => {
    governanceLoadingByAssetId.value = {
      ...governanceLoadingByAssetId.value,
      [assetId]: loadingValue,
    };
  };

  const fetchGovernance = async (assetId: number) => {
    setGovernanceLoading(assetId, true);
    try {
      return cacheGovernance(await ownedGroupsApi.getGovernance(assetId));
    } finally {
      setGovernanceLoading(assetId, false);
    }
  };

  const refreshGovernanceAfterFailure = async (assetId: number) => {
    try {
      cacheGovernance(await ownedGroupsApi.getGovernance(assetId));
    } catch {
      // Preserve the original mutation error. A failed best-effort refresh
      // must not replace the actionable bind/reconcile response.
    }
  };

  const fetchGovernanceCandidates = async (assetId: number) => {
    governanceCandidatesLoadingByAssetId.value = {
      ...governanceCandidatesLoadingByAssetId.value,
      [assetId]: true,
    };
    try {
      const candidates =
        await ownedGroupsApi.getGovernanceCandidates(assetId);
      governanceCandidatesByAssetId.value = {
        ...governanceCandidatesByAssetId.value,
        [assetId]: candidates,
      };
      return candidates;
    } finally {
      governanceCandidatesLoadingByAssetId.value = {
        ...governanceCandidatesLoadingByAssetId.value,
        [assetId]: false,
      };
    }
  };

  const bindGovernance = async (
    assetId: number,
    guardianBotAccountId: number,
  ) => {
    setGovernanceLoading(assetId, true);
    try {
      return cacheGovernance(
        await ownedGroupsApi.bindGovernance(
          assetId,
          guardianBotAccountId,
        ),
      );
    } catch (error) {
      await refreshGovernanceAfterFailure(assetId);
      throw error;
    } finally {
      setGovernanceLoading(assetId, false);
    }
  };

  const reconcileGovernance = async (assetId: number) => {
    setGovernanceLoading(assetId, true);
    try {
      return cacheGovernance(
        await ownedGroupsApi.reconcileGovernance(assetId),
      );
    } catch (error) {
      await refreshGovernanceAfterFailure(assetId);
      throw error;
    } finally {
      setGovernanceLoading(assetId, false);
    }
  };

  const fetchInviteLinks = async (
    id: number,
  ): Promise<OwnedGroupInviteLinkListResponse> => {
    const requestId = ++inviteLinksRequestId;
    inviteLinksLoading.value = true;
    try {
      const response = await ownedGroupsApi.listInviteLinks(id);
      if (requestId === inviteLinksRequestId) {
        inviteLinks.value = response.data;
        inviteLinksTotal.value = response.total;
        inviteLinksAssetId.value = id;
      }
      return response;
    } finally {
      if (requestId === inviteLinksRequestId) inviteLinksLoading.value = false;
    }
  };

  const revokeInviteLink = async (
    assetId: number,
    linkId: number,
  ): Promise<OwnedGroupInviteLinkMutationResponse> => {
    inviteLinksLoading.value = true;
    try {
      const response = await ownedGroupsApi.revokeInviteLink(assetId, linkId);
      // Re-fetch after a mutation so the plaintext value and active state come
      // from the server's authoritative row, not an optimistic local copy.
      await fetchInviteLinks(assetId);
      return response;
    } finally {
      inviteLinksLoading.value = false;
    }
  };

  const regenerateInviteLink = async (
    assetId: number,
    requestNeeded?: boolean,
  ): Promise<OwnedGroupInviteLinkMutationResponse> => {
    inviteLinksLoading.value = true;
    try {
      const response = await ownedGroupsApi.regenerateInviteLink(
        assetId,
        requestNeeded,
      );
      await fetchInviteLinks(assetId);
      return response;
    } finally {
      inviteLinksLoading.value = false;
    }
  };

  const clearInviteLinks = () => {
    // Explicitly overwrite the reactive array reference when an asset is
    // deselected so a previous private URL cannot remain visible.
    inviteLinksRequestId += 1;
    inviteLinks.value = [];
    inviteLinksTotal.value = 0;
    inviteLinksAssetId.value = null;
    inviteLinksLoading.value = false;
  };

  const createDraft = async (data: OwnedGroupDraftInput) => {
    loading.value = true;
    try {
      const asset = await ownedGroupsApi.createDraft(data);
      current.value = asset;
      await fetchList();
      return asset;
    } finally {
      loading.value = false;
    }
  };

  const precheck = async (id: number) => {
    loading.value = true;
    try {
      const result = await ownedGroupsApi.precheck(id);
      await fetchAsset(id);
      return result;
    } finally {
      loading.value = false;
    }
  };

  const reconcileAsset = async (
    id: number,
    telegramChatId?: number,
    telegramUsername?: string,
  ) => {
    loading.value = true;
    try {
      const result = await ownedGroupsApi.reconcileAsset(
        id,
        telegramChatId,
        telegramUsername,
      );
      await fetchAsset(id);
      return result;
    } finally {
      loading.value = false;
    }
  };

  const precheckOperation = async (
    id: number,
    data: OwnedGroupOperationInput,
  ): Promise<OwnedGroupPrecheckResult> =>
    ownedGroupsApi.precheckOperation(id, data);

  const fetchBotProfiles = async (): Promise<OwnedBotProfileListResponse> => {
    botProfilesLoading.value = true;
    try {
      const response = await ownedGroupsApi.listBotProfiles({ limit: 200 });
      botProfiles.value = response.data;
      botProfilesTotal.value = response.total;
      return response;
    } finally {
      botProfilesLoading.value = false;
    }
  };

  const registerBotProfile = async (data: OwnedBotProfileRegisterInput) => {
    botProfilesLoading.value = true;
    try {
      // Do not copy `data.bot_token` into store state; the API receives it
      // once and returns a token-free profile representation.
      const profile = await ownedGroupsApi.registerBotProfile(data);
      const alreadyListed = botProfiles.value.some(
        (item) => item.id === profile.id,
      );
      botProfiles.value = [
        profile,
        ...botProfiles.value.filter((item) => item.id !== profile.id),
      ];
      if (!alreadyListed) botProfilesTotal.value += 1;
      return profile;
    } finally {
      botProfilesLoading.value = false;
    }
  };

  const verifyBotProfile = async (id: number) => {
    botProfilesLoading.value = true;
    try {
      const profile = await ownedGroupsApi.verifyBotProfile(id);
      const index = botProfiles.value.findIndex((item) => item.id === id);
      if (index >= 0) botProfiles.value[index] = profile;
      return profile;
    } finally {
      botProfilesLoading.value = false;
    }
  };

  const setBotProfileEnabled = async (id: number, enabled: boolean) => {
    botProfilesLoading.value = true;
    try {
      const profile = await ownedGroupsApi.setBotProfileEnabled(id, enabled);
      const index = botProfiles.value.findIndex((item) => item.id === id);
      if (index >= 0) botProfiles.value[index] = profile;
      return profile;
    } finally {
      botProfilesLoading.value = false;
    }
  };

  const refreshOperation = async (id = operation.value?.id) => {
    if (!id) return null;
    const result = await ownedGroupsApi.getOperation(id);
    operation.value = result;
    return result;
  };

  const submitOperation = async (
    id: number,
    data: OwnedGroupOperationInput,
  ) => {
    loading.value = true;
    try {
      const created = await ownedGroupsApi.submitOperation(
        id,
        data,
        "owned-group-" + id + "-" + Date.now(),
      );
      return await refreshOperation(created.id);
    } finally {
      loading.value = false;
    }
  };

  const controlOperation = async (
    action: "pause" | "resume" | "stop" | "retry" | "reconcile",
  ) => {
    if (!operation.value) throw new Error("No operation selected");
    const id = operation.value.id;
    const handlers = {
      pause: ownedGroupsApi.pauseOperation,
      resume: ownedGroupsApi.resumeOperation,
      stop: ownedGroupsApi.stopOperation,
      retry: ownedGroupsApi.retryOperation,
      reconcile: ownedGroupsApi.reconcileOperation,
    };
    await handlers[action](id);
    return refreshOperation(id);
  };

  const select = (asset: OwnedGroupAsset) => {
    current.value = asset;
    operation.value = null;
    clearInviteLinks();
  };

  return {
    list,
    total,
    current,
    operation,
    loading,
    botProfiles,
    botProfilesTotal,
    botProfilesLoading,
    inviteLinks,
    inviteLinksTotal,
    inviteLinksLoading,
    inviteLinksAssetId,
    governanceByAssetId,
    governanceLoadingByAssetId,
    governanceCandidatesByAssetId,
    governanceCandidatesLoadingByAssetId,
    fetchList,
    fetchAsset,
    fetchGovernance,
    fetchGovernanceCandidates,
    bindGovernance,
    reconcileGovernance,
    fetchInviteLinks,
    revokeInviteLink,
    regenerateInviteLink,
    clearInviteLinks,
    createDraft,
    precheck,
    reconcileAsset,
    precheckOperation,
    fetchBotProfiles,
    registerBotProfile,
    verifyBotProfile,
    setBotProfileEnabled,
    submitOperation,
    refreshOperation,
    controlOperation,
    select,
  };
});
