<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'

import type {
  AccountPersonaCategory,
  AccountPersonaPreviewInput,
  AccountPersonaPreviewResult,
  AccountPersonaPreviewTarget,
} from '@/api/accountPersonas'

const props = withDefaults(
  defineProps<{
    targets: AccountPersonaPreviewTarget[]
    result: AccountPersonaPreviewResult | null
    loading?: boolean
    disabled?: boolean
    runtimeWillApply?: boolean
  }>(),
  {
    loading: false,
    disabled: false,
    runtimeWillApply: true,
  },
)

const emit = defineEmits<{
  (event: 'preview', value: AccountPersonaPreviewInput): void
}>()

const selectedAssetId = ref<number | null>(null)
const contentCategory = ref<AccountPersonaCategory>('community')
const topic = ref('')
const includeSampleContext = ref(false)
const sampleUserName = ref('测试用户')
const sampleText = ref('')
const validationMessage = ref('')

const selectedTarget = computed(() =>
  props.targets.find((item) => item.owned_group_asset_id === selectedAssetId.value),
)

const availableCategories = computed(() => selectedTarget.value?.available_categories ?? [])

const resetForm = () => {
  selectedAssetId.value = props.targets[0]?.owned_group_asset_id ?? null
  contentCategory.value = props.targets[0]?.available_categories[0] ?? 'community'
  topic.value = ''
  includeSampleContext.value = false
  sampleUserName.value = '测试用户'
  sampleText.value = ''
  validationMessage.value = ''
}

watch(
  () => props.targets,
  () => resetForm(),
  { immediate: true },
)

watch(selectedAssetId, () => {
  const categories = availableCategories.value
  if (!categories.includes(contentCategory.value)) {
    contentCategory.value = categories[0] ?? 'community'
  }
  validationMessage.value = ''
})

const requestPreview = () => {
  if (props.loading || props.disabled) return
  const normalizedTopic = topic.value.normalize('NFKC').trim()
  if (!selectedTarget.value) {
    validationMessage.value = '当前账号没有可用于 Persona 预览的 AI 群策略'
    return
  }
  if (normalizedTopic.length < 1 || normalizedTopic.length > 100) {
    validationMessage.value = '预览主题需为 1–100 个字符'
    return
  }
  const normalizedSample = sampleText.value.normalize('NFKC').trim()
  if (includeSampleContext.value && !normalizedSample) {
    validationMessage.value = '启用示例上下文后，请填写示例消息'
    return
  }
  validationMessage.value = ''
  emit('preview', {
    owned_group_asset_id: selectedTarget.value.owned_group_asset_id,
    content_category: contentCategory.value,
    trigger_type: 'manual',
    topic: normalizedTopic,
    ...(includeSampleContext.value
      ? {
          sample_context: [
            {
              message_id: 1,
              user_name: sampleUserName.value.normalize('NFKC').trim() || '测试用户',
              text: normalizedSample,
            },
          ],
        }
      : {}),
  })
}

const copySample = async () => {
  if (!props.result?.sample_text) return
  try {
    if (!navigator.clipboard?.writeText) throw new Error('clipboard_unavailable')
    await navigator.clipboard.writeText(props.result.sample_text)
    ElMessage.success('样例已复制')
  } catch {
    ElMessage.error('复制失败，请手动选择样例文本')
  }
}

defineExpose({
  contentCategory,
  includeSampleContext,
  requestPreview,
  resetForm,
  sampleText,
  selectedAssetId,
  topic,
})
</script>

