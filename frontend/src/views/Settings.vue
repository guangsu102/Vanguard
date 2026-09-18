<script setup lang="ts">
import { computed, ref, reactive, onMounted } from 'vue'
import { ElButton, ElIcon, ElMessage, ElTabs, ElTabPane, ElForm, ElFormItem, ElInput, ElSwitch, ElCard, ElTable, ElTableColumn, ElTag, ElDivider, ElAlert, ElPopconfirm } from 'element-plus'
import { Select, Download, Delete, FolderOpened, Lock, Refresh } from '@element-plus/icons-vue'
import { authApi } from '@/api/auth'
import { apiConfigsApi, type ApiConfig, type ApiConfigAssignResult, type ApiConfigPlatform } from '@/api/accounts'
import {
  getOwnedGroupAiPersonaFeatureErrorMessage,
  getSettingsApiError,
} from '@/api/settings'
import { useAuthStore } from '@/stores/auth'
import { useSettingsStore } from '@/stores/settings'
import { settingsApi } from '@/api/settings'
import { downloadBlob } from '@/utils/download'
import ClientListPagination from '@/components/ClientListPagination.vue'
import dayjs from 'dayjs'

const settingsStore = useSettingsStore()
const authStore = useAuthStore()
const isAdmin = computed(() => authStore.userInfo?.role === 'admin')

const loading = ref(false)
const activeTab = ref('notification')
const personaRuntimeIntent = ref(false)
const personaFeatureSaving = ref(false)
const personaFeatureConflict = ref(false)
const personaFeature = computed(() => settingsStore.settings?.ownedGroupAiPersona ?? null)

const passwordForm = reactive({
  oldPassword: '',
  newPassword: '',
  confirmPassword: '',
})
const passwordLoading = ref(false)

// ---- API 配置分池（api_id/app_hash 多套池化） ----
const apiConfigList = ref<ApiConfig[]>([])
const apiConfigSummary = ref<ApiConfigSummaryLite>({ max: 30, total: 0, bound: 0, unbound: 0, envFallback: 0, withSession: 0 })
const apiConfigLoading = ref(false)
const apiConfigCreating = ref(false)
const apiConfigAssigning = ref(false)
const apiConfigAssignResult = ref<ApiConfigAssignResult | null>(null)
const apiConfigFormVisible = ref(false)
const apiConfigForm = reactive({
  name: '',
  api_id: '',
  api_hash: '',
  description: '',
  platform: 'any' as ApiConfigPlatform,
})
const apiConfigPlatformOptions: { value: ApiConfigPlatform; label: string }[] = [
  { value: 'windows', label: 'Windows（Telegram Desktop）' },
  { value: 'macos', label: 'macOS' },
  { value: 'android', label: 'Android' },
  { value: 'ios', label: 'iOS' },
  { value: 'any', label: '通用（不限平台）' },
]

interface ApiConfigSummaryLite {
  max: number
  total: number
  bound: number
  unbound: number
  envFallback: number
  withSession: number
}

const loadApiConfigs = async () => {
  apiConfigLoading.value = true
  try {
    const { configs, summary } = await apiConfigsApi.list()
    apiConfigList.value = configs
    if (summary) {
      apiConfigSummary.value = {
        max: summary.max_accounts_per_config ?? 30,
        total: summary.accounts_total ?? 0,
        bound: summary.accounts_bound ?? 0,
        unbound: summary.accounts_unbound ?? 0,
        envFallback: summary.accounts_env_fallback ?? 0,
        withSession: summary.accounts_with_session ?? 0,
      }
    }
  } catch (error) {
    ElMessage.error('加载 API 配置失败')
  } finally {
    apiConfigLoading.value = false
  }
}

