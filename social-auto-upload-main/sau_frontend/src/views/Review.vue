<template>
  <section class="review-workspace">
    <div class="review-toolbar">
      <div class="review-filters">
        <button v-for="item in filters" :key="item.value" type="button" :class="{ active: status === item.value }" @click="changeStatus(item.value)">{{ item.label }}</button>
      </div>
      <el-button :loading="loading" @click="load">刷新</el-button>
    </div>

    <div class="review-layout" v-loading="loading">
      <aside class="review-list">
        <button v-for="job in jobs" :key="job.id" type="button" class="review-row" :class="{ active: selected?.id === job.id }" @click="selectJob(job)">
          <span class="review-row-top"><strong>{{ job.title || '未命名内容' }}</strong><span class="mono">#{{ job.id }}</span></span>
          <span class="review-row-meta"><span>{{ platformName(job.type) }}</span><span>{{ shortDate(job.created_at) }}</span></span>
        </button>
        <div v-if="!jobs.length && !loading" class="review-empty"><span>没有需要处理的内容</span></div>
      </aside>

      <article v-if="selected" class="review-detail">
        <header class="detail-head">
          <div><span class="detail-platform">{{ platformName(selected.type) }}</span><h2>{{ selected.title || '未命名内容' }}</h2></div>
          <span class="status-text">{{ statusText(selected.status) }}</span>
        </header>
        <div class="detail-grid">
          <section>
            <h3>发布内容</h3>
            <p class="detail-body">{{ selectedExtra.desc || post?.body || '没有正文' }}</p>
            <div v-if="selectedTags.length" class="tag-list"><span v-for="tag in selectedTags" :key="tag">#{{ tag }}</span></div>
            <div class="file-list"><span v-for="file in selectedFiles" :key="file">{{ fileName(file) }}</span></div>
          </section>
          <section v-if="variants.length">
            <h3>平台版本</h3>
            <div class="variant-list">
              <div v-for="variant in variants" :key="variant.platform" class="variant-row">
                <strong>{{ platformLabel(variant.platform) }}</strong>
                <span>{{ variant.title || post?.title }}</span>
                <p>{{ variant.body || post?.body }}</p>
              </div>
            </div>
          </section>
        </div>
        <section v-if="status === 'pending_review' && userStore.can('review.decide')" class="review-action">
          <label for="review-note">审核意见</label>
          <el-input id="review-note" v-model="reviewNote" type="textarea" :rows="3" maxlength="1000" placeholder="写清需要调整的地方" />
          <div class="review-buttons">
            <el-button type="danger" :loading="submitting" @click="reject">驳回</el-button>
            <el-button type="primary" :loading="submitting" @click="approve">通过</el-button>
          </div>
        </section>
        <section v-else-if="selected.review_note || selected.reviewed_at || selected.reviewed_by" class="review-history">
          <h3>审核意见</h3><p>{{ selected.review_note || '未填写审核意见' }}</p><span>审核人：{{ selected.reviewed_by || '历史未记录' }} · {{ selected.reviewed_at || '' }}</span>
        </section>
      </article>
      <el-empty v-else description="选择一条内容" :image-size="80" />
    </div>
  </section>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { http } from '@/utils/request'
import { opsApi } from '@/api/ops'
import { useUserStore } from '@/stores/user'

defineProps({ embedded: Boolean })
const userStore = useUserStore()
const filters = [
  { value: 'pending_review', label: '待审核' },
  { value: 'pending', label: '已通过' },
  { value: 'rejected', label: '已驳回' }
]
const status = ref('pending_review')
const jobs = ref([])
const selected = ref(null)
const post = ref(null)
const loading = ref(false)
const submitting = ref(false)
const reviewNote = ref('')
const names = { 1: '小红书', 2: '视频号', 3: '抖音', 4: '快手', 5: '微博', 6: '百家号' }

const selectedExtra = computed(() => parseJson(selected.value?.extra, {}))
const selectedFiles = computed(() => parseJson(selected.value?.file_list, []))
const selectedTags = computed(() => parseJson(selected.value?.tags, []))
const variants = computed(() => post.value?.variants || [])

function parseJson(value, fallback) {
  if (value && typeof value === 'object') return value
  try { return JSON.parse(value || '') } catch { return fallback }
}
function platformName(type) { return names[Number(type)] || '平台' }
function platformLabel(value) { return ({ douyin: '抖音', xiaohongshu: '小红书', channels: '视频号', tencent: '视频号', kuaishou: '快手', weibo: '微博', baijiahao: '百家号' })[String(value || '').toLowerCase()] || value }
function statusText(value) { return ({ pending_review: '待审核', pending: '已通过', rejected: '已驳回', running: '发布中', success: '已完成', failed: '失败' })[value] || value }
function shortDate(value) { return String(value || '').replace('T', ' ').slice(5, 16) }
function fileName(path) { return String(path || '').replace(/\\/g, '/').split('/').pop() }

