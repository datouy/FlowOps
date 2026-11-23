<template>
  <section v-loading="loading" class="post-composer">
    <div class="composer-grid">
      <main class="editor-column">
        <section class="editor-block">
          <label class="field-label" for="post-title">标题</label>
          <el-input
            id="post-title"
            v-model="draft.title"
            maxlength="120"
            show-word-limit
            placeholder="内容标题"
          />
          <label class="field-label field-label--spaced" for="post-body">正文</label>
          <el-input
            id="post-body"
            v-model="draft.body"
            type="textarea"
            :rows="7"
            maxlength="4000"
            show-word-limit
            placeholder="写下这条内容"
          />
          <label class="field-label field-label--spaced" for="post-tags">话题</label>
          <el-input id="post-tags" v-model="draft.tagsText" placeholder="旅行，周末出发，目的地" />
        </section>

        <section class="editor-block">
          <div class="block-heading">
            <span>素材</span>
            <div class="block-actions">
              <input ref="fileInput" class="hidden-file-input" type="file" multiple @change="uploadFiles" />
              <el-button size="small" @click="fileInput?.click()">上传</el-button>
              <el-select
                v-model="selectedMaterials"
                multiple
                collapse-tags
                filterable
                placeholder="素材库"
                class="material-select"
                @change="applyMaterials"
              >
                <el-option
                  v-for="material in materials"
                  :key="material.file_path || material.filename"
                  :label="material.filename"
                  :value="material.file_path || material.filename"
                />
              </el-select>
            </div>
          </div>
          <div v-if="draft.files.length" class="file-list">
            <span v-for="file in draft.files" :key="file" class="file-chip">
              <span>{{ fileName(file) }}</span>
              <button type="button" aria-label="移除素材" @click="removeFile(file)">×</button>
            </span>
          </div>
          <div v-else class="material-empty">选择素材库文件或上传</div>
        </section>

        <section class="editor-block">
          <div class="block-heading">
            <span>发布到</span>
            <span class="mono">{{ selectedAccounts.length }} 个账号</span>
          </div>
          <div class="account-groups">
            <div v-for="group in accountGroups" :key="group.key" class="account-group">
              <div class="platform-name">{{ group.label }}</div>
              <div class="account-options">
                <label
                  v-for="account in group.accounts"
                  :key="account.id"
                  class="account-option"
                  :class="{ selected: selectedAccountIds.includes(account.id), disabled: account.status !== '正常' }"
                >
                  <input
                    v-model="selectedAccountIds"
                    type="checkbox"
                    :value="account.id"
                    :disabled="account.status !== '正常'"
                    @change="ensureActivePlatform"
                  />
                  <span>{{ account.name }}</span>
                  <span v-if="account.status !== '正常'" class="account-state">异常</span>
                </label>
              </div>
            </div>
            <button v-if="!accountStore.accounts.length && userStore.can('account.manage')" type="button" class="add-account-link" @click="router.push('/settings?view=accounts')">
              添加账号
            </button>
          </div>
        </section>

        <section v-if="selectedPlatforms.length" class="editor-block">
          <div class="block-heading">
            <span>平台覆盖</span>
            <button type="button" class="quiet-action" @click="toggleAdvanced = !toggleAdvanced">
              {{ toggleAdvanced ? '收起' : '展开' }}
            </button>
          </div>
          <div class="platform-overrides">
            <button
              v-for="platform in selectedPlatforms"
              :key="platform.key"
              type="button"
              class="platform-choice"
              :class="{ active: activePlatform === platform.key, overridden: hasOverride(platform.key) }"
              @click="activePlatform = platform.key"
            >
              {{ platform.label }}
              <span>{{ hasOverride(platform.key) ? '已覆盖' : '主内容' }}</span>
            </button>
          </div>

          <div v-if="toggleAdvanced && activePlatform" class="override-editor">
            <div class="override-head">
              <span>{{ activePlatformLabel }}预览内容</span>
              <el-switch
                :model-value="hasOverride(activePlatform)"
                active-text="独立文案"
                @change="toggleOverride(activePlatform, $event)"
              />
            </div>
            <template v-if="hasOverride(activePlatform)">
              <el-input v-model="overrides[activePlatform].title" placeholder="平台标题" />
              <el-input v-model="overrides[activePlatform].body" type="textarea" :rows="4" placeholder="平台正文" />
              <el-input v-model="overrides[activePlatform].tagsText" placeholder="平台话题" />
            </template>
            <el-date-picker
              v-model="overrides[activePlatform].scheduledAt"
              type="datetime"
              value-format="YYYY-MM-DD HH:mm"
              placeholder="仅为此平台排期"
              clearable
            />
          </div>
        </section>

        <section class="editor-block editor-block--compact">
          <div class="publish-options">
            <span class="policy-note">审核要求由服务端按角色和团队策略确定</span>
            <el-switch v-model="draft.scheduleEnabled" active-text="定时发布" />
            <el-date-picker
              v-if="draft.scheduleEnabled"
              v-model="draft.scheduledAt"
              type="datetime"
              value-format="YYYY-MM-DD HH:mm"
              placeholder="发布时间"
            />
          </div>
        </section>
      </main>

      <aside class="preview-column">
        <div class="preview-label">
          <span>{{ activePlatformLabel }}</span>
          <span>{{ previewAccounts.map(item => item.name).join('、') || '尚未选择账号' }}</span>
        </div>
        <article class="platform-preview">
          <div class="preview-author">
            <el-avatar :size="30">{{ previewAuthor }}</el-avatar>
            <span>{{ previewAccounts[0]?.name || '账号' }}</span>
          </div>
          <div class="preview-body">
            <strong>{{ previewVariant.title || '内容标题' }}</strong>
            <p>{{ previewVariant.body || '正文会显示在这里。' }}</p>
            <div v-if="previewTags.length" class="preview-tags">
              <span v-for="tag in previewTags" :key="tag">#{{ tag }}</span>
            </div>
          </div>
          <div v-if="previewFile" class="preview-media">
            <img v-if="isImage(previewFile)" :src="materialUrl(previewFile)" :alt="fileName(previewFile)" />
            <div v-else class="video-placeholder">{{ fileName(previewFile) }}</div>
          </div>
        </article>
        <div class="lifecycle-note">
          <span class="lifecycle-dot" :class="{ active: reviewWillBeRequired || draft.scheduleEnabled }" />
          <span>{{ lifecycleText }}</span>
        </div>
      </aside>
    </div>

    <footer class="composer-actions">
      <span v-if="message" class="composer-message" :class="message.tone">{{ message.text }}</span>
      <span v-else />
      <div>
        <el-button :loading="saving" @click="saveDraft">保存草稿</el-button>
        <el-button type="primary" :loading="submitting" @click="submit">
          {{ reviewWillBeRequired ? '提交审核' : (draft.scheduleEnabled ? '安排发布' : '加入发布队列') }}
        </el-button>
      </div>
    </footer>
  </section>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { materialApi } from '@/api/material'
