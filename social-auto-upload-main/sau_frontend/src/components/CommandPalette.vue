<template>
  <Teleport to="body">
    <Transition name="palette">
      <div v-if="modelValue" class="palette-layer" @mousedown.self="close">
        <section class="palette" role="dialog" aria-modal="true" aria-label="搜索工作台">
          <header class="palette-search">
            <el-icon><Search /></el-icon>
            <input
              ref="inputRef"
              v-model="query"
              type="search"
              autocomplete="off"
              spellcheck="false"
              placeholder="搜索"
              aria-label="搜索内容、账号、素材和会话"
              @keydown="onKeydown"
            />
            <button type="button" aria-label="关闭搜索" @click="close">Esc</button>
          </header>

          <div class="palette-results" :aria-busy="loading">
            <button
              v-for="(item, index) in results"
              :key="item.key"
              type="button"
              class="palette-row"
              :class="{ active: index === selectedIndex }"
              @mouseenter="selectedIndex = index"
              @click="choose(item)"
            >
              <span class="result-kind">{{ item.kind }}</span>
              <span class="result-copy">
                <strong>{{ item.label }}</strong>
                <span v-if="item.detail">{{ item.detail }}</span>
              </span>
              <el-icon><ArrowRight /></el-icon>
            </button>

            <div v-if="!results.length && !loading" class="palette-empty">
              {{ query.trim() ? '没有匹配结果' : '直接选择一个操作' }}
            </div>
          </div>

          <footer class="palette-footer">
            <span><kbd>↑</kbd><kbd>↓</kbd> 选择</span>
            <span><kbd>Enter</kbd> 打开</span>
            <span v-if="error" class="palette-error">{{ error }}</span>
          </footer>
        </section>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ArrowRight, Search } from '@element-plus/icons-vue'
import { http } from '@/utils/request'
import { useAccountStore } from '@/stores/account'
import { useUserStore } from '@/stores/user'

const props = defineProps({
  modelValue: Boolean
})
const emit = defineEmits(['update:modelValue'])
const router = useRouter()
const accountStore = useAccountStore()
const userStore = useUserStore()
const inputRef = ref(null)
const query = ref('')
const loading = ref(false)
const error = ref('')
const selectedIndex = ref(0)
const posts = ref([])
const materials = ref([])
const conversations = ref([])
let loadedAt = 0
let previousOverflow = ''

const actionDefinitions = [
  { key: 'action:new', kind: '操作', label: '新建内容', detail: '写一条内容并选择发布平台', search: '新建 发布 草稿 内容', to: '/content?view=compose', permissions: ['content.edit'] },
  { key: 'action:today', kind: '操作', label: '今天', detail: '查看待处理事项', search: '今天 待办 审核 回复', to: '/today', permissions: ['content.edit', 'review.decide', 'publish.execute', 'message.operate', 'account.manage', 'team.manage', 'audit.read'] },
  { key: 'action:review', kind: '操作', label: '审核内容', detail: '处理等待审核的发布任务', search: '审核 通过 驳回', to: '/content?view=review', permissions: ['review.decide'] },
  { key: 'action:calendar', kind: '操作', label: '发布日历', detail: '查看排期', search: '日历 排期 定时', to: '/content?view=calendar', permissions: ['publish.execute'] },
  { key: 'action:inbox', kind: '操作', label: '收件箱', detail: '回复私信和评论', search: '收件箱 私信 评论 回复', to: '/inbox?view=messages', permissions: ['message.operate'] },
  { key: 'action:settings', kind: '操作', label: '设置', detail: '账号、素材和运行状态', search: '设置 账号 素材 运行状态', to: '/settings', permissions: ['material.write', 'account.manage', 'team.manage', 'audit.read'] }
]
const actions = computed(() => actionDefinitions.filter(item => userStore.canAny(...item.permissions)))

