<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { ElButton, ElCard, ElForm, ElFormItem, ElInput, ElInputNumber, ElMessage, ElOption, ElSelect, ElSwitch } from 'element-plus'
import { guardianApi, type ManagedGroupBinding } from '@/api/guardian'

const route = useRoute()
const loading = ref(false)
const currentGroupId = ref<number>()
const managedGroups = ref<ManagedGroupBinding[]>([])

const verificationForm = reactive({
  group_id: 0,
  enable_verification: false,
  verification_type: 'captcha',
  timeout_minutes: 5,
  max_attempts: 3,
  whitelist_bypass: true,
  auto_kick_unverified: false,
  kick_after_minutes: 10,
  welcome_message: '',
  questions: [] as any[],
})

const moderationForm = reactive({
  group_id: 0,
  message_interval_seconds: 10,
  max_messages_per_minute: 5,
  max_links_per_hour: 3,
  new_member_silent_minutes: 5,
  first_speak_delay_seconds: 30,
})

const punishmentForm = reactive({
  group_id: 0,
  warn_threshold: 3,
  mute_on_warn_threshold: true,
  mute_duration_seconds: 300,
  ban_on_warn_threshold: 5,
  repeat_violation_window_hours: 24,
  auto_reset_warning_days: 7,
  severe_violation_direct_action: 'mute',
})

const currentGroupLabel = computed(() => {
  const matched = managedGroups.value.find((item) => item.telegram_group_id === currentGroupId.value)
  if (matched) {
    return matched.title || matched.username || String(matched.telegram_group_id)
  }
  const title = route.query.title
  return typeof title === 'string' && title ? title : ''
})

const loadManagedGroups = async () => {
  const res = await guardianApi.listManagedGroups({ limit: 200 })
  managedGroups.value = res.data.data
}

const applyRouteGroup = () => {
  const groupId = Number(route.query.groupId)
  if (Number.isFinite(groupId) && groupId > 0) {
    currentGroupId.value = groupId
  }
}

const loadPolicies = async () => {
  if (!currentGroupId.value) {
    ElMessage.warning('请先输入群 ID')
    return
  }
  loading.value = true
  try {
    const [verificationRes, moderationRes, punishmentRes] = await Promise.allSettled([
      guardianApi.getVerificationPolicy(currentGroupId.value),
      guardianApi.getModerationPolicy(currentGroupId.value),
      guardianApi.getPunishmentPolicy(currentGroupId.value),
    ])

    if (verificationRes.status === 'fulfilled') Object.assign(verificationForm, verificationRes.value.data.data)
    if (moderationRes.status === 'fulfilled') Object.assign(moderationForm, moderationRes.value.data.data)
    if (punishmentRes.status === 'fulfilled') Object.assign(punishmentForm, punishmentRes.value.data.data)
  } finally {
    loading.value = false
  }
}

const saveAll = async () => {
  if (!currentGroupId.value) {
    ElMessage.warning('请先输入群 ID')
    return
  }
  verificationForm.group_id = currentGroupId.value
  moderationForm.group_id = currentGroupId.value
  punishmentForm.group_id = currentGroupId.value
  loading.value = true
  try {
    await Promise.all([
      guardianApi.saveVerificationPolicy({ ...verificationForm }),
      guardianApi.saveModerationPolicy({ ...moderationForm }),
      guardianApi.savePunishmentPolicy({ ...punishmentForm }),
    ])
    ElMessage.success('群治理策略已保存')
  } finally {
    loading.value = false
  }
}

watch(
  () => route.query.groupId,
  () => {
    applyRouteGroup()
    if (currentGroupId.value) {
      loadPolicies()
    }
  },
)

onMounted(async () => {
  await loadManagedGroups()
  applyRouteGroup()
  if (currentGroupId.value) {
    await loadPolicies()
  }
})
</script>

