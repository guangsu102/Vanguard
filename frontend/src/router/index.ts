import { createRouter, createWebHistory, RouteRecordRaw } from 'vue-router'
import NProgress from 'nprogress'
import 'nprogress/nprogress.css'
import { useAuthStore } from '@/stores/auth'

NProgress.configure({ showSpinner: false })

const routes: RouteRecordRaw[] = [
  {
    path: '/login',
    name: 'Login',
    component: () => import('@/views/Login.vue'),
    meta: { title: '登录', requiresAuth: false },
  },
  {
    path: '/',
    component: () => import('@/views/Layout.vue'),
    redirect: '/dashboard',
    meta: { requiresAuth: true },
    children: [
      {
        path: 'dashboard',
        name: 'Dashboard',
        component: () => import('@/views/Dashboard.vue'),
        meta: { title: '仪表盘', icon: 'Odometer' },
      },
      {
        path: 'accounts',
        name: 'Accounts',
        component: () => import('@/views/Accounts.vue'),
        meta: { title: '推广账号', icon: 'User' },
      },
      {
        path: 'private-chats',
        name: 'PrivateChats',
        component: () => import('@/views/PrivateChats.vue'),
        meta: { title: '私聊工作台', icon: 'ChatLineSquare' },
      },
      {
        path: 'growth-dashboard',
        name: 'GrowthDashboard',
        component: () => import('@/views/GrowthDashboard.vue'),
        meta: { title: '增长驾驶舱', icon: 'Promotion' },
      },
      {
        path: 'growth-settings',
        name: 'GrowthSettings',
        component: () => import('@/views/GrowthSettings.vue'),
        meta: { title: '配置中心', icon: 'Setting' },
      },
      {
        path: 'growth-logs',
        name: 'GrowthLogs',
        component: () => import('@/views/GrowthLogs.vue'),
        meta: { title: '增长日志', icon: 'Document' },
      },
      {
        path: 'proxies',
        name: 'Proxies',
        component: () => import('@/views/Proxies.vue'),
        meta: { title: '静态代理IP', icon: 'Connection' },
      },
      {
        path: 'groups',
        name: 'Groups',
        component: () => import('@/views/Groups.vue'),
        meta: { title: '群池管理', icon: 'ChatDotRound' },
      },
      {
        path: 'keywords',
        name: 'Keywords',
        component: () => import('@/views/Keywords.vue'),
        meta: { title: '关键词管理', icon: 'Key' },
      },
      {
        path: 'users',
        name: 'Users',
        component: () => import('@/views/Users.vue'),
        meta: { title: '用户管理', icon: 'UserFilled' },
      },
      {
        path: 'campaigns',
        name: 'Campaigns',
        component: () => import('@/views/Campaigns.vue'),
        meta: { title: '活动管理', icon: 'Gift' },
      },
      {
        path: 'automation',
        name: 'Automation',
        component: () => import('@/views/Automation.vue'),
        meta: { title: '自动化管理', icon: 'Promotion' },
      },
      {
        path: 'workers',
        name: 'WorkerStatus',
        component: () => import('@/views/WorkerStatus.vue'),
        meta: { title: '执行器状态', icon: 'Monitor' },
      },
      {
        path: 'guardian/bots',
        name: 'GuardianBots',
        component: () => import('@/views/GuardianBots.vue'),
        meta: { title: 'Bot账号', icon: 'User' },
      },
      {
        path: 'guardian/groups',
        name: 'ManagedGroups',
        component: () => import('@/views/ManagedGroups.vue'),
        meta: { title: 'Bot管理群', icon: 'ChatDotRound' },
      },
      {
        path: 'guardian/qq',
        name: 'QQGroups',
        component: () => import('@/views/QQGroups.vue'),
        meta: { title: 'NapCat QQ群', icon: 'ChatLineSquare' },
      },
      {
        path: 'guardian/keywords',
        name: 'ModerationSensitiveKeywords',
        component: () => import('@/views/ModerationSensitiveKeywords.vue'),
        meta: { title: '群管敏感词', icon: 'Key' },
      },
      {
        path: 'guardian/policies',
        name: 'GroupGovernancePolicies',
        component: () => import('@/views/GroupGovernancePolicies.vue'),
        meta: { title: '群治理策略', icon: 'SetUp' },
      },
      {
        path: 'rules',
        name: 'Rules',
        component: () => import('@/views/Rules.vue'),
        meta: { title: '审核规则', icon: 'SetUp' },
      },
      {
        path: 'stats',
        name: 'Stats',
        component: () => import('@/views/Stats.vue'),
        meta: { title: '数据统计', icon: 'DataLine' },
      },
      {
        path: 'settings',
        name: 'Settings',
        component: () => import('@/views/Settings.vue'),
        meta: { title: '系统设置', icon: 'Setting' },
      },
    ],
  },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

router.beforeEach(async (to, _from, next) => {
  NProgress.start()

  const authStore = useAuthStore()
  const requiresAuth = to.matched.some((record) => record.meta.requiresAuth !== false)

  // Check if route requires authentication
  if (requiresAuth && !authStore.isAuthenticated()) {
    // Try to get token from localStorage
    const token = localStorage.getItem('token')
    if (token) {
      authStore.setToken(token)
      try {
        await authStore.fetchUserInfo()
        document.title = `${to.meta.title || ''} - Vanguard`
        next()
      } catch {
        localStorage.removeItem('token')
        next('/login')
      }
    } else {
      next('/login')
    }
    return
  }

  // Redirect to dashboard if already logged in and trying to access login
  if (to.path === '/login' && authStore.isAuthenticated()) {
    next('/dashboard')
    return
  }

  document.title = `${to.meta.title || ''} - Vanguard`
  next()
})

router.afterEach(() => {
  NProgress.done()
})

export default router
