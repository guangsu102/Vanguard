<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { ElButton, ElDialog, ElForm, ElFormItem, ElInput, ElInputNumber, ElMessage, ElSelect, ElOption, ElSwitch, ElTable, ElTableColumn, ElTag } from 'element-plus'
import { guardianApi, type ManagedGroupBinding, type ModerationSensitiveKeyword } from '@/api/guardian'

const route = useRoute()
const loading = ref(false)
const dialogVisible = ref(false)
const keywords = ref<ModerationSensitiveKeyword[]>([])
const managedGroups = ref<ManagedGroupBinding[]>([])
const currentGroupId = ref<number | undefined>()

const form = reactive({
  text: '',
  category: 'sensitive',
  source: 'manual',
  level: 'medium',
  action: 'warn',
  group_id: undefined as number | undefined,
  enabled: true,
  confidence: 1,
  source_sample: '',
})

const currentGroupLabel = computed(() => {
  const matched = managedGroups.value.find((item) => item.telegram_group_id === currentGroupId.value)
  if (matched) {
    return matched.title || matched.username || String(matched.telegram_group_id)
  }
  const title = route.query.title
  return typeof title === 'string' && title ? title : ''
})

const applyRouteGroup = () => {
  const groupId = Number(route.query.groupId)
  if (Number.isFinite(groupId) && groupId > 0) {
    currentGroupId.value = groupId
    form.group_id = groupId
  } else {
    currentGroupId.value = undefined
    form.group_id = undefined
  }
}

const loadManagedGroups = async () => {
  const res = await guardianApi.listManagedGroups({ limit: 200 })
  managedGroups.value = res.data.data
}

const loadKeywords = async () => {
  loading.value = true
  try {
    const params: Record<string, any> = { page: 1, page_size: 100 }
    if (currentGroupId.value) {
      params.group_id = currentGroupId.value
    }
    const res = await guardianApi.listSensitiveKeywords(params)
    keywords.value = res.data.data
  } finally {
    loading.value = false
  }
}

const createKeyword = async () => {
  if (!form.text) {
    ElMessage.warning('请输入敏感词')
    return
  }
  await guardianApi.createSensitiveKeyword({ ...form, group_id: form.group_id || null, source_sample: form.source_sample || undefined })
  ElMessage.success('群管敏感词已创建')
  dialogVisible.value = false
  Object.assign(form, {
    text: '',
    category: 'sensitive',
    source: 'manual',
    level: 'medium',
    action: 'warn',
    group_id: undefined,
    enabled: true,
    confidence: 1,
    source_sample: '',
  })
  await loadKeywords()
}

const toggleKeyword = async (row: ModerationSensitiveKeyword) => {
  await guardianApi.updateSensitiveKeyword(row.id, { enabled: !row.enabled })
  ElMessage.success('状态已更新')
  await loadKeywords()
}

watch(
  () => route.query.groupId,
  async () => {
    applyRouteGroup()
    await loadKeywords()
  },
)

watch(currentGroupId, async (value) => {
  form.group_id = value
  await loadKeywords()
})

onMounted(async () => {
  await loadManagedGroups()
  applyRouteGroup()
  await loadKeywords()
})
</script>

<template>
  <div class="page-shell">
    <div class="page-header">
      <div>
        <h2 class="page-title">群管敏感词</h2>
        <p class="page-desc">只用于群治理违规检测，不参与搜群和营销触发。</p>
        <p v-if="currentGroupLabel" class="page-subtitle">当前管理群：{{ currentGroupLabel }}</p>
      </div>
      <div class="header-actions">
        <el-select v-model="currentGroupId" clearable filterable placeholder="全部管理群" style="width: 280px">
          <el-option
            v-for="item in managedGroups"
            :key="item.id"
            :label="`${item.title || item.username || item.telegram_group_id} (${item.telegram_group_id})`"
            :value="item.telegram_group_id"
          />
        </el-select>
        <el-button @click="loadKeywords">刷新</el-button>
        <el-button type="primary" @click="dialogVisible = true">新增敏感词</el-button>
      </div>
    </div>

    <el-table v-loading="loading" :data="keywords" border>
      <el-table-column prop="text" label="敏感词" min-width="180" />
      <el-table-column prop="category" label="分类" width="120" />
      <el-table-column prop="level" label="等级" width="100">
        <template #default="{ row }">
          <el-tag :type="row.level === 'high' ? 'danger' : row.level === 'medium' ? 'warning' : 'success'">{{ row.level }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="action" label="建议动作" width="120" />
      <el-table-column prop="group_id" label="群级覆盖" width="120">
        <template #default="{ row }">
          {{ row.group_id || '全局' }}
        </template>
      </el-table-column>
      <el-table-column label="启用" width="100">
        <template #default="{ row }">
          <el-switch :model-value="row.enabled" @change="toggleKeyword(row as ModerationSensitiveKeyword)" />
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="dialogVisible" title="新增群管敏感词" width="560px">
      <el-form label-width="120px">
        <el-form-item label="敏感词">
          <el-input v-model="form.text" />
        </el-form-item>
        <el-form-item label="分类">
          <el-input v-model="form.category" />
        </el-form-item>
        <el-form-item label="等级">
          <el-select v-model="form.level" style="width: 100%">
            <el-option label="low" value="low" />
            <el-option label="medium" value="medium" />
            <el-option label="high" value="high" />
          </el-select>
        </el-form-item>
        <el-form-item label="建议动作">
          <el-select v-model="form.action" style="width: 100%">
            <el-option label="warn" value="warn" />
            <el-option label="mute" value="mute" />
            <el-option label="kick" value="kick" />
            <el-option label="ban" value="ban" />
          </el-select>
        </el-form-item>
        <el-form-item label="群级覆盖">
          <el-input-number v-model="form.group_id" :min="1" :precision="0" style="width: 100%" />
        </el-form-item>
        <el-form-item label="置信度">
          <el-input-number v-model="form.confidence" :min="0" :max="1" :step="0.1" style="width: 100%" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" @click="createKeyword">创建</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped lang="scss">
.page-shell { display: grid; gap: 16px; }
.page-header { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; }
.page-title { margin: 0; font-size: 20px; }
.page-desc { margin: 6px 0 0; color: #606266; }
.page-subtitle { margin: 6px 0 0; color: #909399; }
.header-actions { display: flex; gap: 12px; }
</style>
