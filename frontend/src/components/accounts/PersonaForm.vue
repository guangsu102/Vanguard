<script setup lang="ts">
import { reactive } from 'vue'

import type {
  AccountPersona,
  AccountPersonaAdStyle,
  AccountPersonaFieldErrors,
  AccountPersonaLanguageStyle,
} from '@/api/accountPersonas'

type PersonaListField =
  | 'interests'
  | 'expertise'
  | 'preferred_topics'
  | 'forbidden_topics'
  | 'catchphrases'

const props = withDefaults(
  defineProps<{
    modelValue: AccountPersona
    disabled?: boolean
    fieldErrors?: AccountPersonaFieldErrors
    maxSystemPromptChars?: number
  }>(),
  {
    disabled: false,
    fieldErrors: () => ({}),
    maxSystemPromptChars: 1000,
  },
)

const emit = defineEmits<{
  (event: 'update:modelValue', value: AccountPersona): void
}>()

const tagDrafts = reactive<Record<PersonaListField, string>>({
  interests: '',
  expertise: '',
  preferred_topics: '',
  forbidden_topics: '',
  catchphrases: '',
})

const listLimits: Readonly<Record<PersonaListField, { maxItems: number; maxChars: number }>> = {
  interests: { maxItems: 10, maxChars: 40 },
  expertise: { maxItems: 10, maxChars: 40 },
  preferred_topics: { maxItems: 12, maxChars: 60 },
  forbidden_topics: { maxItems: 20, maxChars: 60 },
  catchphrases: { maxItems: 8, maxChars: 60 },
}

const update = <K extends keyof AccountPersona>(field: K, value: AccountPersona[K]) => {
  emit('update:modelValue', { ...props.modelValue, [field]: value })
}

const updateString = (field: 'name' | 'tone' | 'system_prompt', value: string) => {
  update(field, value)
}

const updateReplyLength = (value: string | number | boolean | undefined) => {
  if (value === 'short' || value === 'medium' || value === 'long') {
    update('reply_length', value)
  }
}

const updateAdStyle = (value: AccountPersonaAdStyle) => {
  update('ad_style', value)
}

const updateLanguageStyle = (value: AccountPersonaLanguageStyle) => {
  update('language_style', value)
}

const addTag = (field: PersonaListField) => {
  const limits = listLimits[field]
  const normalized = tagDrafts[field].normalize('NFKC').trim()
  const current = props.modelValue[field]
  if (!normalized || normalized.length > limits.maxChars || current.length >= limits.maxItems) return
  if (current.some((item) => item.toLocaleLowerCase() === normalized.toLocaleLowerCase())) {
    tagDrafts[field] = ''
    return
  }
  update(field, [...current, normalized])
  tagDrafts[field] = ''
}

const removeTag = (field: PersonaListField, index: number) => {
  update(
    field,
    props.modelValue[field].filter((_, itemIndex) => itemIndex !== index),
  )
}

defineExpose({ addTag, removeTag, tagDrafts })
</script>

