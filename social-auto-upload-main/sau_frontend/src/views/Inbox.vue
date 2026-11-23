<template>
  <div class="inbox-page">
    <header class="page-heading">
      <div class="page-heading__copy"><h1>收件箱</h1></div>
    </header>

    <nav class="view-switch" aria-label="收件箱类型">
      <button :class="{ active: activeView === 'messages' }" @click="setView('messages')">私信</button>
      <button :class="{ active: activeView === 'comments' }" @click="setView('comments')">评论</button>
    </nav>

    <component
      :is="activeView === 'messages' ? Messages : Comments"
      :key="`${activeView}:${route.query.contact || ''}:${route.query.status || ''}`"
      embedded
    />
  </div>
</template>

<script setup>
import { onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import Messages from './Messages.vue'
import Comments from './Comments.vue'

const route = useRoute()
const router = useRouter()
const activeView = ref('messages')

function normalizeView(value) {
  return value === 'comments' ? 'comments' : 'messages'
}

function setView(value) {
  router.push({ path: '/inbox', query: { ...route.query, view: value } })
}

watch(() => route.query.view, value => {
  activeView.value = normalizeView(value)
}, { immediate: true })

onMounted(() => {
  if (!route.query.view) router.replace({ path: '/inbox', query: { ...route.query, view: 'messages' } })
})
</script>

<style scoped lang="scss">
@use '@/styles/variables.scss' as *;
.inbox-page { padding-bottom: 24px; }
.view-switch { display: flex; gap: 4px; margin: -8px 0 18px; padding: 4px; width: fit-content; border: 1px solid $border-lighter; border-radius: 10px; background: $bg-color-raised; }
.view-switch button { min-height: 32px; padding: 0 14px; color: $text-secondary; border: 0; border-radius: 7px; background: transparent; cursor: pointer; }
.view-switch button.active { color: $text-primary; background: $paper; box-shadow: inset 0 0 0 1px $border-lighter; }
</style>
