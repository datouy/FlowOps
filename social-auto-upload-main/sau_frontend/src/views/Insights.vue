<template>
  <div class="insights-page">
    <header class="page-heading">
      <div class="page-heading__copy"><h1>洞察</h1></div>
    </header>

    <nav class="view-switch" aria-label="洞察视图">
      <button
        v-for="item in views"
        :key="item.value"
        :class="{ active: activeView === item.value }"
        @click="setView(item.value)"
      >{{ item.label }}</button>
    </nav>

    <component :is="currentComponent" :key="activeView" embedded />
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import WeeklyReport from './WeeklyReport.vue'
import PublishRecords from './PublishRecords.vue'
import Roi from './Roi.vue'

const route = useRoute()
const router = useRouter()
const views = [
  { value: 'overview', label: '概览' },
  { value: 'works', label: '作品' },
  { value: 'leads', label: '线索' }
]
const components = { overview: WeeklyReport, works: PublishRecords, leads: Roi }
const activeView = ref('overview')
const currentComponent = computed(() => components[activeView.value] || WeeklyReport)

function normalizeView(value) {
  return Object.prototype.hasOwnProperty.call(components, value) ? value : 'overview'
}

function setView(value) {
  router.push({ path: '/insights', query: { ...route.query, view: value } })
}

watch(() => route.query.view, value => {
  activeView.value = normalizeView(value)
}, { immediate: true })

onMounted(() => {
  if (!route.query.view) router.replace({ path: '/insights', query: { ...route.query, view: 'overview' } })
})
</script>

<style scoped lang="scss">
@use '@/styles/variables.scss' as *;
.insights-page { padding-bottom: 24px; }
.view-switch { display: flex; gap: 4px; margin: -8px 0 18px; padding: 4px; width: fit-content; border: 1px solid $border-lighter; border-radius: 10px; background: $bg-color-raised; }
.view-switch button { min-height: 32px; padding: 0 14px; color: $text-secondary; border: 0; border-radius: 7px; background: transparent; cursor: pointer; }
.view-switch button.active { color: $text-primary; background: $paper; box-shadow: inset 0 0 0 1px $border-lighter; }
</style>