<template>
  <section class="preview-panel" data-testid="persona-preview-panel">
    <div class="section-heading">
      <div>
        <h3>生成预览</h3>
        <p>预览会消耗真实 LLM Token，但不会创建任务或发送 Telegram 消息。</p>
      </div>
      <el-button
        type="primary"
        :loading="loading"
        :disabled="disabled || loading || targets.length === 0"
        data-testid="persona-preview-button"
        @click="requestPreview"
      >
        生成示例
      </el-button>
    </div>

    <el-alert
      v-if="!runtimeWillApply"
      title="Persona 当前不会用于实际发送；可继续预览启用后的效果。"
      type="warning"
      :closable="false"
      show-icon
    />

    <el-empty
      v-if="targets.length === 0"
      description="没有绑定当前账号且启用 AI 模式的自建群策略，暂时无法预览"
      :image-size="64"
    />

    <el-form v-else label-position="top" :disabled="disabled" class="preview-form">
      <div class="preview-grid">
        <el-form-item label="预览自建群">
          <el-select v-model="selectedAssetId" placeholder="选择自建群">
            <el-option
              v-for="target in targets"
              :key="target.owned_group_asset_id"
              :label="target.group_name"
              :value="target.owned_group_asset_id"
            >
              <span>{{ target.group_name }}</span>
              <span v-if="!target.runtime_send_eligible" class="option-warning">仅预览、不可发送</span>
            </el-option>
          </el-select>
        </el-form-item>
        <el-form-item label="内容类别">
          <el-radio-group v-model="contentCategory">
            <el-radio-button
              v-if="availableCategories.includes('community')"
              value="community"
            >普通消息</el-radio-button>
            <el-radio-button
              v-if="availableCategories.includes('promotion')"
              value="promotion"
            >推广正文</el-radio-button>
          </el-radio-group>
        </el-form-item>
      </div>
      <el-alert
        v-if="selectedTarget && !selectedTarget.runtime_send_eligible"
        :title="`该目标仅可预览，实际发送会被阻断：${selectedTarget.blocking_reasons.join('、') || selectedTarget.governance_status}`"
        type="warning"
        :closable="false"
      />
      <el-form-item label="预览主题">
        <el-input v-model="topic" maxlength="100" show-word-limit placeholder="例如：节点稳定性" />
      </el-form-item>
      <el-form-item>
        <el-checkbox v-model="includeSampleContext">明确添加一条示例上下文</el-checkbox>
      </el-form-item>
      <div v-if="includeSampleContext" class="sample-context">
        <el-form-item label="示例用户">
          <el-input v-model="sampleUserName" maxlength="80" />
        </el-form-item>
        <el-form-item label="示例消息">
          <el-input
            v-model="sampleText"
            type="textarea"
            :rows="3"
            maxlength="500"
            show-word-limit
          />
        </el-form-item>
      </div>
      <el-alert
        v-if="validationMessage"
        :title="validationMessage"
        type="error"
        :closable="false"
      />
    </el-form>

    <div v-if="result" class="preview-result" data-testid="persona-preview-result">
      <div class="result-header">
        <div>
          <span>生成样例</span>
          <el-tag :type="result.governance.allowed ? 'success' : 'danger'" effect="plain">
            {{ result.governance.allowed ? '治理检查通过' : '治理检查阻断' }}
          </el-tag>
        </div>
        <el-button link type="primary" @click="copySample">只复制样例</el-button>
      </div>

      <template v-if="result.content_category === 'promotion' && result.promotion_composition">
        <div class="composition-card">
          <span>AI 推广正文</span>
          <p>{{ result.promotion_composition.body_text }}</p>
        </div>
        <div class="composition-card">
          <span>确定性 CTA</span>
          <p>{{ result.promotion_composition.cta_text || '未配置' }}</p>
        </div>
        <div class="composition-card">
          <span>只读 URL</span>
          <el-input :model-value="result.promotion_composition.destination_url" readonly />
        </div>
      </template>

      <div class="sample-output">{{ result.sample_text }}</div>
      <el-descriptions :column="2" border size="small">
        <el-descriptions-item label="Persona 来源">
          {{ result.persona_source }}
          <template v-if="result.persona_revision !== null"> · v{{ result.persona_revision }}</template>
        </el-descriptions-item>
        <el-descriptions-item label="运行时应用">
          {{ result.feature.runtime_will_apply ? '会应用' : '当前不会应用' }}
        </el-descriptions-item>
        <el-descriptions-item label="有效长度上限">
          {{ result.effective_constraints.max_chars }} 字
        </el-descriptions-item>
        <el-descriptions-item label="Token 用量">
          {{ result.usage.input_tokens }} / {{ result.usage.output_tokens }}
        </el-descriptions-item>
        <el-descriptions-item label="Provider / Model" :span="2">
          {{ result.usage.provider }} / {{ result.usage.model }}
        </el-descriptions-item>
      </el-descriptions>
      <div v-if="result.warnings.length" class="warning-list">
        <el-tag v-for="warning in result.warnings" :key="warning" type="warning" effect="plain">
          {{ warning }}
        </el-tag>
      </div>
    </div>
  </section>
</template>

<style scoped>
.preview-panel {
  display: flex;
  flex-direction: column;
  gap: 14px;
  padding: 16px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  background: var(--el-bg-color);
}

.section-heading,
.result-header,
.result-header > div {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.section-heading h3,
.section-heading p,
.composition-card p {
  margin: 0;
}

.section-heading h3 {
  font-size: 15px;
}

.section-heading p,
.composition-card span,
.option-warning {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.preview-grid,
.sample-context {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0 14px;
}

.option-warning {
  float: right;
  margin-left: 14px;
  color: var(--el-color-warning);
}

.preview-result {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding-top: 14px;
  border-top: 1px solid var(--el-border-color-lighter);
}

.composition-card,
.sample-output {
  padding: 12px;
  border-radius: 6px;
  background: var(--el-fill-color-light);
}

.sample-output {
  color: var(--el-text-color-primary);
  line-height: 1.7;
  white-space: pre-wrap;
}

.warning-list {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

@media (max-width: 720px) {
  .preview-grid,
  .sample-context {
    grid-template-columns: 1fr;
  }

  .section-heading {
    align-items: flex-start;
    flex-direction: column;
  }
}
</style>
