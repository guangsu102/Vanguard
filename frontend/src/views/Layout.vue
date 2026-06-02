<script setup lang="ts">
import { ref } from 'vue'
import { ElContainer, ElAside, ElMenu, ElMenuItem, ElSubMenu, ElIcon, ElScrollbar, ElDropdown, ElDropdownMenu, ElDropdownItem, ElAvatar, ElBadge, ElMessage } from 'element-plus'
import {
  Odometer, User, ChatDotRound,
  Key, UserFilled, Present, SetUp, DataLine, Setting,
  Fold, Expand, Bell, SwitchButton, User as UserIcon, Operation, Promotion, Monitor
} from '@element-plus/icons-vue'
import { useRouter, useRoute } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

const router = useRouter()
const route = useRoute()
const authStore = useAuthStore()

const isCollapse = ref(false)

const menuItems = [
  { path: '/dashboard', title: '仪表盘', icon: Odometer },
  {
    path: '/growth',
    title: '增长中心',
    icon: Promotion,
    children: [
      { path: '/accounts', title: '推广账号', icon: User },
      { path: '/groups', title: '群池管理', icon: ChatDotRound },
      { path: '/keywords', title: '关键词管理', icon: Key },
      { path: '/campaigns', title: '活动管理', icon: Present },
      { path: '/automation', title: '自动化管理', icon: Promotion },
      { path: '/workers', title: '执行器状态', icon: Monitor },
    ],
  },
  {
    path: '/guardian',
    title: '群治理中心',
    icon: SetUp,
    children: [
      { path: '/guardian/bots', title: 'Bot账号', icon: User },
      { path: '/guardian/groups', title: 'Bot管理群', icon: ChatDotRound },
      { path: '/guardian/policies', title: '群治理策略', icon: SetUp },
      { path: '/guardian/keywords', title: '群管敏感词', icon: Key },
    ],
  },
  { path: '/rules', title: '审核规则', icon: SetUp },
  { path: '/users', title: '用户管理', icon: UserFilled },
  { path: '/stats', title: '数据统计', icon: DataLine },
  { path: '/settings', title: '系统设置', icon: Setting },
]

const handleMenuSelect = (index: string) => {
  router.push(index)
}

const handleLogout = async () => {
  await authStore.logout()
  ElMessage.success('已退出登录')
}

const toggleCollapse = () => {
  isCollapse.value = !isCollapse.value
}
</script>

<template>
  <el-container class="layout-container">
    <el-aside :width="isCollapse ? '64px' : '200px'" class="sidebar">
      <div class="logo">
        <el-icon v-if="isCollapse" class="logo-icon"><Operation /></el-icon>
        <span v-else class="logo-text">Vanguard</span>
      </div>
      <el-scrollbar>
        <el-menu
          :default-active="route.path"
          :collapse="isCollapse"
          :collapse-transition="false"
          router
          class="sidebar-menu"
        >
          <template v-for="item in menuItems" :key="item.path">
            <el-sub-menu v-if="item.children" :index="item.path">
              <template #title>
                <el-icon><component :is="item.icon" /></el-icon>
                <span>{{ item.title }}</span>
              </template>
              <el-menu-item
                v-for="child in item.children"
                :key="child.path"
                :index="child.path"
                @click="handleMenuSelect(child.path)"
              >
                <el-icon><component :is="child.icon" /></el-icon>
                <template #title>{{ child.title }}</template>
              </el-menu-item>
            </el-sub-menu>
            <el-menu-item
              v-else
              :index="item.path"
              @click="handleMenuSelect(item.path)"
            >
              <el-icon><component :is="item.icon" /></el-icon>
              <template #title>{{ item.title }}</template>
            </el-menu-item>
          </template>
        </el-menu>
      </el-scrollbar>
    </el-aside>

    <el-container>
      <el-header class="header">
        <div class="header-left">
          <el-icon class="collapse-btn" @click="toggleCollapse">
            <component :is="isCollapse ? 'Expand' : 'Fold'" />
          </el-icon>
          <el-breadcrumb separator="/">
            <el-breadcrumb-item :to="{ path: '/dashboard' }">首页</el-breadcrumb-item>
            <el-breadcrumb-item>{{ route.meta.title }}</el-breadcrumb-item>
          </el-breadcrumb>
        </div>
        <div class="header-right">
          <el-badge :value="0" :max="99" class="header-badge">
            <el-icon size="20" class="header-icon"><Bell /></el-icon>
          </el-badge>

          <el-dropdown trigger="click" @command="handleLogout">
            <div class="user-info">
              <el-avatar :size="32" style="background: #409eff;">
                <el-icon><UserIcon /></el-icon>
              </el-avatar>
              <span class="username">{{ authStore.userInfo?.username || 'Admin' }}</span>
              <el-icon class="dropdown-arrow"><SwitchButton /></el-icon>
            </div>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item>
                  <el-icon><UserIcon /></el-icon>
                  个人中心
                </el-dropdown-item>
                <el-dropdown-item divided @click="handleLogout">
                  <el-icon><SwitchButton /></el-icon>
                  退出登录
                </el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </el-header>

      <el-main class="main-content">
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<script lang="ts">
import { Fold, Expand, Bell, SwitchButton, User as UserIcon, Operation } from '@element-plus/icons-vue'
export default {
  components: { Fold, Expand, Bell, SwitchButton, UserIcon, Operation },
}
</script>

