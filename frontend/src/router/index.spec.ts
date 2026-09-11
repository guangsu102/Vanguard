import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";

const getUserInfo = vi.hoisted(() => vi.fn());
vi.mock("@/api/auth", () => ({ authApi: { getUserInfo, login: vi.fn(), logout: vi.fn(), updatePassword: vi.fn() } }));
vi.mock("nprogress", () => ({ default: { configure: vi.fn(), start: vi.fn(), done: vi.fn() } }));
import router from "./index";

describe("group operations route guard", () => {
  beforeEach(async () => {
    setActivePinia(createPinia());
    localStorage.clear();
    if (router.currentRoute.value.path !== "/dashboard") await router.replace("/dashboard").catch(() => undefined);
  });

  it("restores user info before denying a first direct visit by an unknown role", async () => {
    localStorage.setItem("token", "stored-token");
    getUserInfo.mockResolvedValue({ data: { data: { id: 7, username: "viewer", role: "viewer" } } });
    await router.push("/owned-groups/18/operations");
    await router.isReady();
    expect(getUserInfo).toHaveBeenCalled();
    expect(router.currentRoute.value.path).toBe("/dashboard");
  }, 15_000);
});
