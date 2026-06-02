<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElButton, ElDialog, ElForm, ElFormItem, ElInput, ElMessage, ElSwitch, ElTable, ElTableColumn, ElTag } from 'element-plus'
import { guardianApi, type GuardianBot } from '@/api/guardian'

const loading = ref(false)
const dialogVisible = ref(false)
const bots = ref<GuardianBot[]>([])

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
    const res = await guardianApi.listBots({ limit: 100 })
    bots.value = res.data.data
  } finally {
    loading.value = false
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
</script>

<template>
  <div class="page-shell">
    <div class="page-header">
      <div>
        <h2 class="page-title">Bot账号</h2>
        <p class="page-desc">纯 Telegram Bot，用于管理群验证、处罚、广播和群内活动。</p>
      </div>
      <div class="header-actions">
        <el-button @click="loadBots">刷新</el-button>
        <el-button type="primary" @click="dialogVisible = true">新增 Bot</el-button>
      </div>
    </div>

    <el-table v-loading="loading" :data="bots" border>
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
          <el-switch :model-value="row.enabled" @change="toggleBot(row)" />
        </template>
      </el-table-column>
    </el-table>

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