const sourceItems = computed(() => [
  ...posts.value.map(post => ({
    key: `post:${post.id}`,
    kind: '内容',
    label: post.title || '未命名内容',
    detail: [post.status, String(post.body || '').replace(/\s+/g, ' ').trim()].filter(Boolean).join(' · '),
    search: [post.title, post.body, ...(post.variants || []).map(item => item.title)].join(' '),
    to: { path: '/content', query: { view: 'compose', id: post.id } }
  })),
  ...accountStore.accounts.map(account => ({
    key: `account:${account.id}`,
    kind: '账号',
    label: account.name || '未命名账号',
    detail: `${account.platform} · ${account.status}`,
    search: `${account.name} ${account.platform} ${account.status}`,
    to: { path: '/settings', query: { view: 'accounts', filter: account.name } }
  })),
  ...materials.value.map((material, index) => ({
    key: `material:${material.uuid || material.file_path || index}`,
    kind: '素材',
    label: material.filename || material.file_path || '素材',
    detail: material.tags || material.filesize ? [material.tags, material.filesize ? `${material.filesize} MB` : ''].filter(Boolean).join(' · ') : '',
    search: `${material.filename || ''} ${material.file_path || ''} ${material.tags || ''}`,
    to: { path: '/settings', query: { view: 'materials', q: material.filename || '' } }
  })),
  ...conversations.value.map(conversation => ({
    key: `conversation:${conversation.id}`,
    kind: '会话',
    label: conversation.nickname || conversation.author || '匿名用户',
    detail: conversation.last_content || conversation.recommended_action || '',
    search: `${conversation.nickname || ''} ${conversation.author || ''} ${conversation.last_content || ''} ${conversation.platform || ''}`,
    to: { path: '/inbox', query: { view: 'messages', contact: conversation.id } }
  }))
])

const results = computed(() => {
  const keyword = query.value.trim().toLowerCase()
  const source = [...actions.value, ...sourceItems.value]
  if (!keyword) {
    return [
      ...actions.value.slice(0, 4),
      ...posts.value.slice(0, 4).map(post => sourceItems.value.find(item => item.key === `post:${post.id}`)),
      ...accountStore.accounts.slice(0, 3).map(account => sourceItems.value.find(item => item.key === `account:${account.id}`))
    ].filter(Boolean)
  }
  return source
    .filter(item => `${item.label} ${item.detail || ''} ${item.search || ''}`.toLowerCase().includes(keyword))
    .slice(0, 14)
})

async function loadSources(force = false) {
  if (!force && Date.now() - loadedAt < 30_000) return
  loading.value = true
  error.value = ''
  const [postResult, materialResult, conversationResult, accountResult] = await Promise.allSettled([
    userStore.canAny('content.edit', 'review.decide', 'publish.execute')
      ? http.get('/api/v2/posts', { limit: 50 }, { silent: true })
      : Promise.resolve({ data: { items: [] } }),
    userStore.canAny('content.edit', 'material.write', 'review.decide', 'publish.execute')
      ? http.get('/getFiles', { page: 1, page_size: 100 }, { silent: true })
      : Promise.resolve({ data: [] }),
    userStore.can('message.operate')
      ? http.get('/dm/conversations', { page: 1, page_size: 50 }, { silent: true })
      : Promise.resolve({ data: [] }),
    userStore.canAny('content.edit', 'publish.execute', 'message.operate', 'account.manage')
      ? accountStore.loadAccounts(true)
      : Promise.resolve()
  ])
  if (postResult.status === 'fulfilled') {
    const data = postResult.value.data || {}
    posts.value = data.items || (Array.isArray(data) ? data : [])
  }
  if (materialResult.status === 'fulfilled') materials.value = materialResult.value.data || []
  if (conversationResult.status === 'fulfilled') conversations.value = conversationResult.value.data || []
  if (accountResult.status === 'rejected') {
    error.value = '部分结果暂不可用'
  }
  if ([postResult, materialResult, conversationResult].some(item => item.status === 'rejected')) {
    error.value = '部分结果暂不可用'
  }
  loadedAt = Date.now()
  loading.value = false
}

function close() {
  emit('update:modelValue', false)
}

function choose(item) {
  if (!item) return
  close()
  router.push(item.to)
}

function onKeydown(event) {
  if (event.isComposing) return
  if (event.key === 'Escape') {
    event.preventDefault()
    close()
    return
  }
  if (event.key === 'ArrowDown') {
    event.preventDefault()
    selectedIndex.value = results.value.length ? (selectedIndex.value + 1) % results.value.length : 0
    return
  }
  if (event.key === 'ArrowUp') {
    event.preventDefault()
    selectedIndex.value = results.value.length ? (selectedIndex.value - 1 + results.value.length) % results.value.length : 0
    return
  }
  if (event.key === 'Enter') {
    event.preventDefault()
    choose(results.value[selectedIndex.value])
  }
}

