<template>
  <div class="content-center">
    <header class="page-heading">
      <div class="page-heading__copy">
        <h1>内容</h1>
      </div>
      <div class="page-heading__actions">
        <el-button v-if="userStore.can('content.edit')" type="primary" @click="newPost">新建内容</el-button>
      </div>
    </header>

    <nav class="view-switch" aria-label="内容视图">
      <button
        v-for="item in views"
        :key="item.value"
        type="button"
        :class="{ active: activeView === item.value }"
        @click="setView(item.value)"
      >{{ item.label }}</button>
    </nav>

    <component :is="currentComponent" :key="viewKey" embedded />
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import ContentLibrary from './content/ContentLibrary.vue'
import PostComposer from './content/PostComposer.vue'
import PublishCalendar from './PublishCalendar.vue'
import ContentPack from './ContentPack.vue'
import AIContent from './AIContent.vue'
import Review from './Review.vue'
import { useUserStore } from '@/stores/user'

const route = useRoute()
const router = useRouter()
const userStore = useUserStore()
const activeView = ref('library')

const viewDefinitions = [
  { value: 'library', label: '内容库', permissions: ['content.edit', 'publish.execute', 'review.decide'] },
  { value: 'calendar', label: '日历', permissions: ['publish.execute'] },
  { value: 'pack', label: 'AI 内容包', permissions: ['content.edit'] },
  { value: 'ai', label: 'AI 文案', permissions: ['content.edit'] },
  { value: 'review', label: '审核', permissions: ['review.decide'] }
]
const views = computed(() => viewDefinitions.filter(item => userStore.canAny(...item.permissions)))

const components = {
  library: ContentLibrary,
  compose: PostComposer,
  calendar: PublishCalendar,
  pack: ContentPack,
  ai: AIContent,
  review: Review
}

const currentComponent = computed(() => components[activeView.value] || ContentLibrary)
const viewKey = computed(() => `${activeView.value}:${route.query.id || ''}`)

function viewAllowed(value) {
  if (value === 'compose') return userStore.can('content.edit')
  const definition = viewDefinitions.find(item => item.value === value)
  return Boolean(definition && userStore.canAny(...definition.permissions))
}

function defaultView() {
  if (userStore.can('content.edit')) return 'library'
  if (userStore.can('review.decide')) return 'review'
  return views.value[0]?.value || 'library'
}

function normalizeView(value) {
  return Object.prototype.hasOwnProperty.call(components, value) && viewAllowed(value)
    ? value
    : defaultView()
}

function setView(value) {
  const view = normalizeView(value)
  const query = { ...route.query, view }
  if (view !== 'compose') delete query.id
  router.push({ path: '/content', query })
}

function newPost() {
  if (!userStore.can('content.edit')) return
  router.push({ path: '/content', query: { view: 'compose' } })
}

watch(() => route.query.view, value => {
  const normalized = normalizeView(value)
  activeView.value = normalized
  if (value && value !== normalized) {
    router.replace({ path: '/content', query: { ...route.query, view: normalized } })
  }
}, { immediate: true })

onMounted(() => {
  if (!route.query.view) router.replace({ path: '/content', query: { ...route.query, view: defaultView() } })
})
</script>

<style scoped lang="scss">
@use '@/styles/variables.scss' as *;

.content-center { padding-bottom: 32px; }
.view-switch {
  display: flex;
  align-items: center;
  gap: 4px;
  margin: -8px 0 20px;
  padding: 4px;
  width: fit-content;
  border: 1px solid $border-lighter;
  border-radius: 10px;
  background: $bg-color-raised;
}
.view-switch button {
  min-height: 32px;
  padding: 0 12px;
  color: $text-secondary;
  border: 0;
  border-radius: 7px;
  background: transparent;
  cursor: pointer;
  transition: color $duration-base $ease-out, background-color $duration-base $ease-out;
}
.view-switch button:hover { color: $text-primary; }
.view-switch button.active { color: $text-primary; background: $paper; box-shadow: inset 0 0 0 1px $border-lighter; }
@media (max-width: 720px) {
  .view-switch { width: 100%; overflow-x: auto; }
  .view-switch button { flex: none; }
}
</style>