const submitApiConfig = async () => {
  if (!apiConfigForm.name.trim() || !apiConfigForm.api_id.trim() || !apiConfigForm.api_hash.trim()) {
    ElMessage.warning('请填写名称、API ID 和 API Hash')
    return
  }
  apiConfigCreating.value = true
  try {
    await apiConfigsApi.create({
      name: apiConfigForm.name.trim(),
      api_id: apiConfigForm.api_id.trim(),
      api_hash: apiConfigForm.api_hash.trim(),
      description: apiConfigForm.description.trim() || undefined,
      platform: apiConfigForm.platform,
    })
    ElMessage.success('API 配置已创建')
    apiConfigFormVisible.value = false
    apiConfigForm.name = ''
    apiConfigForm.api_id = ''
    apiConfigForm.api_hash = ''
    apiConfigForm.description = ''
    apiConfigForm.platform = 'any'
    await loadApiConfigs()
  } catch (error) {
    ElMessage.error('创建 API 配置失败（名称可能已存在）')
  } finally {
    apiConfigCreating.value = false
  }
}

const removeApiConfig = async (name: string) => {
  try {
    await apiConfigsApi.delete(name)
    ElMessage.success('已删除')
    await loadApiConfigs()
  } catch (error) {
    ElMessage.error('删除失败（配置可能仍被账号使用）')
  }
}

const assignApiConfigs = async () => {
  apiConfigAssigning.value = true
  apiConfigAssignResult.value = null
  try {
    apiConfigAssignResult.value = await apiConfigsApi.assign()
    await loadApiConfigs()
  } catch (error) {
    ElMessage.error('分配失败')
  } finally {
    apiConfigAssigning.value = false
  }
}

const notificationForm = reactive({
  sub2apiAlertsEnabled: false,
  sub2apiNotifyResolved: true,
  sub2apiAnnouncementsEnabled: false,
  telegramEnabled: false,
  telegramChatId: '',
  telegramAnnouncementsEnabled: false,
  telegramAnnouncementChatId: '',
  telegramAnnouncementPin: true,
  telegramAnnouncementPinSilent: true,
  qqEnabled: false,
  qqAnnouncementsEnabled: false,
})

const xboardForm = reactive({
  enabled: false,
  callbackEnabled: false,
  protocol: 'hmac',
  source: 'environment',
})

const aiReplyForm = reactive({
  enabled: false,
})

const keywordPrivateReplyForm = reactive({
  enabled: false,
})

const privateMessagingForm = reactive({
  autoReplyEnabled: false,
  manualReplyEnabled: true,
  proactiveEnabled: false,
})

const privateReplyTemplatesForm = reactive<Record<string, string>>({
  startWelcome: '',
  help: '',
  register: '',
  statusFound: '',
  statusPending: '',
  unknownCommand: '',
  thanks: '',
  usageHelp: '',
  registerIntent: '',
  priceIntent: '',
  nodeIntent: '',
  default: '',
  guideWelcome: '',
  guideIntroduce: '',
  guideInviteRegister: '',
  guideConfirm: '',
  guideTimeout: '',
  guideNoNeed: '',
  guideConfirmSuccess: '',
  guideRegisterReminder: '',
  guideFallback: '',
  triggerInvite: '',
})

const privateReplyTemplateGroups = [
  {
    title: '命令回复',
    fields: [
      { key: 'startWelcome', label: '/start 欢迎', rows: 5 },
      { key: 'help', label: '/help 帮助', rows: 6 },
      { key: 'register', label: '/register 注册', rows: 4 },
      { key: 'statusFound', label: '/status 已查询', rows: 2 },
      { key: 'statusPending', label: '/status 查询中', rows: 2 },
      { key: 'unknownCommand', label: '未知命令', rows: 2 },
    ],
  },
  {
    title: '意图回复',
    fields: [
      { key: 'thanks', label: '感谢确认', rows: 2 },
      { key: 'usageHelp', label: '使用帮助', rows: 2 },
      { key: 'registerIntent', label: '注册/试用', rows: 4 },
      { key: 'priceIntent', label: '价格咨询', rows: 3 },
      { key: 'nodeIntent', label: '线路/速度', rows: 3 },
      { key: 'default', label: '默认回复', rows: 3 },
    ],
  },
  {
    title: '引导流程',
    fields: [
      { key: 'guideWelcome', label: '引导欢迎', rows: 2 },
      { key: 'guideIntroduce', label: '服务介绍', rows: 3 },
      { key: 'guideInviteRegister', label: '邀请注册', rows: 3 },
      { key: 'guideConfirm', label: '注册确认', rows: 2 },
      { key: 'guideTimeout', label: '流程超时', rows: 4 },
      { key: 'guideNoNeed', label: '暂不需要', rows: 2 },
      { key: 'guideConfirmSuccess', label: '确认成功', rows: 2 },
      { key: 'guideRegisterReminder', label: '注册链接提醒', rows: 3 },
      { key: 'guideFallback', label: '引导兜底', rows: 2 },
    ],
  },
  {
    title: '保留模板',
    fields: [
      { key: 'triggerInvite', label: '关键词私聊内容', rows: 4 },
    ],
  },
]