<template>
  <div class="persona-form" data-testid="persona-form">
    <section class="persona-section">
      <div class="section-heading">
        <h3>基础性格</h3>
        <span>定义账号在群内的稳定表达风格</span>
      </div>
      <el-form label-position="top" :disabled="disabled">
        <div class="form-grid">
          <el-form-item label="Persona 名称" :error="fieldErrors.name">
            <el-input
              :model-value="modelValue.name"
              maxlength="50"
              show-word-limit
              placeholder="例如：技术型群友"
              @update:model-value="updateString('name', $event)"
            />
          </el-form-item>
          <el-form-item label="语言风格" :error="fieldErrors.language_style">
            <el-select
              :model-value="modelValue.language_style"
              @update:model-value="updateLanguageStyle"
            >
              <el-option label="自动（仍输出简体中文）" value="auto" />
              <el-option label="简体中文" value="zh_cn" />
            </el-select>
          </el-form-item>
          <el-form-item class="wide" label="语气" :error="fieldErrors.tone">
            <el-input
              :model-value="modelValue.tone"
              maxlength="120"
              show-word-limit
              placeholder="自然、克制、简短"
              @update:model-value="updateString('tone', $event)"
            />
          </el-form-item>
          <el-form-item label="回复长度" :error="fieldErrors.reply_length">
            <el-radio-group
              :model-value="modelValue.reply_length"
              @update:model-value="updateReplyLength"
            >
              <el-radio-button value="short">简短</el-radio-button>
              <el-radio-button value="medium">适中</el-radio-button>
              <el-radio-button value="long">较长</el-radio-button>
            </el-radio-group>
          </el-form-item>
        </div>
      </el-form>
    </section>

    <section class="persona-section">
      <div class="section-heading">
        <h3>兴趣与专长</h3>
        <span>输入后按回车添加；重复项会自动忽略</span>
      </div>
      <el-form label-position="top" :disabled="disabled">
        <el-form-item :error="fieldErrors.interests">
          <template #label>
            <span>兴趣 {{ modelValue.interests.length }}/{{ listLimits.interests.maxItems }}</span>
          </template>
          <div class="tag-editor">
            <div class="tag-list">
              <el-tag
                v-for="(item, index) in modelValue.interests"
                :key="`${item}-${index}`"
                :closable="!disabled"
                @close="removeTag('interests', index)"
              >{{ item }}</el-tag>
            </div>
            <el-input
              v-model="tagDrafts.interests"
              :maxlength="listLimits.interests.maxChars"
              :disabled="disabled || modelValue.interests.length >= listLimits.interests.maxItems"
              placeholder="输入兴趣并回车"
              @keyup.enter="addTag('interests')"
            />
          </div>
        </el-form-item>

        <el-form-item :error="fieldErrors.expertise">
          <template #label>
            <span>专长 {{ modelValue.expertise.length }}/{{ listLimits.expertise.maxItems }}</span>
          </template>
          <div class="tag-editor">
            <div class="tag-list">
              <el-tag
                v-for="(item, index) in modelValue.expertise"
                :key="`${item}-${index}`"
                :closable="!disabled"
                type="success"
                @close="removeTag('expertise', index)"
              >{{ item }}</el-tag>
            </div>
            <el-input
              v-model="tagDrafts.expertise"
              :maxlength="listLimits.expertise.maxChars"
              :disabled="disabled || modelValue.expertise.length >= listLimits.expertise.maxItems"
              placeholder="输入专长并回车"
              @keyup.enter="addTag('expertise')"
            />
          </div>
        </el-form-item>

        <el-form-item :error="fieldErrors.preferred_topics">
          <template #label>
            <span>
              偏好话题 {{ modelValue.preferred_topics.length }}/{{ listLimits.preferred_topics.maxItems }}
            </span>
          </template>
          <div class="tag-editor">
            <div class="tag-list">
              <el-tag
                v-for="(item, index) in modelValue.preferred_topics"
                :key="`${item}-${index}`"
                :closable="!disabled"
                type="success"
                @close="removeTag('preferred_topics', index)"
              >{{ item }}</el-tag>
            </div>
            <el-input
              v-model="tagDrafts.preferred_topics"
              :maxlength="listLimits.preferred_topics.maxChars"
              :disabled="disabled || modelValue.preferred_topics.length >= listLimits.preferred_topics.maxItems"
              placeholder="输入偏好话题并回车"
              @keyup.enter="addTag('preferred_topics')"
            />
          </div>
        </el-form-item>

        <el-form-item :error="fieldErrors.forbidden_topics">
          <template #label>
            <span>
              附加禁区 {{ modelValue.forbidden_topics.length }}/{{ listLimits.forbidden_topics.maxItems }}
            </span>
          </template>
          <div class="tag-editor">
            <div class="tag-list">
              <el-tag
                v-for="(item, index) in modelValue.forbidden_topics"
                :key="`${item}-${index}`"
                :closable="!disabled"
                type="danger"
                @close="removeTag('forbidden_topics', index)"
              >{{ item }}</el-tag>
            </div>
            <el-input
              v-model="tagDrafts.forbidden_topics"
              :maxlength="listLimits.forbidden_topics.maxChars"
              :disabled="disabled || modelValue.forbidden_topics.length >= listLimits.forbidden_topics.maxItems"
              placeholder="输入禁区并回车"
              @keyup.enter="addTag('forbidden_topics')"
            />
          </div>
        </el-form-item>
      </el-form>
    </section>

    <section class="persona-section">
      <div class="section-heading">
        <h3>推广表达</h3>
        <span>只影响 AI 推广正文，不会修改 CTA 或链接</span>
      </div>
      <el-form label-position="top" :disabled="disabled">
        <el-form-item label="推广风格" :error="fieldErrors.ad_style">
          <el-select :model-value="modelValue.ad_style" @update:model-value="updateAdStyle">
            <el-option label="中性表达" value="neutral" />
            <el-option label="柔和分享" value="soft_share" />
            <el-option label="体验分享" value="experience_share" />
            <el-option label="问题与方案" value="problem_solution" />
          </el-select>
        </el-form-item>
        <el-form-item :error="fieldErrors.catchphrases">
          <template #label>
            <span>口头禅 {{ modelValue.catchphrases.length }}/{{ listLimits.catchphrases.maxItems }}</span>
          </template>
          <div class="tag-editor">
            <div class="tag-list">
              <el-tag
                v-for="(item, index) in modelValue.catchphrases"
                :key="`${item}-${index}`"
                :closable="!disabled"
                type="warning"
                @close="removeTag('catchphrases', index)"
              >{{ item }}</el-tag>
            </div>
            <el-input
              v-model="tagDrafts.catchphrases"
              :maxlength="listLimits.catchphrases.maxChars"
              :disabled="disabled || modelValue.catchphrases.length >= listLimits.catchphrases.maxItems"
              placeholder="输入口头禅并回车"
              @keyup.enter="addTag('catchphrases')"
            />
          </div>
        </el-form-item>
      </el-form>
    </section>

    <section class="persona-section">
      <div class="section-heading">
        <h3>高级风格说明</h3>
        <span>低优先级风格备注，不会覆盖安全规则</span>
      </div>
      <el-form label-position="top" :disabled="disabled">
        <el-form-item :error="fieldErrors.system_prompt">
          <el-input
            :model-value="modelValue.system_prompt"
            type="textarea"
            :rows="5"
            :maxlength="maxSystemPromptChars"
            show-word-limit
            placeholder="可选。请只描述风格，不要填写密钥、链接或安全绕过要求。"
            @update:model-value="updateString('system_prompt', $event)"
          />
        </el-form-item>
        <el-alert v-if="fieldErrors._form" :title="fieldErrors._form" type="error" :closable="false" />
      </el-form>
    </section>
  </div>
</template>

<style scoped>
.persona-form {
  display: flex;
  flex-direction: column;
  gap: 18px;
}

.persona-section {
  padding: 16px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  background: var(--el-bg-color);
}

.section-heading {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 14px;
}

.section-heading h3 {
  margin: 0;
  color: var(--el-text-color-primary);
  font-size: 15px;
}

.section-heading span {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.form-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0 16px;
}

.wide {
  grid-column: 1 / -1;
}

.tag-editor,
.tag-list {
  display: flex;
  width: 100%;
  flex-wrap: wrap;
  gap: 8px;
}

.tag-editor {
  flex-direction: column;
}

.tag-list:empty {
  display: none;
}

@media (max-width: 720px) {
  .form-grid {
    grid-template-columns: 1fr;
  }

  .section-heading {
    align-items: flex-start;
    flex-direction: column;
  }
}
</style>