async function load() {
  loading.value = true
  const previousId = selected.value?.id
  try {
    const response = await http.get('/review/pending', { status: status.value })
    jobs.value = response.data || []
    selected.value = jobs.value.find(item => item.id === previousId) || jobs.value[0] || null
    if (selected.value) await loadPost(selected.value)
  } catch (error) { console.error('加载审核列表失败:', error) }
  finally { loading.value = false }
}

async function selectJob(job) {
  selected.value = job
  reviewNote.value = job.review_note || ''
  await loadPost(job)
}

async function loadPost(job) {
  post.value = null
  if (!job?.content_id) return
  try {
    const response = await opsApi.post(job.content_id)
    post.value = response.data
  } catch (error) { console.warn('读取内容版本失败:', error) }
}

function changeStatus(value) { status.value = value; selected.value = null; load() }

async function approve() {
  submitting.value = true
  try {
    await http.post(`/review/${selected.value.id}/approve`, { note: reviewNote.value.trim() })
    ElMessage.success('已通过')
    await load()
  } finally { submitting.value = false }
}

async function reject() {
  if (!reviewNote.value.trim()) return ElMessage.warning('请填写驳回原因')
  submitting.value = true
  try {
    await http.post(`/review/${selected.value.id}/reject`, { note: reviewNote.value.trim() })
    ElMessage.success('已驳回')
    await load()
  } finally { submitting.value = false }
}

onMounted(load)
</script>

<style scoped lang="scss">
@use '@/styles/variables.scss' as *;
.review-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 14px; }
.review-filters { display: flex; gap: 3px; padding: 3px; border: 1px solid $border-lighter; border-radius: 9px; background: $bg-color-raised; }
.review-filters button { min-height: 30px; padding: 0 11px; color: $text-secondary; border: 0; border-radius: 6px; background: transparent; cursor: pointer; }
.review-filters button.active { color: $text-primary; background: $paper; box-shadow: inset 0 0 0 1px $border-lighter; }
.review-layout { display: grid; grid-template-columns: 280px minmax(0, 1fr); min-height: 480px; overflow: hidden; border: 1px solid $border-lighter; border-radius: 14px; background: $paper; }
.review-list { border-right: 1px solid $border-lighter; }
.review-row { width: 100%; display: flex; flex-direction: column; gap: 7px; padding: 14px 16px; color: inherit; border: 0; border-bottom: 1px solid $border-lighter; background: transparent; text-align: left; cursor: pointer; }
.review-row:hover, .review-row.active { background: $bg-color-raised; }
.review-row.active { box-shadow: inset 2px 0 $primary-color; }
.review-row-top, .review-row-meta { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.review-row-top strong { overflow: hidden; font-size: 13px; font-weight: 500; text-overflow: ellipsis; white-space: nowrap; }
.review-row-top span { color: $text-placeholder; font-size: 10px; }
.review-row-meta { color: $text-secondary; font-size: 11px; }
.review-empty { padding: 28px 16px; color: $text-placeholder; text-align: center; }
.review-detail { padding: 24px; }
.detail-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; padding-bottom: 20px; border-bottom: 1px solid $border-lighter; }
.detail-platform, .status-text { color: $text-secondary; font-size: 12px; }
.detail-head h2 { margin-top: 6px; font-size: 20px; font-weight: 500; }
.detail-grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(220px, .8fr); gap: 28px; padding: 22px 0; }
.detail-grid h3, .review-history h3 { margin-bottom: 10px; color: $text-secondary; font-size: 12px; font-weight: 500; }
.detail-body { color: $text-regular; line-height: 1.75; white-space: pre-wrap; }
.tag-list, .file-list { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 14px; }
.tag-list span { color: $primary-color-dark; font-size: 12px; }
.file-list span { padding: 4px 7px; color: $text-secondary; border: 1px solid $border-lighter; border-radius: 5px; font-size: 11px; }
.variant-list { display: flex; flex-direction: column; gap: 10px; }
.variant-row { padding: 10px; border: 1px solid $border-lighter; border-radius: 8px; }
.variant-row strong { display: block; margin-bottom: 4px; font-size: 12px; font-weight: 500; }
.variant-row span { color: $text-regular; font-size: 12px; }
.variant-row p { margin-top: 4px; color: $text-secondary; font-size: 11px; line-height: 1.55; }
.review-action { padding-top: 18px; border-top: 1px solid $border-lighter; }
.review-action label { display: block; margin-bottom: 8px; color: $text-secondary; font-size: 12px; }
.review-buttons { display: flex; justify-content: flex-end; gap: 8px; margin-top: 12px; }
.review-history { padding-top: 18px; border-top: 1px solid $border-lighter; }
.review-history p { color: $text-regular; line-height: 1.7; }
.review-history span { display: block; margin-top: 8px; color: $text-placeholder; font-size: 11px; }
@media (max-width: 860px) {
  .review-layout { grid-template-columns: 1fr; }
  .review-list { max-height: 220px; overflow-y: auto; border-right: 0; border-bottom: 1px solid $border-lighter; }
  .detail-grid { grid-template-columns: 1fr; }
}
</style>