const logColumns = [
  { prop: 'id', label: 'ID', width: '80' },
  { prop: 'user', label: '用户', width: '120' },
  { prop: 'action', label: '操作', minWidth: '150' },
  { prop: 'target', label: '对象', minWidth: '150' },
  { prop: 'ip', label: 'IP地址', width: '140' },
  { prop: 'timestamp', label: '时间', width: '180', slot: 'timestamp' },
  { prop: 'status', label: '状态', width: '100', slot: 'status' },
]

const fetchSettings = async () => {
  loading.value = true
  try {
    await settingsStore.fetchSettings()
    await settingsStore.fetchSystemInfo()
    await settingsStore.fetchLogs()

    if (settingsStore.settings) {
      Object.assign(notificationForm, settingsStore.settings.notification || {})
      Object.assign(xboardForm, settingsStore.settings.xboard || {})
      Object.assign(aiReplyForm, settingsStore.settings.aiReply || {})
      Object.assign(keywordPrivateReplyForm, settingsStore.settings.keywordPrivateReply || {})
      personaRuntimeIntent.value = settingsStore.settings.ownedGroupAiPersona.enabled
      personaFeatureConflict.value = false
      const privateMessaging = settingsStore.settings.privateMessaging || {}
      Object.assign(privateMessagingForm, {
        autoReplyEnabled: privateMessaging.autoReplyEnabled ?? privateMessaging.inboundRepliesEnabled ?? false,
        manualReplyEnabled: privateMessaging.manualReplyEnabled ?? true,
        proactiveEnabled: privateMessaging.proactiveEnabled ?? false,
      })
      Object.assign(privateReplyTemplatesForm, privateMessaging.templates || {})
    }
  } catch (error) {
    console.error('Failed to fetch settings:', error)
  } finally {
    loading.value = false
  }
}

const handlePersonaIntentChange = () => {
  personaFeatureConflict.value = false
}

const handleSavePersonaFeature = async () => {
  if (!isAdmin.value || !personaFeature.value) return

  personaFeatureSaving.value = true
  try {
    const saved = await settingsStore.updateOwnedGroupAiPersonaFeature(personaRuntimeIntent.value)
    personaRuntimeIntent.value = saved.enabled
    personaFeatureConflict.value = false
    ElMessage.success('Persona 功能开关已保存')
  } catch (error) {
    const failure = getSettingsApiError(error)
    if (failure.code === 'PERSONA_FEATURE_REVISION_CONFLICT') {
      personaFeatureConflict.value = true
      ElMessage.warning('设置已被其他管理员修改，请确认后重试')
      return
    }
    ElMessage.error(getOwnedGroupAiPersonaFeatureErrorMessage(failure))
  } finally {
    personaFeatureSaving.value = false
  }
}

const handleSaveNotification = async () => {
  try {
    await settingsStore.updateSettings({ notification: notificationForm })
    ElMessage.success('保存成功')
  } catch (error) {
    ElMessage.error('保存失败')
  }
}

const handleSaveAiReply = async () => {
  try {
    await settingsStore.updateSettings({
      aiReply: aiReplyForm,
      keywordPrivateReply: keywordPrivateReplyForm,
      privateMessaging: {
        ...privateMessagingForm,
        templates: privateReplyTemplatesForm,
      },
    })
    ElMessage.success('保存成功')
  } catch (error) {
    ElMessage.error('保存失败')
  }
}