import { opsApi } from '@/api/ops'
import { useAccountStore } from '@/stores/account'
import { useUserStore } from '@/stores/user'

const route = useRoute()
const router = useRouter()
const accountStore = useAccountStore()
const userStore = useUserStore()
const platforms = [
  { key: 'douyin', type: 3, label: '抖音', accountLabel: '抖音' },
  { key: 'xiaohongshu', type: 1, label: '小红书', accountLabel: '小红书' },
  { key: 'channels', type: 2, label: '视频号', accountLabel: '视频号' },
  { key: 'kuaishou', type: 4, label: '快手', accountLabel: '快手' },
  { key: 'weibo', type: 5, label: '微博', accountLabel: '微博' },
  { key: 'baijiahao', type: 6, label: '百家号', accountLabel: '百家号' }
]

const fileInput = ref(null)
const materials = ref([])
const selectedMaterials = ref([])
const selectedAccountIds = ref([])
const activePlatform = ref('')
const toggleAdvanced = ref(false)
const loading = ref(false)
const saving = ref(false)
const submitting = ref(false)
const contentId = ref(Number(route.query.id || 0) || null)
const message = ref(null)
const overrides = reactive({})
const draft = reactive({
  title: '',
  body: '',
  tagsText: '',
  files: [],
  reviewRequired: userStore.session.reviewMode === 'all_required' || userStore.session.role === 'cutter',
  scheduleEnabled: false,
  scheduledAt: ''
})

const reviewWillBeRequired = computed(() => Boolean(draft.reviewRequired))

const accountGroups = computed(() => platforms.map(platform => ({
  ...platform,
  accounts: accountStore.accounts.filter(account => account.platform === platform.accountLabel)
})).filter(group => group.accounts.length))

