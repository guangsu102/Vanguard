<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElButton, ElDialog, ElForm, ElFormItem, ElInput, ElInputNumber, ElMessage, ElSelect, ElOption, ElTable, ElTableColumn, ElTag } from 'element-plus'
import { guardianApi, type GuardianBot, type ManagedGroupBinding } from '@/api/guardian'

const router = useRouter()
const loading = ref(false)
const dialogVisible = ref(false)
const groups = ref<ManagedGroupBinding[]>([])
const bots = ref<GuardianBot[]>([])

const form = reactive({
  telegram_group_id: 0,
  title: '',
  username: '',
  member_count: 0,
  bot_account_id: undefined as number | undefined,
  binding_status: 'active',
  bot_role: 'admin',
})

const loadData = async () => {
  loading.value = true
  try {
    const [groupsRes, botsRes] = await Promise.all([
      guardianApi.listManagedGroups({ limit: 100 }),
      guardianApi.listBots({ enabled: true, limit: 100 }),
    ])
    groups.value = groupsRes.data.data
    bots.value = botsRes.data.data
  } finally {
    loading.value = false
  }
}

const createBinding = async () => {
  if (!form.telegram_group_id || !form.bot_account_id) {
    ElMessage.warning('请填写群 ID 并选择主 Bot')
    return
  }
  await guardianApi.createManagedGroup({ ...form })
  ElMessage.success('Bot 管理群已绑定')
  dialogVisible.value = false
  Object.assign(form, {
    telegram_group_id: 0,
    title: '',
    username: '',
    member_count: 0,
    bot_account_id: undefined,
    binding_status: 'active',
    bot_role: 'admin',
  })
  await loadData()
}

const markDegraded = async (row: ManagedGroupBinding) => {
  await guardianApi.updateManagedGroup(row.id, { binding_status: row.binding_status === 'active' ? 'degraded' : 'active' })
  ElMessage.success('绑定状态已更新')
  await loadData()
}

const openPolicies = (row: ManagedGroupBinding) => {
  router.push({
    path: '/guardian/policies',
    query: {
      groupId: String(row.telegram_group_id),
      title: row.title || row.username || String(row.telegram_group_id),
      botId: String(row.bot_account_id),
    },
  })
}

const openSensitiveKeywords = (row: ManagedGroupBinding) => {
  router.push({
    path: '/guardian/keywords',
    query: {
      groupId: String(row.telegram_group_id),
      title: row.title || row.username || String(row.telegram_group_id),
    },
  })
}

const openGroupCampaigns = (row: ManagedGroupBinding) => {
  router.push({
    path: '/campaigns',
    query: {
      scope: 'managed_group',
      groupId: String(row.telegram_group_id),
      title: row.title || row.username || String(row.telegram_group_id),
      botId: String(row.bot_account_id),
    },
  })
}

onMounted(loadData)
</script>

<template>
  <div class="page-shell">
    <div class="page-header">
      <div>
        <h2 class="page-title">Bot管理群</h2>
        <p class="page-desc">仅展示已绑定主 Bot 的治理群，和增长侧群池分开管理。</p>
      </div>
      <div class="header-actions">
        <el-button @click="loadData">刷新</el-button>
        <el-button type="primary" @click="dialogVisible = true">绑定管理群</el-button>
      </div>
    </div>

    <el-table v-loading="loading" :data="groups" border>
      <el-table-column prop="telegram_group_id" label="Telegram群ID" min-width="150" />
      <el-table-column prop="title" label="群名称" min-width="180" />
      <el-table-column prop="username" label="群用户名" min-width="160" />
      <el-table-column prop="bot_identifier" label="主 Bot" min-width="180" />
      <el-table-column prop="bot_role" label="Bot 角色" width="110" />
      <el-table-column prop="binding_status" label="治理状态" width="120">
        <template #default="{ row }">
          <el-tag :type="row.binding_status === 'active' ? 'success' : 'warning'">{{ row.binding_status }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="操作" min-width="320">
        <template #default="{ row }">
          <el-button type="primary" link @click="openPolicies(row)">治理策略</el-button>
          <el-button type="success" link @click="openSensitiveKeywords(row)">群管敏感词</el-button>
          <el-button type="info" link @click="openGroupCampaigns(row)">群内活动</el-button>
          <el-button type="primary" link @click="markDegraded(row)">
            {{ row.binding_status === 'active' ? '标记降级' : '恢复治理' }}
          </el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="dialogVisible" title="绑定 Bot 管理群" width="560px">
      <el-form label-width="120px">
        <el-form-item label="Telegram群ID">
          <el-input-number v-model="form.telegram_group_id" :min="1" :precision="0" style="width: 100%" />
        </el-form-item>
        <el-form-item label="群名称">
          <el-input v-model="form.title" />
        </el-form-item>
        <el-form-item label="群用户名">
          <el-input v-model="form.username" placeholder="@group_name" />
        </el-form-item>
        <el-form-item label="成员数">
          <el-input-number v-model="form.member_count" :min="0" :precision="0" style="width: 100%" />
        </el-form-item>
        <el-form-item label="主 Bot">
          <el-select v-model="form.bot_account_id" filterable style="width: 100%">
            <el-option v-for="item in bots" :key="item.account_id" :label="item.display_name || item.identifier" :value="item.account_id" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" @click="createBinding">绑定</el-button>
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
