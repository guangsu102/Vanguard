import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useAuthStore } from "@/stores/auth";

const getUserInfo = vi.hoisted(() => vi.fn());
vi.mock("@/api/auth", () => ({ authApi: { getUserInfo, login: vi.fn(), logout: vi.fn(), updatePassword: vi.fn() } }));
vi.mock("nprogress", () => ({ default: { configure: vi.fn(), start: vi.fn(), done: vi.fn() } }));
import router from "./index";

describe("group operations route guard", () => {
  beforeEach(async () => {
    localStorage.clear();
    getUserInfo.mockReset();
    setActivePinia(createPinia());
    if (router.currentRoute.value.path !== "/login") await router.replace("/login").catch(() => undefined);
  });

  it("restores user info for an ordinary protected route when the token was persisted", async () => {
    localStorage.setItem("token", "stored-token");
    setActivePinia(createPinia());
    const authStore = useAuthStore();
    getUserInfo.mockResolvedValue({ data: { data: { id: 1, username: "admin", role: "admin" } } });

    expect(authStore.token).toBe("stored-token");
    expect(authStore.userInfo).toBeNull();

    await router.push("/dashboard");
    await router.isReady();

    expect(getUserInfo).toHaveBeenCalledTimes(1);
    expect(authStore.userInfo?.role).toBe("admin");
    expect(router.currentRoute.value.path).toBe("/dashboard");
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
