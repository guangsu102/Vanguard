export const GROUP_OPS_READ_ROLES = ["admin", "operator", "auditor"] as const;

export type GroupOpsReadRole = (typeof GROUP_OPS_READ_ROLES)[number];

export const canReadGroupOps = (role?: string | null): role is GroupOpsReadRole =>
  GROUP_OPS_READ_ROLES.some((item) => item === role);

export const isSafePositiveId = (value: unknown): value is number =>
  typeof value === "number" && Number.isSafeInteger(value) && value > 0;

export const parseSafePositiveId = (value: unknown): number | null => {
  const raw = Array.isArray(value) ? value[0] : value;
  if (typeof raw !== "string" && typeof raw !== "number") return null;
  if (typeof raw === "string" && !/^[1-9]\d*$/.test(raw)) return null;
  const parsed = Number(raw);
  return isSafePositiveId(parsed) ? parsed : null;
};

/** Telegram chat identifiers are signed, so unlike local IDs they may be negative. */
export const parseSafeTelegramId = (value: unknown): number | null => {
  const raw = Array.isArray(value) ? value[0] : value;
  if (typeof raw !== "string" && typeof raw !== "number") return null;
  if (typeof raw === "string" && !/^-?[1-9]\d*$/.test(raw)) return null;
  const parsed = Number(raw);
  return Number.isSafeInteger(parsed) && parsed !== 0 ? parsed : null;
};
