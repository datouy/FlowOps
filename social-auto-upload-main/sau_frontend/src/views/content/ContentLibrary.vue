<template>
  <section class="content-library">
    <div class="library-toolbar">
      <el-input v-model="query" clearable placeholder="搜索内容">
        <template #prefix><el-icon><Search /></el-icon></template>
      </el-input>
      <div class="status-filter">
        <button
          v-for="item in filters"
          :key="item.value"
          type="button"
          :class="{ active: status === item.value }"
          @click="status = item.value"
        >{{ item.label }}</button>
      </div>
    </div>

    <div v-loading="loading" class="post-list">
      <button
        v-for="post in filteredPosts"
        :key="post.id"
        type="button"
        class="post-row"
        @click="router.push({ path: '/content', query: { view: 'compose', id: post.id } })"
      >
        <span class="lifecycle" aria-hidden="true">
          <span class="lifecycle-dot" :class="`is-${statusTone(post.status)}`" />
        </span>
        <span class="post-copy">
          <strong>{{ post.title || '未命名内容' }}</strong>
          <span>{{ summary(post) }}</span>
        </span>
        <span class="platform-list">
          <span v-for="variant in post.variants.slice(0, 3)" :key="variant.platform">{{ platformLabel(variant.platform) }}</span>
          <span v-if="post.variants.length > 3">+{{ post.variants.length - 3 }}</span>
        </span>
        <span class="post-status" :class="`is-${statusTone(post.status)}`">{{ statusLabel(post.status) }}</span>
        <span class="post-date mono">{{ shortDate(post.updated_at || post.created_at) }}</span>
        <el-icon><ArrowRight /></el-icon>
      </button>

      <div v-if="!loading && !filteredPosts.length" class="library-empty">
        <span class="empty-line" aria-hidden="true" />
        <div>
          <strong>{{ posts.length ? '没有匹配内容' : '还没有内容' }}</strong>
          <p v-if="!posts.length">从第一条草稿开始。</p>
        </div>
        <el-button v-if="!posts.length" type="primary" @click="router.push('/content?view=compose')">新建内容</el-button>
      </div>
    </div>
  </section>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ArrowRight, Search } from '@element-plus/icons-vue'
import { opsApi } from '@/api/ops'

const router = useRouter()
const loading = ref(false)
const posts = ref([])
const query = ref('')
const status = ref('all')
const filters = [
  { value: 'all', label: '全部' },
  { value: 'draft', label: '草稿' },
  { value: 'review', label: '待审核' },
  { value: 'scheduled', label: '已排期' },
  { value: 'published', label: '已发布' },
  { value: 'attention', label: '异常' }
]
const platformNames = {
  douyin: '抖音',
  xiaohongshu: '小红书',
  channels: '视频号',
  tencent: '视频号',
  kuaishou: '快手',
  weibo: '微博',
  baijiahao: '百家号'
}

const filteredPosts = computed(() => {
  const keyword = query.value.trim().toLowerCase()
  return posts.value.filter(post => {
    const matchesStatus = status.value === 'all' || post.status === status.value
    const text = [post.title, post.body, ...(post.variants || []).map(item => item.title)].join(' ').toLowerCase()
    return matchesStatus && (!keyword || text.includes(keyword))
  })
})

function platformLabel(key) {
  return platformNames[String(key || '').toLowerCase()] || key || '平台'
}

function statusLabel(value) {
  return ({ draft: '草稿', review: '待审核', scheduled: '已排期', publishing: '发布中', published: '已发布', attention: '需处理' })[value] || '草稿'
}

function statusTone(value) {
  return ({ attention: 'danger', published: 'good', review: 'warning', scheduled: 'accent', publishing: 'warning' })[value] || 'neutral'
}

function summary(post) {
  const body = String(post.body || '').replace(/\s+/g, ' ').trim()
  return body || (post.files || []).length + ' 个素材'
}

function shortDate(value) {
  const text = String(value || '').replace('T', ' ')
  return text.slice(5, 16) || '—'
}

async function load() {
  loading.value = true
  try {
    const response = await opsApi.posts({ limit: 100 })
    posts.value = response.data?.items || response.data || []
  } catch (error) {
    console.error('加载内容失败:', error)
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<style scoped lang="scss">
@use '@/styles/variables.scss' as *;
.library-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 16px; }
.library-toolbar :deep(.el-input) { width: min(320px, 100%); }
.status-filter { display: flex; align-items: center; gap: 2px; }
.status-filter button { min-height: 32px; padding: 0 10px; color: $text-secondary; border: 0; border-radius: 7px; background: transparent; cursor: pointer; }
.status-filter button:hover { color: $text-primary; background: $bg-color-raised; }
.status-filter button.active { color: $text-primary; background: $paper; box-shadow: inset 0 0 0 1px $border-lighter; }
.post-list { overflow: hidden; min-height: 180px; border: 1px solid $border-lighter; border-radius: 14px; background: $paper; }
.post-row { width: 100%; display: grid; grid-template-columns: 18px minmax(220px, 1fr) minmax(150px, .7fr) 80px 78px 16px; align-items: center; gap: 14px; min-height: 68px; padding: 12px 18px; color: inherit; border: 0; border-bottom: 1px solid $border-lighter; background: transparent; text-align: left; cursor: pointer; transition: background-color $duration-base $ease-out; }
.post-row:last-child { border-bottom: 0; }
.post-row:hover { background: $bg-color-raised; }
.lifecycle { position: relative; align-self: stretch; }
.lifecycle::before { position: absolute; top: 10px; bottom: 10px; left: 5px; width: 1px; background: $border-light; content: ''; }
.lifecycle-dot { position: absolute; top: 50%; left: 2px; width: 7px; height: 7px; border: 1px solid $paper; border-radius: 50%; background: $ink-300; transform: translateY(-50%); }
.lifecycle-dot.is-good { background: $success-color; }
.lifecycle-dot.is-warning { background: $warning-color; }
.lifecycle-dot.is-danger { background: $danger-color; }
.lifecycle-dot.is-accent { background: $primary-color; }
.post-copy { min-width: 0; display: flex; flex-direction: column; gap: 5px; }
.post-copy strong { overflow: hidden; color: $text-primary; font-size: 14px; font-weight: 500; text-overflow: ellipsis; white-space: nowrap; }
.post-copy span { overflow: hidden; color: $text-secondary; font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
.platform-list { display: flex; flex-wrap: wrap; gap: 4px; }
.platform-list span { padding: 2px 6px; color: $text-secondary; border: 1px solid $border-lighter; border-radius: 5px; font-size: 11px; }
.post-status { font-size: 12px; }
.post-status.is-good { color: $success-color; }
.post-status.is-warning { color: $warning-color; }
.post-status.is-danger { color: $danger-color; }
.post-status.is-accent { color: $primary-color; }
.post-date { color: $text-placeholder; font-size: 11px; }
.post-row > .el-icon { color: $text-placeholder; }
.library-empty { display: flex; align-items: center; gap: 16px; padding: 32px 24px; }
.empty-line { width: 44px; height: 1px; background: $primary-color; }
.library-empty div { flex: 1; }
.library-empty strong { font-weight: 500; }
.library-empty p { margin-top: 4px; color: $text-secondary; font-size: 13px; }
@media (max-width: 900px) {
  .library-toolbar { align-items: stretch; flex-direction: column; }
  .status-filter { overflow-x: auto; }
  .post-row { grid-template-columns: 16px minmax(0, 1fr) 70px 16px; }
  .platform-list, .post-date { display: none; }
}
</style>