const handleExportLogs = async () => {
  try {
    const response = await settingsApi.exportLogs()
    downloadBlob(response.data, 'vanguard-operation-logs.csv')
  } catch (error) {
    console.error('Failed to export logs:', error)
    ElMessage.error('导出失败')
  }
}

const handleClearLogs = async () => {
  try {
    await settingsStore.clearLogs()
    ElMessage.success('日志已清空')
  } catch (error) {
    ElMessage.error('清空失败')
  }
}

const handleLogPageChange = async (page: number) => {
  settingsStore.setPage(page)
  await settingsStore.fetchLogs()
}

const handleLogPageSizeChange = async (pageSize: number) => {
  settingsStore.setPageSize(pageSize)
  await settingsStore.fetchLogs()
}

const handleBackup = async () => {
  loading.value = true
  try {
    const result = await settingsStore.backupDatabase()
    ElMessage.success(`备份文件: ${result.filename}`)
  } catch (error) {
    ElMessage.error('备份失败')
  } finally {
    loading.value = false
  }
}

const handleChangePassword = async () => {
  if (passwordForm.newPassword.length < 16) {
    ElMessage.error('新密码至少需要 16 个字符')
    return
  }
  if (passwordForm.newPassword !== passwordForm.confirmPassword) {
    ElMessage.error('两次输入的新密码不一致')
    return
  }

  passwordLoading.value = true
  try {
    await authApi.updatePassword({
      oldPassword: passwordForm.oldPassword,
      newPassword: passwordForm.newPassword,
    })
    passwordForm.oldPassword = ''
    passwordForm.newPassword = ''
    passwordForm.confirmPassword = ''
    ElMessage.success('密码修改成功，请使用新密码重新登录')
  } catch (error) {
    console.error('Failed to update password:', error)
    ElMessage.error('密码修改失败，请检查原密码')
  } finally {
    passwordLoading.value = false
  }
}

const formatDate = (date: string) => {
  return dayjs(date).format('YYYY-MM-DD HH:mm:ss')
}

onMounted(() => {
  fetchSettings()
  loadApiConfigs()
})
</script>