const selectedAccounts = computed(() => accountStore.accounts.filter(account => selectedAccountIds.value.includes(account.id)))
const selectedPlatforms = computed(() => {
  const labels = new Set(selectedAccounts.value.map(account => account.platform))
  return platforms.filter(platform => labels.has(platform.accountLabel))
})
const activePlatformLabel = computed(() => platforms.find(item => item.key === activePlatform.value)?.label || '平台')
const previewAccounts = computed(() => selectedAccounts.value.filter(account => account.platform === activePlatformLabel.value))
const previewAuthor = computed(() => String(previewAccounts.value[0]?.name || '云').slice(0, 1))

function tagsFromText(value) {
  return String(value || '').split(/[,，\s]+/).map(item => item.trim().replace(/^#/, '')).filter(Boolean)
}

const previewVariant = computed(() => {
  if (!activePlatform.value || !hasOverride(activePlatform.value)) {
    return { title: draft.title, body: draft.body, tagsText: draft.tagsText }
  }
  return overrides[activePlatform.value]
})
const previewTags = computed(() => tagsFromText(previewVariant.value.tagsText))
const previewFile = computed(() => draft.files[0] || '')
const lifecycleText = computed(() => {
  if (draft.reviewRequired) return '草稿 · 待审核 · 审核通过后发布'
  if (draft.scheduleEnabled || selectedPlatforms.value.some(item => overrides[item.key]?.scheduledAt)) return '草稿 · 已排期 · 等待到点发布'
  return '草稿 · 加入队列后发布'
})

function fileName(path) {
  return String(path || '').replace(/\\/g, '/').split('/').pop()
}

function isImage(path) {
  return /\.(png|jpe?g|webp|gif)$/i.test(String(path || ''))
}

function materialUrl(path) {
  return materialApi.getMaterialPreviewUrl(fileName(path))
}

function hasOverride(key) {
  return Boolean(key && overrides[key])
}

function ensureActivePlatform() {
  if (!selectedPlatforms.value.some(item => item.key === activePlatform.value)) {
    activePlatform.value = selectedPlatforms.value[0]?.key || ''
  }
}

function ensureOverride(key) {
  if (!overrides[key]) {
    overrides[key] = reactive({
      title: draft.title,
      body: draft.body,
      tagsText: draft.tagsText,
      scheduledAt: ''
    })
  }
  return overrides[key]
}

function toggleOverride(key, enabled) {
  if (enabled) ensureOverride(key)
  else delete overrides[key]
}

async function load() {
  loading.value = true
  try {
    await Promise.allSettled([accountStore.loadAccounts(true), loadMaterials()])
    if (contentId.value) await loadPost(contentId.value)
  } finally {
    loading.value = false
  }
}

async function loadMaterials() {
  const response = await materialApi.getAllMaterials()
  materials.value = response.data || []
}

async function loadPost(id) {
  const response = await opsApi.post(id)
  const post = response.data || {}
  draft.title = post.title || ''
  draft.body = post.body || ''
  draft.files = post.files || []
  draft.reviewRequired = post.review_required !== false
  const variantList = post.variants || []
  if (variantList.length) {
    draft.tagsText = (variantList[0].tags || []).join('，')
    activePlatform.value = variantList[0].platform || ''
  }
  for (const variant of variantList) {
    const key = String(variant.platform || '')
    if (variant.title !== post.title || variant.body !== post.body || variant.scheduled_at) {
      ensureOverride(key)
      overrides[key].title = variant.title || post.title || ''
      overrides[key].body = variant.body || post.body || ''
      overrides[key].tagsText = (variant.tags || []).join('，')
      overrides[key].scheduledAt = variant.scheduled_at || ''
    }
    const account = accountStore.accounts.find(item => platformKey(item.platform) === key)
    if (account) selectedAccountIds.value.push(account.id)
  }
  ensureActivePlatform()
}

async function uploadFiles(event) {
  const files = Array.from(event.target.files || [])
  if (!files.length) return
  for (const file of files) {
    try {
      const form = new FormData()
      form.append('file', file)
      const response = await materialApi.uploadMaterial(form)
      const path = response.data?.filepath || response.data?.path
      if (path && !draft.files.includes(path)) draft.files.push(path)
    } catch (error) {
      ElMessage.error(error.message || '上传失败')
    }
  }
  event.target.value = ''
}

function applyMaterials(values) {
  for (const file of values) {
    if (!draft.files.includes(file)) draft.files.push(file)
  }
}

function removeFile(file) {
  draft.files = draft.files.filter(item => item !== file)
  selectedMaterials.value = selectedMaterials.value.filter(item => item !== file)
}

function platformKey(label) {
  return platforms.find(item => item.accountLabel === label)?.key || String(label || '').toLowerCase()
}

function buildVariantPayload() {
  const result = {}
  for (const platform of selectedPlatforms.value) {
    const override = overrides[platform.key]
    result[platform.key] = {
      title: override?.title || draft.title,
      body: override?.body || draft.body,
      tags: tagsFromText(override?.tagsText ?? draft.tagsText),
      scheduledAt: override?.scheduledAt || (draft.scheduleEnabled ? draft.scheduledAt : '')
    }
  }
  return result
}

function validate(forSubmit = false) {
  if (!draft.title.trim()) return '请填写标题'
  if (forSubmit && !draft.files.length) return '请选择素材'
  if (forSubmit && !selectedAccounts.value.length) return '请选择发布账号'
  if (forSubmit && draft.scheduleEnabled && !draft.scheduledAt) return '请选择发布时间'
  return ''
}

async function persist(forSubmit = false) {
  const error = validate(forSubmit)
  if (error) {
    ElMessage.warning(error)
    throw new Error(error)
  }
  const variants = buildVariantPayload()
  const response = await opsApi.postSave(contentId.value, {
    title: draft.title.trim(),
    body: draft.body,
    files: draft.files,
    variants
  })
  contentId.value = Number(response.data?.id || contentId.value)
  return variants
}

async function saveDraft() {
  saving.value = true
  message.value = null
  try {
    await persist(false)
    message.value = { text: '草稿已保存', tone: 'success' }
    ElMessage.success('草稿已保存')
  } catch (error) {
    if (error.message && !message.value) message.value = { text: error.message, tone: 'danger' }
  } finally {
    saving.value = false
  }
}

async function submit() {
  submitting.value = true
  message.value = null
  try {
    const variants = await persist(true)
    const accounts = {}
    for (const platform of selectedPlatforms.value) {
      accounts[platform.key] = selectedAccounts.value
        .filter(account => account.platform === platform.accountLabel)
        .map(account => account.filePath)
    }
    const response = await opsApi.postSubmit(contentId.value, {
      accounts,
      variants,
      scheduledAt: draft.scheduleEnabled ? draft.scheduledAt : ''
    })
    const count = response.data?.jobs?.length || 0
    const requiresReview = Boolean(response.data?.reviewRequired)
    draft.reviewRequired = requiresReview
    message.value = { text: `已创建 ${count} 个发布任务`, tone: 'success' }
    ElMessage.success(requiresReview ? '已提交审核' : '已加入发布队列')
    router.push('/insights?view=works')
  } catch (error) {
    message.value = { text: error.message || '提交失败', tone: 'danger' }
    ElMessage.error(error.message || '提交失败')
  } finally {
    submitting.value = false
  }
}

onMounted(load)
</script>

<style scoped lang="scss">
@use '@/styles/variables.scss' as *;
.post-composer { position: relative; }
.composer-grid { display: grid; grid-template-columns: minmax(0, 1fr) 340px; align-items: start; gap: 24px; padding-bottom: 76px; }
.editor-column { min-width: 0; display: flex; flex-direction: column; gap: 16px; }
.editor-block { padding: 20px; border: 1px solid $border-lighter; border-radius: 14px; background: $paper; }
.editor-block--compact { padding: 14px 20px; }
.field-label { display: block; margin-bottom: 8px; color: $text-secondary; font-size: 12px; }
.field-label--spaced { margin-top: 18px; }
.block-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; min-height: 32px; margin-bottom: 12px; }
.block-heading > span:first-child { color: $text-primary; font-size: 14px; font-weight: 500; }
.block-heading .mono { color: $text-placeholder; font-size: 12px; }
.block-actions { display: flex; align-items: center; gap: 8px; }
.material-select { width: 188px; }
.hidden-file-input { display: none; }
.file-list { display: flex; flex-wrap: wrap; gap: 6px; }
.file-chip { display: inline-flex; align-items: center; gap: 8px; max-width: 100%; padding: 5px 8px; color: $text-regular; border: 1px solid $border-lighter; border-radius: 6px; background: $bg-color-raised; font-size: 12px; }
.file-chip span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.file-chip button { color: $text-placeholder; border: 0; background: transparent; cursor: pointer; }
.material-empty { padding: 14px; color: $text-placeholder; border: 1px dashed $border-light; border-radius: 8px; font-size: 12px; text-align: center; }
.account-groups { display: flex; flex-direction: column; gap: 12px; }
.account-group { display: grid; grid-template-columns: 68px minmax(0, 1fr); align-items: start; gap: 10px; }
.platform-name { padding-top: 7px; color: $text-secondary; font-size: 12px; }
.account-options { display: flex; flex-wrap: wrap; gap: 6px; }
.account-option { display: inline-flex; align-items: center; gap: 7px; min-height: 32px; padding: 0 10px; border: 1px solid $border-lighter; border-radius: 7px; color: $text-regular; font-size: 12px; cursor: pointer; }
.account-option.selected { border-color: $ink-300; background: $bg-color-raised; }
.account-option.disabled { color: $text-placeholder; cursor: default; }
.account-option input { accent-color: $ink-850; }
.account-state { color: $danger-color; }
.add-account-link { align-self: flex-start; padding: 0; color: $primary-color; border: 0; background: transparent; cursor: pointer; }
.platform-overrides { display: flex; flex-wrap: wrap; gap: 6px; }
.platform-choice { display: inline-flex; align-items: center; gap: 8px; min-height: 34px; padding: 0 10px; color: $text-secondary; border: 1px solid $border-lighter; border-radius: 7px; background: transparent; cursor: pointer; }
.platform-choice span { color: $text-placeholder; font-size: 11px; }
.platform-choice.active { color: $text-primary; border-color: $ink-300; background: $bg-color-raised; }
.platform-choice.overridden span { color: $primary-color; }
.override-editor { display: flex; flex-direction: column; gap: 10px; margin-top: 14px; padding-top: 14px; border-top: 1px solid $border-lighter; }
.override-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; color: $text-secondary; font-size: 12px; }
.publish-options { display: flex; align-items: center; gap: 24px; flex-wrap: wrap; }
.policy-note { color: $text-secondary; font-size: 12px; }
.preview-column { position: sticky; top: 0; }
.preview-label { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-bottom: 8px; color: $text-secondary; font-size: 12px; }
.preview-label span:last-child { overflow: hidden; color: $text-placeholder; text-overflow: ellipsis; white-space: nowrap; }
.platform-preview { overflow: hidden; padding: 16px; border: 1px solid $border-lighter; border-radius: 16px; background: $paper; }
.preview-author { display: flex; align-items: center; gap: 8px; margin-bottom: 14px; color: $text-secondary; font-size: 12px; }
.preview-body { display: flex; flex-direction: column; gap: 10px; }
.preview-body strong { color: $text-primary; font-size: 15px; font-weight: 500; line-height: 1.45; }
.preview-body p { color: $text-regular; font-size: 13px; line-height: 1.7; white-space: pre-wrap; }
.preview-tags { display: flex; flex-wrap: wrap; gap: 6px; color: $primary-color-dark; font-size: 12px; }
.preview-media { margin-top: 14px; overflow: hidden; border-radius: 10px; background: $ink-100; }
.preview-media img { display: block; width: 100%; max-height: 260px; object-fit: cover; }
.video-placeholder { display: grid; place-items: center; min-height: 150px; padding: 16px; color: $text-secondary; font-size: 12px; text-align: center; }
.lifecycle-note { display: flex; align-items: center; gap: 8px; margin-top: 12px; color: $text-placeholder; font-size: 11px; }
.lifecycle-dot { width: 6px; height: 6px; border-radius: 50%; background: $ink-300; }
.lifecycle-dot.active { background: $primary-color; }
.composer-actions { position: sticky; z-index: 5; bottom: 0; display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-top: -60px; padding: 14px 0 18px; background: linear-gradient(to bottom, rgba(244, 243, 239, 0), $bg-color-page 18px); }
.composer-actions > div { display: flex; gap: 8px; }
.composer-message { font-size: 12px; }
.composer-message.success { color: $success-color; }
.composer-message.danger { color: $danger-color; }
@media (max-width: 1080px) {
  .composer-grid { grid-template-columns: 1fr; }
  .preview-column { position: static; }
}
@media (max-width: 720px) {
  .editor-block { padding: 16px; }
  .block-heading { align-items: flex-start; flex-direction: column; }
  .block-actions { width: 100%; }
  .material-select { flex: 1; width: auto; }
  .account-group { grid-template-columns: 1fr; }
  .composer-actions { align-items: stretch; flex-direction: column; }
  .composer-actions > div { justify-content: flex-end; }
}
</style>
