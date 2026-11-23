<template>
  <div class="settings-page">
    <header class="page-heading">
      <div class="page-heading__copy"><h1>设置</h1></div>
    </header>

    <div class="settings-layout">
      <nav class="settings-nav" aria-label="设置分类">
        <button
          v-for="item in items"
          :key="item.value"
          :class="{ active: activeView === item.value }"
          @click="setView(item.value)"
        >
          <el-icon><component :is="item.icon" /></el-icon>
          <span>{{ item.label }}</span>
        </button>
      </nav>
      <section class="settings-main">
        <component
          :is="currentComponent"
          :key="activeView"
          embedded
          :initial-filter="route.query.filter || ''"
          :initial-search="route.query.q || ''"
        />
      </section>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { DataAnalysis, InfoFilled, Lock, Picture, User, UserFilled } from '@element-plus/icons-vue'
import AccountManagement from './AccountManagement.vue'
import MaterialManagement from './MaterialManagement.vue'
import OpsConsole from './OpsConsole.vue'
import UserManagement from './UserManagement.vue'
import AccountSecurity from './AccountSecurity.vue'
import About from './About.vue'
import { useUserStore } from '@/stores/user'

const route = useRoute()
const router = useRouter()
const userStore = useUserStore()
const itemDefinitions = [
  { value: 'accounts', label: '账号', icon: User, permissions: ['account.manage'] },
  { value: 'team', label: '团队', icon: UserFilled, permissions: ['team.manage'] },
  { value: 'security', label: '安全', icon: Lock, permissions: [] },
  { value: 'materials', label: '素材', icon: Picture, permissions: ['material.write'] },
  { value: 'diagnostics', label: '运行状态', icon: DataAnalysis, permissions: ['team.manage', 'audit.read'] },
  { value: 'about', label: '关于', icon: InfoFilled, permissions: [] }
]
const items = computed(() => itemDefinitions.filter(item => !item.permissions.length || userStore.canAny(...item.permissions)))
const components = {
  accounts: AccountManagement,
  team: UserManagement,
  security: AccountSecurity,
  materials: MaterialManagement,
  diagnostics: OpsConsole,
  about: About
}
const activeView = ref('about')
const currentComponent = computed(() => components[activeView.value] || AccountManagement)

function normalizeView(value) {
  const allowed = items.value.some(item => item.value === value)
  return Object.prototype.hasOwnProperty.call(components, value) && allowed
    ? value
    : (items.value[0]?.value || 'about')
}

function setView(value) {
  router.push({ path: '/settings', query: { ...route.query, view: value, filter: undefined } })
}

watch(() => route.query.view, value => {
  const normalized = normalizeView(value)
  activeView.value = normalized
  if (value && value !== normalized) {
    router.replace({ path: '/settings', query: { ...route.query, view: normalized } })
  }
}, { immediate: true })

onMounted(() => {
  if (!route.query.view) router.replace({ path: '/settings', query: { ...route.query, view: normalizeView() } })
})
</script>

<style scoped lang="scss">
@use '@/styles/variables.scss' as *;
.settings-page { padding-bottom: 24px; }
.settings-layout { display: grid; grid-template-columns: 180px minmax(0, 1fr); align-items: start; gap: 24px; }
.settings-nav { position: sticky; top: 0; display: flex; flex-direction: column; gap: 3px; padding: 4px; border: 1px solid $border-lighter; border-radius: 12px; background: $bg-color-raised; }
.settings-nav button { display: flex; align-items: center; gap: 10px; min-height: 36px; padding: 0 10px; color: $text-secondary; border: 0; border-radius: 8px; background: transparent; text-align: left; cursor: pointer; }
.settings-nav button:hover { color: $text-primary; background: $paper; }
.settings-nav button.active { color: $text-primary; background: $paper; box-shadow: inset 0 0 0 1px $border-lighter; }
.settings-main { min-width: 0; }
@media (max-width: 900px) {
  .settings-layout { grid-template-columns: 1fr; gap: 16px; }
  .settings-nav { position: static; flex-direction: row; overflow-x: auto; }
  .settings-nav button { flex: none; }
}
</style>