<template>
  <div class="settings-page">
    <div class="page-header">
      <h2 class="page-title">系统设置</h2>
    </div>

    <el-tabs v-model="activeTab" class="settings-tabs">
      <el-tab-pane label="通知设置" name="notification">
        <el-card shadow="never">
          <el-form :model="notificationForm" label-width="140px">
            <el-form-item label="Sub2API告警">
              <el-switch v-model="notificationForm.sub2apiAlertsEnabled" />
              <span class="form-tip">接收通过签名验证的 Sub2API 运维告警</span>
            </el-form-item>

            <el-form-item v-if="notificationForm.sub2apiAlertsEnabled" label="恢复通知">
              <el-switch v-model="notificationForm.sub2apiNotifyResolved" />
            </el-form-item>

            <el-form-item label="Sub2API公告">
              <el-switch v-model="notificationForm.sub2apiAnnouncementsEnabled" />
              <span class="form-tip">接收并分发 Sub2API 的公开公告</span>
            </el-form-item>

            <el-divider />

            <el-form-item label="Sub2API Telegram通知">
              <el-switch v-model="notificationForm.telegramEnabled" />
              <span class="form-tip">仅用于签名验证后的 Sub2API 告警；系统任务告警使用服务器告警配置</span>
            </el-form-item>

            <el-form-item v-if="notificationForm.telegramEnabled" label="Telegram Chat ID">
              <el-input v-model="notificationForm.telegramChatId" placeholder="多个 Chat ID 用逗号分隔" style="width: 400px;" />
            </el-form-item>

            <template v-if="notificationForm.sub2apiAnnouncementsEnabled">
              <el-form-item label="公告发到 Telegram">
                <el-switch v-model="notificationForm.telegramAnnouncementsEnabled" />
              </el-form-item>
              <el-form-item v-if="notificationForm.telegramAnnouncementsEnabled" label="公告 Chat ID">
                <el-input v-model="notificationForm.telegramAnnouncementChatId" placeholder="多个 Chat ID 用逗号分隔" style="width: 400px;" />
              </el-form-item>
              <el-form-item v-if="notificationForm.telegramAnnouncementsEnabled" label="自动置顶公告">
                <el-switch v-model="notificationForm.telegramAnnouncementPin" />
              </el-form-item>
              <el-form-item v-if="notificationForm.telegramAnnouncementsEnabled && notificationForm.telegramAnnouncementPin" label="静默置顶">
                <el-switch v-model="notificationForm.telegramAnnouncementPinSilent" />
              </el-form-item>
            </template>

            <el-divider />

            <el-form-item label="QQ 群通知">
              <el-switch v-model="notificationForm.qqEnabled" />
              <span class="form-tip">具体目标群由“QQ 群管理”中的“群通知”开关选择</span>
            </el-form-item>

            <el-form-item v-if="notificationForm.sub2apiAnnouncementsEnabled" label="公告发到 QQ 群">
              <el-switch v-model="notificationForm.qqAnnouncementsEnabled" />
              <span class="form-tip">目标群沿用“QQ 群管理”中的“群通知”开关</span>
            </el-form-item>

            <el-form-item>
              <el-button type="primary" @click="handleSaveNotification">
                <el-icon><Select /></el-icon>
                保存设置
              </el-button>
            </el-form-item>
          </el-form>
        </el-card>
      </el-tab-pane>

      <el-tab-pane label="API设置" name="api">
        <el-card shadow="never">
          <el-alert
            title="XBoard 配置由服务器环境变量统一管理"
            type="info"
            :closable="false"
            style="margin-bottom: 20px;"
          />

          <el-descriptions :column="1" border>
            <el-descriptions-item label="集成状态">
              <el-tag :type="xboardForm.enabled ? 'success' : 'info'">
                {{ xboardForm.enabled ? '已启用' : '未启用' }}
              </el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="接口协议">
              {{ xboardForm.protocol === 'hmac' ? 'HMAC 签名 API' : xboardForm.protocol }}
            </el-descriptions-item>
            <el-descriptions-item label="回调接口">
              {{ xboardForm.callbackEnabled ? '已启用' : '未启用' }}
            </el-descriptions-item>
            <el-descriptions-item label="配置来源">
              {{ xboardForm.source === 'environment' ? '服务器环境变量' : xboardForm.source }}
            </el-descriptions-item>
          </el-descriptions>
        </el-card>
      </el-tab-pane>

      <el-tab-pane label="API 分池" name="apiPool">
        <el-card shadow="never">
          <el-alert
            type="info"
            :closable="false"
            style="margin-bottom: 16px;"
            title="多套 api_id/app_hash 分池：同一 api_id 下所有账号共享反垃圾声誉，按设备平台匹配配置，每套配置最多挂 30 个账号。已有会话的账号不会切换 api_id（需重新登录时才生效）。"
          />
          <div class="api-pool-toolbar">
            <span class="form-tip">
              账号 {{ apiConfigSummary.total }} 个：{{ apiConfigSummary.bound }} 个已显式绑定配置；
              {{ apiConfigSummary.envFallback }} 个未绑定配置、运行时经环境变量回退使用环境 api_id
              （重新登录后才会显式绑定；这些账号已计入对应配置的负载与 30 上限）
            </span>
            <el-button :icon="Refresh" :loading="apiConfigLoading" @click="loadApiConfigs">刷新</el-button>
            <el-button type="primary" :loading="apiConfigAssigning" @click="assignApiConfigs">一键分配</el-button>
            <el-button type="success" @click="apiConfigFormVisible = true">新增配置</el-button>
          </div>

          <el-alert
            v-if="apiConfigAssignResult"
            type="success"
            :closable="true"
            style="margin-bottom: 16px;"
            :title="`分配完成：本次绑定 ${apiConfigAssignResult.assigned.length} 个，待重新登录生效 ${apiConfigAssignResult.needs_relogin.length} 个，失败 ${apiConfigAssignResult.failed.length} 个`"
          />

          <el-table :data="apiConfigList" v-loading="apiConfigLoading" style="width: 100%">
            <el-table-column prop="name" label="名称" min-width="120" />
            <el-table-column prop="platform" label="平台" width="110">
              <template #default="{ row }">
                <el-tag :type="row.platform === 'any' ? 'info' : 'warning'" effect="plain">
                  {{ row.platform }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="api_id" label="API ID" min-width="110" />
            <el-table-column label="账号数" width="200">
              <template #default="{ row }">
                {{ row.account_count }} / {{ apiConfigSummary.max }}
                <el-tag v-if="row.under_cap === false" type="danger" size="small" effect="plain">已满</el-tag>
                <div v-if="row.env_fallback_count > 0" class="form-tip" style="margin-left: 0;">
                  含环境回退 {{ row.env_fallback_count }} 个，显式绑定 {{ row.explicit_account_count }} 个
                </div>
              </template>
            </el-table-column>
            <el-table-column prop="description" label="备注" min-width="160" show-overflow-tooltip />
            <el-table-column label="操作" width="90">
              <template #default="{ row }">
                <el-popconfirm title="确认删除该配置？" @confirm="removeApiConfig(row.name)">
                  <template #reference>
                    <el-button size="small" type="danger" plain>删除</el-button>
                  </template>
                </el-popconfirm>
              </template>
            </el-table-column>
          </el-table>

          <el-divider />

          <el-form v-if="apiConfigFormVisible" :model="apiConfigForm" label-width="110px" style="max-width: 560px;">
            <el-form-item label="配置名称" required>
              <el-input v-model="apiConfigForm.name" placeholder="如 ios-pool-1" maxlength="50" />
            </el-form-item>
            <el-form-item label="API ID" required>
              <el-input v-model="apiConfigForm.api_id" placeholder="my.telegram.org 申请的 api_id" />
            </el-form-item>
            <el-form-item label="API Hash" required>
              <el-input v-model="apiConfigForm.api_hash" placeholder="my.telegram.org 申请的 api_hash" show-password />
            </el-form-item>
            <el-form-item label="注册平台" required>
              <el-select v-model="apiConfigForm.platform" style="width: 100%;">
                <el-option
                  v-for="option in apiConfigPlatformOptions"
                  :key="option.value"
                  :label="option.label"
                  :value="option.value"
                />
              </el-select>
            </el-form-item>
            <el-form-item label="备注">
              <el-input v-model="apiConfigForm.description" maxlength="200" />
            </el-form-item>
            <el-form-item>
              <el-button type="primary" :loading="apiConfigCreating" @click="submitApiConfig">保存</el-button>
              <el-button @click="apiConfigFormVisible = false">取消</el-button>
            </el-form-item>
          </el-form>
        </el-card>
      </el-tab-pane>

      <el-tab-pane label="AI回复设置" name="aiReply">
        <el-card shadow="never">
          <el-alert
            title="私聊消息始终进入工作台；自动回复与人工回复分别控制。"
            type="warning"
            :closable="false"
            style="margin-bottom: 20px;"
          />

          <el-form :model="aiReplyForm" label-width="160px">
            <el-divider content-position="left">自建群账号 Persona</el-divider>

            <el-alert
              v-if="personaFeature && !personaFeature.staticEnabled"
              title="需启用环境开关并重启后端才会生效"
              type="warning"
              :closable="false"
              style="margin-bottom: 20px;"
            />

            <el-descriptions v-if="personaFeature" :column="1" border class="persona-feature-status">
              <el-descriptions-item label="静态开关">
                <el-tag :type="personaFeature.staticEnabled ? 'success' : 'info'">
                  {{ personaFeature.staticEnabled ? '已启用' : '未启用' }}
                </el-tag>
              </el-descriptions-item>
              <el-descriptions-item label="运行时开关">
                <template v-if="isAdmin">
                  <el-switch
                    v-model="personaRuntimeIntent"
                    :loading="personaFeatureSaving"
                    @change="handlePersonaIntentChange"
                  />
                  <span class="form-tip">保存后只影响之后创建的 AI 任务</span>
                </template>
                <el-tag v-else :type="personaFeature.enabled ? 'success' : 'info'">
                  {{ personaFeature.enabled ? '已启用' : '未启用' }}
                </el-tag>
              </el-descriptions-item>
              <el-descriptions-item label="最终生效">
                <el-tag :type="personaFeature.effectiveEnabled ? 'success' : 'info'">
                  {{ personaFeature.effectiveEnabled ? '已生效' : '未生效' }}
                </el-tag>
                <span class="form-tip">静态开关与运行时开关同时启用才会生效</span>
              </el-descriptions-item>
              <el-descriptions-item label="设置版本">
                v{{ personaFeature.revision }}
                <span v-if="personaFeature.updatedAt" class="form-tip">
                  最近更新：{{ formatDate(personaFeature.updatedAt) }}
                </span>
              </el-descriptions-item>
            </el-descriptions>

            <el-form-item v-if="isAdmin && personaFeature" class="persona-feature-save">
              <el-button
                data-testid="save-persona-feature"
                type="primary"
                :loading="personaFeatureSaving"
                @click="handleSavePersonaFeature"
              >
                {{ personaFeatureConflict ? '确认并重试' : '保存 Persona 开关' }}
              </el-button>
            </el-form-item>

            <el-divider content-position="left">私聊 AI 与模板</el-divider>

            <el-form-item label="私聊自动回复">
              <el-switch v-model="privateMessagingForm.autoReplyEnabled" />
              <span class="form-tip">默认关闭，开启后按下方模板处理未被人工接管的会话</span>
            </el-form-item>

            <el-form-item label="允许人工回复">
              <el-switch v-model="privateMessagingForm.manualReplyEnabled" />
              <span class="form-tip">控制运营人员是否可以在私聊工作台发送回复</span>
            </el-form-item>

            <el-form-item label="主动私聊触达">
              <el-switch v-model="privateMessagingForm.proactiveEnabled" />
              <span class="form-tip">开启后允许系统按策略主动发起私聊触达</span>
            </el-form-item>

            <el-form-item label="关键词私聊回复">
              <el-switch v-model="keywordPrivateReplyForm.enabled" />
              <span class="form-tip">保留配置项；当前主动私聊策略关闭时不会发出</span>
            </el-form-item>

            <el-form-item label="启用AI自动回复">
              <el-switch v-model="aiReplyForm.enabled" />
              <span class="form-tip">默认关闭，只有开启后才允许调用模型</span>
            </el-form-item>

            <el-divider content-position="left">用户主动私聊模板</el-divider>

            <div class="template-vars">
              可用变量：{user_name}、{user_id}、{register_link}、{status}、{message_text}、{command}、{keyword}
            </div>

            <div
              v-for="group in privateReplyTemplateGroups"
              :key="group.title"
              class="template-group"
            >
              <div class="template-group-title">{{ group.title }}</div>
              <el-form-item
                v-for="field in group.fields"
                :key="field.key"
                :label="field.label"
              >
                <el-input
                  v-model="privateReplyTemplatesForm[field.key]"
                  type="textarea"
                  :rows="field.rows"
                  maxlength="2000"
                  show-word-limit
                  class="template-input"
                />
              </el-form-item>
            </div>

            <el-form-item>
              <el-button type="primary" @click="handleSaveAiReply">
                <el-icon><Select /></el-icon>
                保存设置
              </el-button>
            </el-form-item>
          </el-form>
        </el-card>
      </el-tab-pane>

      <el-tab-pane label="操作日志" name="logs">
        <el-card shadow="never">
          <div class="log-actions">
            <el-button @click="handleExportLogs">
              <el-icon><Download /></el-icon>
              导出日志
            </el-button>
            <el-button type="danger" @click="handleClearLogs">
              <el-icon><Delete /></el-icon>
              清空日志
            </el-button>
          </div>

          <el-table :data="settingsStore.logs" stripe style="margin-top: 16px;">
            <el-table-column v-for="col in logColumns" :key="col.prop" v-bind="col">
              <template v-if="col.prop === 'timestamp'" #default="{ row }">
                {{ formatDate(row.timestamp) }}
              </template>
              <template v-else-if="col.prop === 'status'" #default="{ row }">
                <el-tag :type="row.status === 'success' ? 'success' : 'danger'" size="small">
                  {{ row.status === 'success' ? '成功' : '失败' }}
                </el-tag>
              </template>
            </el-table-column>
          </el-table>
          <ClientListPagination
            :page="settingsStore.page"
            :page-size="settingsStore.pageSize"
            :total="settingsStore.logTotal"
            @update:page="handleLogPageChange"
            @update:page-size="handleLogPageSizeChange"
          />
        </el-card>
      </el-tab-pane>

      <el-tab-pane label="系统信息" name="system">
        <el-card shadow="never">
          <el-descriptions :column="2" border>
            <el-descriptions-item label="系统版本">v1.0.0</el-descriptions-item>
            <el-descriptions-item label="Python版本">{{ settingsStore.systemInfo?.pythonVersion || '-' }}</el-descriptions-item>
            <el-descriptions-item label="数据库">{{ settingsStore.systemInfo?.database || '-' }}</el-descriptions-item>
            <el-descriptions-item label="Redis">{{ settingsStore.systemInfo?.redis || '-' }}</el-descriptions-item>
            <el-descriptions-item label="运行时长">{{ settingsStore.systemInfo?.uptime || '-' }}</el-descriptions-item>
            <el-descriptions-item label="最后备份">{{ settingsStore.systemInfo?.lastBackup || '从未备份' }}</el-descriptions-item>
          </el-descriptions>

          <div class="system-actions">
            <el-button type="primary" @click="handleBackup" :loading="loading">
              <el-icon><FolderOpened /></el-icon>
              备份数据库
            </el-button>
          </div>
        </el-card>
      </el-tab-pane>

      <el-tab-pane label="修改密码" name="password">
        <el-card shadow="never" class="password-card">
          <el-alert
            title="为保护生产环境，请立即将初始密码修改为个人强密码。新密码至少 16 个字符。"
            type="warning"
            :closable="false"
            show-icon
          />
          <el-form :model="passwordForm" label-width="140px" class="password-form" @submit.prevent="handleChangePassword">
            <el-form-item label="原密码" required>
              <el-input v-model="passwordForm.oldPassword" type="password" show-password autocomplete="current-password" />
            </el-form-item>
            <el-form-item label="新密码" required>
              <el-input v-model="passwordForm.newPassword" type="password" show-password autocomplete="new-password" />
            </el-form-item>
            <el-form-item label="确认新密码" required>
              <el-input v-model="passwordForm.confirmPassword" type="password" show-password autocomplete="new-password" />
            </el-form-item>
            <el-form-item>
              <el-button type="primary" :loading="passwordLoading" :icon="Lock" @click="handleChangePassword">
                修改密码
              </el-button>
            </el-form-item>
          </el-form>
        </el-card>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<style scoped lang="scss">
.settings-page {
  padding: 0;
}

.page-header {
  margin-bottom: 20px;
}

.page-title {
  font-size: 20px;
  font-weight: 600;
  margin: 0;
  color: #303133;
}

.settings-tabs {
  :deep(.el-tabs__header) {
    margin-bottom: 20px;
  }
}

.form-tip {
  margin-left: 12px;
  color: #909399;
  font-size: 12px;
}

.api-pool-toolbar {
  display: flex;
  align-items: center;
  gap: 4px;
  margin-bottom: 16px;

  .form-tip {
    flex: 1;
    margin-left: 0;
  }
}

.template-vars {
  margin: -4px 0 18px 160px;
  color: #606266;
  font-size: 12px;
}

.persona-feature-status {
  max-width: 760px;
  margin-bottom: 18px;
}

.persona-feature-save {
  margin-top: 16px;
}

.template-group {
  max-width: 920px;
  margin-bottom: 18px;
}

.template-group-title {
  margin: 0 0 12px 160px;
  color: #303133;
  font-size: 14px;
  font-weight: 600;
}

.template-input {
  width: min(680px, 100%);
}

.log-actions {
  display: flex;
  gap: 12px;
}

.system-actions {
  margin-top: 24px;
  padding-top: 24px;
  border-top: 1px solid #ebeef5;
}
</style>