<template>
  <div class="page-shell">
    <div class="page-header">
      <div>
        <h2 class="page-title">群治理策略</h2>
        <p class="page-desc">按群配置验证、发言限制和处罚阈值，群级覆盖全局默认。</p>
        <p v-if="currentGroupLabel" class="page-subtitle">当前管理群：{{ currentGroupLabel }}</p>
      </div>
      <div class="header-actions">
        <el-select v-model="currentGroupId" filterable placeholder="选择 Bot 管理群" style="width: 300px">
          <el-option
            v-for="item in managedGroups"
            :key="item.id"
            :label="`${item.title || item.username || item.telegram_group_id} (${item.telegram_group_id})`"
            :value="item.telegram_group_id"
          />
        </el-select>
        <el-button :loading="loading" @click="loadPolicies">读取</el-button>
        <el-button type="primary" :loading="loading" @click="saveAll">保存</el-button>
      </div>
    </div>

    <div class="policy-grid">
      <el-card shadow="never">
        <template #header>入群验证</template>
        <el-form label-width="150px">
          <el-form-item label="启用验证"><el-switch v-model="verificationForm.enable_verification" /></el-form-item>
          <el-form-item label="验证类型"><el-input v-model="verificationForm.verification_type" /></el-form-item>
          <el-form-item label="超时(分钟)"><el-input-number v-model="verificationForm.timeout_minutes" :min="1" :precision="0" /></el-form-item>
          <el-form-item label="最大尝试次数"><el-input-number v-model="verificationForm.max_attempts" :min="1" :precision="0" /></el-form-item>
          <el-form-item label="白名单跳过"><el-switch v-model="verificationForm.whitelist_bypass" /></el-form-item>
          <el-form-item label="自动踢未验证"><el-switch v-model="verificationForm.auto_kick_unverified" /></el-form-item>
          <el-form-item label="踢出延迟(分钟)"><el-input-number v-model="verificationForm.kick_after_minutes" :min="1" :precision="0" /></el-form-item>
          <el-form-item label="欢迎消息"><el-input v-model="verificationForm.welcome_message" type="textarea" :rows="3" /></el-form-item>
        </el-form>
      </el-card>

      <el-card shadow="never">
        <template #header>发言与反垃圾</template>
        <el-form label-width="150px">
          <el-form-item label="发言间隔(秒)"><el-input-number v-model="moderationForm.message_interval_seconds" :min="0" :precision="0" /></el-form-item>
          <el-form-item label="每分钟最大发言"><el-input-number v-model="moderationForm.max_messages_per_minute" :min="1" :precision="0" /></el-form-item>
          <el-form-item label="每小时最大链接"><el-input-number v-model="moderationForm.max_links_per_hour" :min="0" :precision="0" /></el-form-item>
          <el-form-item label="新人静默(分钟)"><el-input-number v-model="moderationForm.new_member_silent_minutes" :min="0" :precision="0" /></el-form-item>
          <el-form-item label="首次发言延迟"><el-input-number v-model="moderationForm.first_speak_delay_seconds" :min="0" :precision="0" /></el-form-item>
        </el-form>
      </el-card>

      <el-card shadow="never">
        <template #header>处罚策略</template>
        <el-form label-width="150px">
          <el-form-item label="警告阈值"><el-input-number v-model="punishmentForm.warn_threshold" :min="1" :precision="0" /></el-form-item>
          <el-form-item label="达阈值后禁言"><el-switch v-model="punishmentForm.mute_on_warn_threshold" /></el-form-item>
          <el-form-item label="禁言时长(秒)"><el-input-number v-model="punishmentForm.mute_duration_seconds" :min="0" :precision="0" /></el-form-item>
          <el-form-item label="封禁阈值"><el-input-number v-model="punishmentForm.ban_on_warn_threshold" :min="1" :precision="0" /></el-form-item>
          <el-form-item label="重复违规窗口"><el-input-number v-model="punishmentForm.repeat_violation_window_hours" :min="1" :precision="0" /></el-form-item>
          <el-form-item label="警告重置天数"><el-input-number v-model="punishmentForm.auto_reset_warning_days" :min="0" :precision="0" /></el-form-item>
          <el-form-item label="严重违规动作"><el-input v-model="punishmentForm.severe_violation_direct_action" /></el-form-item>
        </el-form>
      </el-card>
    </div>
  </div>
</template>

<style scoped lang="scss">
.page-shell { display: grid; gap: 16px; }
.page-header { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; }
.page-title { margin: 0; font-size: 20px; }
.page-desc { margin: 6px 0 0; color: #606266; }
.page-subtitle { margin: 6px 0 0; color: #909399; }
.header-actions { display: flex; gap: 12px; align-items: center; }
.policy-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 16px; }
@media (max-width: 1200px) { .policy-grid { grid-template-columns: 1fr; } }
</style>
