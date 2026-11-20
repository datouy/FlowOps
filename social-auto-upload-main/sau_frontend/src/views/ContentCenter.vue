<template>
  <div class="content-center">
    <header class="page-heading">
      <div class="page-heading__copy">
        <h1>内容</h1>
      </div>
      <div class="page-heading__actions">
        <el-button type="primary" @click="newPost">新建内容</el-button>
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

const route = useRoute()
const router = useRouter()
const activeView = ref('library')

const views = [
  { value: 'library', label: '内容库' },
  { value: 'calendar', label: '日历' },
  { value: 'pack', label: 'AI 内容包' },
  { value: 'ai', label: 'AI 文案' },
  { value: 'review', label: '审核' }
]

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

function normalizeView(value) {
  return Object.prototype.hasOwnProperty.call(components, value) ? value : 'library'
}

function setView(value) {
  const view = normalizeView(value)
  const query = { ...route.query, view }
  if (view !== 'compose') delete query.id
  router.push({ path: '/content', query })
}

function newPost() {
  router.push({ path: '/content', query: { view: 'compose' } })
}

watch(() => route.query.view, value => {
  activeView.value = normalizeView(value)
}, { immediate: true })

onMounted(() => {
  if (!route.query.view) router.replace({ path: '/content', query: { ...route.query, view: 'library' } })
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
