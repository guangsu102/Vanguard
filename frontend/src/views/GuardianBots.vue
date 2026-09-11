<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { ElButton, ElDialog, ElForm, ElFormItem, ElInput, ElMessage, ElSwitch, ElTable, ElTableColumn, ElTag } from 'element-plus'
import { guardianApi, type GuardianBot } from '@/api/guardian'
import ClientListPagination from '@/components/ClientListPagination.vue'
import { useClientPagination } from '@/utils/clientPagination'
import { useRoute, useRouter } from 'vue-router'
import { parseSafePositiveId } from '@/utils/groupOpsAccess'

const route = useRoute()
const router = useRouter()

const loading = ref(false)
const dialogVisible = ref(false)
const bots = ref<GuardianBot[]>([])
const botPagination = useClientPagination(bots)
const focusedGuardianBotId = ref<number | null>(null)
const focusedGuardianBot = ref<GuardianBot | null>(null)
const focusedGuardianBotError = ref('')
const returnAssetId = computed(() => parseSafePositiveId(route.query.assetId))
let locationSequence = 0
let locationController: AbortController | null = null
const guardianRowClassName = ({ row }: { row: GuardianBot }) => row.id === focusedGuardianBotId.value ? 'focused-guardian-bot' : ''

const form = reactive({
  identifier: '',
  display_name: '',
  bot_token: '',
  bot_username: '',
  enabled: true,
})

const loadBots = async () => {
  loading.value = true
  try {
    const res = await guardianApi.listBots({ limit: 200 })
    bots.value = res.data.data
    await locateFromRoute()
  } finally {
    loading.value = false
  }
}

const clearLocation = () => {
  locationSequence += 1
  locationController?.abort()
  locationController = null
  focusedGuardianBotId.value = null
  focusedGuardianBot.value = null
  focusedGuardianBotError.value = ''
}

const locateFromRoute = async () => {
  clearLocation()
  const profileId = parseSafePositiveId(route.query.profileId)
  if (!profileId) return
  const sequence = locationSequence
  const index = bots.value.findIndex((item) => item.id === profileId)
  if (index >= 0) {
    focusedGuardianBotId.value = profileId
    botPagination.page.value = Math.floor(index / botPagination.pageSize.value) + 1
    await nextTick()
    document.querySelector('.focused-guardian-bot')?.scrollIntoView({ block: 'center', behavior: 'smooth' })
    return
  }
  const controller = new AbortController()
  locationController = controller
  try {
    const result = await guardianApi.getBot(profileId, controller.signal)
    if (sequence !== locationSequence || parseSafePositiveId(route.query.profileId) !== profileId) return
    focusedGuardianBot.value = result
  } catch (error) {
    const code = (error as { code?: string })?.code
    if (code !== 'ERR_CANCELED' && sequence === locationSequence) focusedGuardianBotError.value = 'Bot 配置不存在或无权限'
  } finally {
    if (sequence === locationSequence) locationController = null
  }
}

const createBot = async () => {
  if (!form.identifier || !form.bot_token) {
    ElMessage.warning('请填写 Bot 标识和 Token')
    return
  }
  await guardianApi.createBot({ ...form })
  ElMessage.success('Bot 账号已创建')
  dialogVisible.value = false
  Object.assign(form, {
    identifier: '',
    display_name: '',
    bot_token: '',
    bot_username: '',
    enabled: true,
  })
  await loadBots()
}

const toggleBot = async (row: GuardianBot) => {
  await guardianApi.updateBot(row.id, { enabled: !row.enabled, is_active: !row.is_active })
  ElMessage.success('状态已更新')
  await loadBots()
}

onMounted(loadBots)
watch(() => [route.query.profileId, route.query.assetId], () => { void locateFromRoute() })
onBeforeUnmount(clearLocation)
</script>

<template>
  <div class="page-shell">
    <div class="page-header">
      <div>
        <h2 class="page-title">Bot账号</h2>
        <p class="page-desc">纯 Telegram Bot，用于管理群验证、处罚、广播和群内活动。</p>
      </div>
      <div class="header-actions">
        <el-button v-if="returnAssetId" @click="router.push(`/owned-groups/${returnAssetId}/operations?tab=members`)">返回群运营中心</el-button>
        <el-button @click="loadBots">刷新</el-button>
        <el-button type="primary" @click="dialogVisible = true">新增 Bot</el-button>
      </div>
    </div>

    <el-alert v-if="focusedGuardianBotError" type="warning" :closable="false" show-icon :title="focusedGuardianBotError" />
    <el-card v-if="focusedGuardianBot" shadow="never">
      <template #header><strong>定位结果（不影响当前分页）</strong></template>
      <el-descriptions :column="4" border><el-descriptions-item label="Profile ID">#{{ focusedGuardianBot.id }}</el-descriptions-item><el-descriptions-item label="名称">{{ focusedGuardianBot.display_name || `Bot #${focusedGuardianBot.id}` }}</el-descriptions-item><el-descriptions-item label="健康">{{ focusedGuardianBot.health_status }}</el-descriptions-item><el-descriptions-item label="同步">{{ focusedGuardianBot.sync_status }}</el-descriptions-item></el-descriptions>
    </el-card>

    <el-table v-loading="loading" :data="botPagination.rows.value" :row-class-name="guardianRowClassName" border>
      <el-table-column prop="identifier" label="标识" min-width="180" />
      <el-table-column prop="display_name" label="名称" min-width="160" />
      <el-table-column prop="bot_username" label="Bot 用户名" min-width="160" />
      <el-table-column prop="status" label="账号状态" width="120">
        <template #default="{ row }">
          <el-tag>{{ row.status }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="health_status" label="健康状态" width="120">
        <template #default="{ row }">
          <el-tag :type="row.health_status === 'healthy' ? 'success' : 'info'">{{ row.health_status }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="sync_status" label="同步状态" width="120" />
      <el-table-column label="启用" width="100">
        <template #default="{ row }">
          <el-switch :model-value="row.enabled" @change="toggleBot(row as GuardianBot)" />
        </template>
      </el-table-column>
    </el-table>
    <ClientListPagination
      v-model:page="botPagination.page.value"
      v-model:page-size="botPagination.pageSize.value"
      :total="botPagination.total.value"
    />

    <el-dialog v-model="dialogVisible" title="新增 Bot 账号" width="520px">
      <el-form label-width="110px">
        <el-form-item label="标识">
          <el-input v-model="form.identifier" placeholder="@guardian_bot_a" />
        </el-form-item>
        <el-form-item label="展示名称">
          <el-input v-model="form.display_name" placeholder="群管 Bot A" />
        </el-form-item>
        <el-form-item label="Bot Token">
          <el-input v-model="form.bot_token" type="password" show-password />
        </el-form-item>
        <el-form-item label="Bot 用户名">
          <el-input v-model="form.bot_username" placeholder="@guardian_bot_a" />
        </el-form-item>
        <el-form-item label="启用">
          <el-switch v-model="form.enabled" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" @click="createBot">创建</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped lang="scss">
.page-shell { display: grid; gap: 16px; }
.page-header { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; }
.page-title { margin: 0; font-size: 20px; }
.page-desc { margin: 6px 0 0; color: #606266; }
.header-actions { display: flex; gap: 12px; }
</style>
