import { beforeEach, describe, expect, it, vi } from "vitest";

const client = vi.hoisted(() => ({
  get: vi.fn(),
}));

vi.mock("./client", () => ({ default: client }));

import { accountsApi } from "./accounts";

describe("accounts API SpamBot and restriction fields", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("normalizes SpamBot results and the independent Telegram restriction source", async () => {
    client.get.mockResolvedValue({
      data: {
        data: [
          {
            id: 17,
            identifier: "promoter-17",
            account_type: "promoter",
            status: "restricted",
            spam_check_status: "clear",
            spam_checked_at: "2026-09-13T01:00:00Z",
            spam_check_summary: "SpamBot reports no limits",
            restriction_source: "telegram_rpc",
            restriction_reason: "UserRestrictedError",
            restriction_detected_at: "2026-09-13T00:59:00Z",
            risk_score: 62,
            risk_level: "limited",
            risk_pause_until: "2026-09-13T02:00:00Z",
            risk_recovery_until: "2026-09-13T03:00:00Z",
            risk_reason: "telegram_flood_wait",
          },
        ],
        total: 1,
        next_cursor: null,
        has_more: false,
      },
    });

    const result = await accountsApi.list({ account_type: "promoter" });

    expect(result.list[0]).toMatchObject({
      status: "restricted",
      spam_check_status: "clear",
      spam_checked_at: "2026-09-13T01:00:00Z",
      spam_check_summary: "SpamBot reports no limits",
      restriction_source: "telegram_rpc",
      restriction_reason: "UserRestrictedError",
      restriction_detected_at: "2026-09-13T00:59:00Z",
      risk_score: 62,
      risk_level: "limited",
      risk_pause_until: "2026-09-13T02:00:00Z",
      risk_recovery_until: "2026-09-13T03:00:00Z",
      risk_reason: "telegram_flood_wait",
    });
  });

  it("defaults a missing SpamBot result to unknown", async () => {
    client.get.mockResolvedValue({
      data: {
        data: [{ id: 18, identifier: "promoter-18" }],
        total: 1,
      },
    });

    const result = await accountsApi.list();

    expect(result.list[0]?.spam_check_status).toBe("unknown");
    expect(result.list[0]?.risk_level).toBe("normal");
    expect(result.list[0]?.risk_score).toBe(0);
  });
});
