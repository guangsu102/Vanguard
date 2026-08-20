<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { ElButton, ElIcon, ElMessage, ElTabs, ElTabPane, ElForm, ElFormItem, ElInput, ElSwitch, ElCard, ElTable, ElTag, ElDivider, ElAlert } from 'element-plus'
import { Select, Download, Delete, FolderOpened } from '@element-plus/icons-vue'
import { useSettingsStore } from '@/stores/settings'
import { settingsApi } from '@/api/settings'
import { downloadBlob } from '@/utils/download'
import dayjs from 'dayjs'

const settingsStore = useSettingsStore()

const loading = ref(false)
const activeTab = ref('notification')

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
  inboundRepliesEnabled: true,
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
      const privateMessaging = settingsStore.settings.privateMessaging || {}
      Object.assign(privateMessagingForm, {
        inboundRepliesEnabled: privateMessaging.inboundRepliesEnabled ?? true,
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

const formatDate = (date: string) => {
  return dayjs(date).format('YYYY-MM-DD HH:mm:ss')
}

onMounted(() => {
  fetchSettings()
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

      <el-tab-pane label="AI回复设置" name="aiReply">
        <el-card shadow="never">
          <el-alert
            title="推广账号不再主动私聊；只有用户先发私聊进来，系统才允许回复。"
            type="warning"
            :closable="false"
            style="margin-bottom: 20px;"
          />

          <el-form :model="aiReplyForm" label-width="160px">
            <el-form-item label="用户私聊后回复">
              <el-switch v-model="privateMessagingForm.inboundRepliesEnabled" />
              <span class="form-tip">开启后，仅当用户主动私聊推广账号时才允许回复</span>
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

.template-vars {
  margin: -4px 0 18px 160px;
  color: #606266;
  font-size: 12px;
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