watch(() => props.modelValue, async open => {
  if (open) {
    query.value = ''
    selectedIndex.value = 0
    previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    loadSources()
    await nextTick()
    inputRef.value?.focus()
  } else {
    document.body.style.overflow = previousOverflow
  }
})

watch(results, () => {
  selectedIndex.value = Math.min(selectedIndex.value, Math.max(results.value.length - 1, 0))
})

onBeforeUnmount(() => {
  document.body.style.overflow = previousOverflow
})
</script>

<style scoped lang="scss">
@use '@/styles/variables.scss' as *;

.palette-layer {
  position: fixed;
  inset: 0;
  z-index: 3000;
  display: flex;
  justify-content: center;
  padding: min(16vh, 140px) 16px 24px;
  background: rgba(23, 26, 24, 0.34);
  backdrop-filter: blur(3px);
}

.palette {
  width: min(640px, 100%);
  height: fit-content;
  max-height: min(680px, 72vh);
  overflow: hidden;
  border: 1px solid rgba(255, 255, 255, 0.28);
  border-radius: 14px;
  background: $paper;
  box-shadow: 0 24px 80px rgba(23, 26, 24, 0.28);
}

.palette-search {
  display: flex;
  align-items: center;
  gap: 12px;
  min-height: 58px;
  padding: 0 16px;
  border-bottom: 1px solid $border-lighter;
}
.palette-search > .el-icon { color: $text-secondary; font-size: 18px; }
.palette-search input { min-width: 0; flex: 1; color: $text-primary; border: 0; outline: 0; background: transparent; font: inherit; font-size: 16px; }
.palette-search input::placeholder { color: $text-placeholder; }
.palette-search button { padding: 4px 7px; color: $text-placeholder; border: 1px solid $border-lighter; border-radius: 5px; background: transparent; font: inherit; font-size: 10px; cursor: pointer; }

.palette-results { max-height: 520px; overflow-y: auto; padding: 6px; }
.palette-row { width: 100%; display: grid; grid-template-columns: 44px minmax(0, 1fr) 16px; align-items: center; gap: 10px; min-height: 52px; padding: 7px 10px; color: inherit; border: 0; border-radius: 8px; background: transparent; text-align: left; cursor: pointer; }
.palette-row.active { background: $bg-color-raised; }
.result-kind { color: $text-placeholder; font-size: 11px; }
.result-copy { min-width: 0; display: flex; flex-direction: column; gap: 3px; }
.result-copy strong, .result-copy span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.result-copy strong { color: $text-primary; font-size: 13px; font-weight: 500; }
.result-copy span { color: $text-secondary; font-size: 11px; }
.palette-row > .el-icon { color: $text-placeholder; }
.palette-row.active > .el-icon { color: $text-regular; }
.palette-empty { padding: 34px 16px; color: $text-secondary; text-align: center; font-size: 13px; }
.palette-footer { display: flex; align-items: center; gap: 18px; min-height: 38px; padding: 0 14px; color: $text-placeholder; border-top: 1px solid $border-lighter; font-size: 10px; }
.palette-footer span { display: inline-flex; align-items: center; gap: 4px; }
.palette-footer kbd { min-width: 18px; padding: 2px 4px; color: $text-secondary; border: 1px solid $border-lighter; border-radius: 4px; background: $bg-color-raised; text-align: center; font: inherit; }
.palette-error { margin-left: auto; color: $warning-color; }
.palette-enter-active, .palette-leave-active { transition: opacity $duration-base $ease-out; }
.palette-enter-active .palette, .palette-leave-active .palette { transition: transform $duration-base $ease-out, opacity $duration-base $ease-out; }
.palette-enter-from, .palette-leave-to { opacity: 0; }
.palette-enter-from .palette, .palette-leave-to .palette { opacity: 0; transform: translateY(-8px) scale(0.985); }

@media (max-width: 720px) {
  .palette-layer { padding-top: 10vh; }
  .palette { max-height: 78vh; }
  .palette-results { max-height: calc(78vh - 96px); }
  .palette-footer span:nth-child(2) { display: none; }
}
</style>