<style scoped lang="scss">
.layout-container {
  height: 100vh;
}

.sidebar {
  background: #1a1a2e;
  transition: width 0.3s;
  overflow: hidden;
  box-shadow: 2px 0 8px rgba(0, 0, 0, 0.1);
}

.logo {
  height: 60px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #16213e;
  border-bottom: 1px solid rgba(255, 255, 255, 0.1);
}

.logo-icon {
  font-size: 28px;
  color: #409eff;
}

.logo-text {
  font-size: 20px;
  font-weight: 700;
  color: #409eff;
  letter-spacing: 1px;
}

.sidebar-menu {
  border-right: none;
  background: transparent;
}

:deep(.el-menu) {
  background: transparent !important;
}

:deep(.el-menu-item) {
  color: rgba(255, 255, 255, 0.7) !important;
  height: 50px;
  line-height: 50px;
  margin: 4px 8px;
  border-radius: 8px;

  &:hover {
    background: rgba(64, 158, 255, 0.1) !important;
    color: #409eff !important;
  }

  &.is-active {
    background: #409eff !important;
    color: white !important;
  }
}

:deep(.el-sub-menu__title) {
  color: rgba(255, 255, 255, 0.75) !important;
  margin: 4px 8px;
  border-radius: 8px;
}

:deep(.el-sub-menu__title:hover) {
  background: rgba(64, 158, 255, 0.08) !important;
  color: #409eff !important;
}

:deep(.el-menu--collapse .el-menu-item) {
  padding: 0 !important;
  justify-content: center;
}

.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: white;
  border-bottom: 1px solid #e6e6e6;
  padding: 0 20px;
  height: 60px;
}

.header-left {
  display: flex;
  align-items: center;
  gap: 16px;
}

.collapse-btn {
  font-size: 20px;
  cursor: pointer;
  color: #606266;
  padding: 8px;
  border-radius: 4px;
  transition: all 0.3s;

  &:hover {
    color: #409eff;
    background: #f5f7fa;
  }
}

.header-right {
  display: flex;
  align-items: center;
  gap: 20px;
}

.header-badge {
  cursor: pointer;

  .header-icon {
    color: #606266;
    transition: color 0.3s;

    &:hover {
      color: #409eff;
    }
  }
}

.user-info {
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  padding: 4px 8px;
  border-radius: 4px;
  transition: background 0.3s;

  &:hover {
    background: #f5f7fa;
  }
}

.username {
  font-size: 14px;
  color: #303133;
  max-width: 100px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.dropdown-arrow {
  color: #909399;
  font-size: 12px;
}

.main-content {
  background: #f0f2f5;
  padding: 20px;
  overflow-y: auto;
}
</style>
