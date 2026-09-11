import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";

const api = vi.hoisted(() => ({ getSummary: vi.fn(), listMembers: vi.fn(), listOperations: vi.fn(), listAuditEvents: vi.fn() }));
vi.mock("@/api/ownedGroupOperations", () => ({ ownedGroupOperationsApi: api }));
import { useOwnedGroupOperationsStore } from "./ownedGroupOperations";

const page = (id: number) => ({ items: [{ member_key: `real_user:telegram:${id}`, telegram_user_id: id }], total: 1, offset: 0, limit: 50, coverage: { coverage_status: "collecting" }, summary: {}, correlation_id: null });
const deferred = <T>() => { let resolve!: (value: T) => void; const promise = new Promise<T>((done) => { resolve = done }); return { promise, resolve }; };

describe("ownedGroupOperations store", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.clearAllMocks();
    api.getSummary.mockResolvedValue({ asset: { asset_id: 1 } });
    api.listMembers.mockResolvedValue(page(1));
    api.listOperations.mockResolvedValue({ items: [], total: 0 });
    api.listAuditEvents.mockResolvedValue({ items: [], total: 0 });
  });

  it("keeps all four request channels independent", async () => {
    const pending = deferred<any>();
    api.getSummary.mockReturnValueOnce(pending.promise);
    const store = useOwnedGroupOperationsStore();
    store.selectAsset(18);
    const summaryRequest = store.fetchSummary(18);
    await store.fetchMembers(18);
    expect(store.loading.summary).toBe(true);
    expect(store.loading.members).toBe(false);
    pending.resolve({ asset: { asset_id: 18 } });
    await summaryRequest;
    expect(store.summary?.asset.asset_id).toBe(18);
  });

  it("clears old asset state and ignores a late response even when abort is ignored", async () => {
    const old = deferred<any>();
    api.listMembers.mockReturnValueOnce(old.promise).mockResolvedValueOnce(page(22));
    const store = useOwnedGroupOperationsStore();
    store.selectAsset(11);
    const staleRequest = store.fetchMembers(11);
    store.selectAsset(22);
    const latest = await store.fetchMembers(22);
    old.resolve(page(11));
    const stale = await staleRequest;
    expect(latest?.items[0].telegram_user_id).toBe(22);
    expect(stale).toBeNull();
    expect(store.currentAssetId).toBe(22);
    expect(store.members[0].telegram_user_id).toBe(22);
  });

  it("preserves the first explicit query when fetch switches assets", async () => {
    const store = useOwnedGroupOperationsStore();
    store.selectAsset(1);
    await store.fetchMembers(9, { q: "target", offset: 20, limit: 20 });
    expect(store.currentAssetId).toBe(9);
    expect(api.listMembers).toHaveBeenCalledWith(9, expect.objectContaining({ q: "target", offset: 20, limit: 20 }), expect.any(AbortSignal));
  });

  it("advances only the requested filter channel", async () => {
    const store = useOwnedGroupOperationsStore();
    store.selectAsset(18);
    const summarySequence = store.requestSequence.summary;
    const auditSequence = store.requestSequence.audit;
    await store.fetchMembers(18, { member_kind: "real_user" });
    expect(store.requestSequence.members).toBeGreaterThan(0);
    expect(store.requestSequence.summary).toBe(summarySequence);
    expect(store.requestSequence.audit).toBe(auditSequence);
  });
});
