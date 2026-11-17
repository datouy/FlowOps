<template>
  <section class="health-card panel" aria-labelledby="health-title">
    <div class="health-header">
      <div class="health-title">
        <el-icon :size="17"><Monitor /></el-icon>
        <h2 id="health-title">服务健康</h2>
        <span v-if="loaded" class="status-key" :class="summary.ready ? 'is-good' : 'is-danger'">
          {{ summary.ready ? '运行正常' : '存在阻塞' }}
        </span>
        <span v-else class="status-key">检测中</span>
      </div>
      <el-button text :loading="loading" @click="load">刷新</el-button>
    </div>

    <div v-if="error" class="health-error">
      <el-icon><WarningFilled /></el-icon>
      <span>{{ error }}</span>
    </div>

    <template v-else>
      <div class="health-summary">
        <div class="summary-item">
          <span>检查项</span>
          <strong class="mono">{{ summary.checks.length }}</strong>
        </div>
        <div class="summary-item">
          <span>阻塞</span>
          <strong class="mono" :class="{ 'is-danger': summary.blockers.length }">
            {{ summary.blockers.length }}
          </strong>
        </div>
        <div class="summary-item">
          <span>提醒</span>
          <strong class="mono" :class="{ 'is-warning': summary.warnings.length }">
            {{ summary.warnings.length }}
          </strong>
        </div>
        <div class="summary-item">
          <span>后台任务</span>
          <strong class="mono" :class="{ 'is-danger': tasks.degraded.length }">
            {{ tasks.running }}/{{ tasks.total }}
          </strong>
        </div>
      </div>

      <div v-if="problemChecks.length" class="health-list">
        <div
          v-for="item in problemChecks"
          :key="item.name"
          class="health-row"
          :class="item.ok ? 'warn' : 'bad'"
        >
          <el-icon>
            <WarningFilled v-if="!item.ok" />
            <InfoFilled v-else />
          </el-icon>
          <div>
            <strong>{{ item.name }}</strong>
            <span>{{ item.detail }}</span>
          </div>
        </div>
      </div>
      <div v-else-if="loaded" class="health-ok">
        <span class="status-key is-good">所有检查项通过</span>
        <span v-if="summary.checkedAt" class="mono checked-at">{{ summary.checkedAt }}</span>
      </div>

      <details v-if="taskRows.length" class="task-disclosure">
        <summary>
          <span>后台任务</span>
          <span class="mono">{{ tasks.running }}/{{ tasks.total }} 正常</span>
          <el-icon :size="14"><ArrowDown /></el-icon>
        </summary>
        <div class="health-tasks">
          <div
            v-for="task in taskRows"
            :key="task.name"
            class="task-row"
            :class="task.bad ? 'bad' : 'ok'"
          >
            <span class="task-state" :class="task.bad ? 'is-danger' : 'is-good'" aria-hidden="true" />
            <div class="task-copy">
              <strong class="mono">{{ task.name }}</strong>
              <span>{{ task.description }}</span>
            </div>
            <span class="task-meta mono">
              <template v-if="task.bad">已停或卡死</template>
              <template v-else>{{ formatAge(task.beatAge) }}</template>
              <template v-if="task.restarts"> · 重启 {{ task.restarts }}</template>
            </span>
          </div>
        </div>
      </details>
    </template>
  </section>
</template>

<script setup>
import { computed, ref } from 'vue'
import {
  ArrowDown,
  InfoFilled,
  Monitor,
  WarningFilled,
} from '@element-plus/icons-vue'
import { http } from '@/utils/request'
import { useLivePolling } from '@/composables/useLivePolling'

const ready = ref({})
const runtime = ref({})
const loading = ref(false)
const loaded = ref(false)
const error = ref('')

const summary = computed(() => ({
  ready: Boolean(ready.value.ready),
  checks: ready.value.checks || [],
  blockers: ready.value.blockers || [],
  warnings: ready.value.warnings || [],
  checkedAt: ready.value.checkedAt || '',
}))

const problemChecks = computed(() =>
  summary.value.checks.filter(item => item.level !== 'ok' || !item.ok)
)

const tasks = computed(() => {
  const list = runtime.value.tasks || []
  return {
    total: list.length,
    running: list.filter(item => item.alive !== false && !item.stalled).length,
    degraded: runtime.value.degraded || [],
    list,
  }
})

const taskRows = computed(() =>
  tasks.value.list.map(item => ({
    name: item.name,
    description: item.description || '',
    beatAge: item.beatAge,
    restarts: item.restarts,
    bad: item.alive === false || item.stalled,
  }))
)

const formatAge = (seconds) => {
  if (seconds === null || seconds === undefined) return '—'
  if (seconds < 60) return `${Math.round(seconds)} 秒前`
  if (seconds < 3600) return `${Math.round(seconds / 60)} 分钟前`
  return `${Math.round(seconds / 3600)} 小时前`
}

const load = async () => {
  loading.value = true
  error.value = ''
  try {
    const [healthRes, runtimeRes] = await Promise.allSettled([
      http.get('/api/v2/system/health', null, { silent: true }),
      http.get('/api/v2/runtime/status', null, { silent: true }),
    ])
    if (healthRes.status === 'fulfilled' && healthRes.value?.data) {
      ready.value = healthRes.value.data
    } else {
      error.value = '无法获取服务健康状态，后端可能未启动'
    }
    if (runtimeRes.status === 'fulfilled' && runtimeRes.value?.data) {
      runtime.value = runtimeRes.value.data
    }
    loaded.value = true
  } finally {
    loading.value = false
  }
}

useLivePolling(load, { interval: 60000 })
load()
</script>

<style lang="scss" scoped>
@use '@/styles/variables.scss' as *;

.health-card {
  margin: 0;
  padding: 20px;
}

.health-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.health-title {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.health-title > .el-icon {
  color: $text-secondary;
}

.health-title h2 {
  margin: 0;
  color: $text-primary;
  font-size: 15px;
  font-weight: 500;
  white-space: nowrap;
}

.health-title .status-key {
  margin-left: 4px;
}

.health-error {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  margin-top: 16px;
  color: $danger-color;
  font-size: 12px;
  line-height: 1.6;
}

.health-summary {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  margin-top: 18px;
  border-block: 1px solid $border-lighter;
}

.summary-item {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 14px 12px;
}

.summary-item:first-child {
  padding-left: 0;
}

.summary-item:nth-child(odd) {
  border-right: 1px solid $border-lighter;
}

.summary-item:nth-child(n + 3) {
  border-top: 1px solid $border-lighter;
}

.summary-item span {
  color: $text-secondary;
  font-size: 11px;
}

.summary-item strong {
  color: $text-primary;
  font-size: 22px;
  font-weight: 500;
  line-height: 1.1;
}

.summary-item strong.is-danger {
  color: $danger-color;
}

.summary-item strong.is-warning {
  color: $warning-color;
}

.health-list {
  display: flex;
  flex-direction: column;
  padding-top: 8px;
}

.health-row {
  display: flex;
  align-items: flex-start;
  gap: 9px;
  padding: 10px 0;
  border-bottom: 1px solid $border-lighter;
  color: $warning-color;
}

.health-row:last-child {
  border-bottom: 0;
}

.health-row.bad {
  color: $danger-color;
}

.health-row > .el-icon {
  flex: none;
  margin-top: 2px;
}

.health-row > div {
  display: flex;
  flex-direction: column;
  min-width: 0;
  gap: 3px;
}

.health-row strong {
  color: currentColor;
  font-size: 12px;
  font-weight: 500;
}

.health-row span {
  color: $text-secondary;
  font-size: 11px;
  line-height: 1.5;
}

.health-ok {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 14px 0 4px;
}

.checked-at {
  overflow: hidden;
  color: $text-secondary;
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.task-disclosure {
  margin-top: 8px;
  border-top: 1px solid $border-lighter;
}

.task-disclosure summary {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto 14px;
  align-items: center;
  gap: 8px;
  min-height: 42px;
  color: $text-regular;
  font-size: 12px;
  cursor: pointer;
  list-style: none;
}

.task-disclosure summary::-webkit-details-marker {
  display: none;
}

.task-disclosure summary > span:nth-child(2) {
  color: $text-secondary;
  font-size: 11px;
}

.task-disclosure summary .el-icon {
  color: $text-secondary;
  transition: transform $duration-base $ease-out;
}

.task-disclosure[open] summary .el-icon {
  transform: rotate(180deg);
}

.health-tasks {
  max-height: 360px;
  overflow-y: auto;
  padding-bottom: 4px;
}

.task-row {
  display: grid;
  grid-template-columns: 6px minmax(0, 1fr) auto;
  align-items: start;
  gap: 9px;
  padding: 9px 0;
  border-top: 1px solid $border-lighter;
}

.task-state {
  width: 6px;
  height: 6px;
  margin-top: 5px;
  border-radius: 1px;
  background: $success-color;
}

.task-state.is-danger {
  background: $danger-color;
}

.task-copy {
  display: flex;
  flex-direction: column;
  min-width: 0;
  gap: 2px;
}

.task-copy strong {
  overflow: hidden;
  color: $text-primary;
  font-size: 11px;
  font-weight: 500;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.task-copy span {
  color: $text-secondary;
  font-size: 11px;
  line-height: 1.45;
}

.task-meta {
  color: $text-secondary;
  font-size: 11px;
  white-space: nowrap;
}

.task-row.bad .task-meta {
  color: $danger-color;
}

@media (max-width: 420px) {
  .health-card {
    padding: 18px;
  }

  .health-title .status-key {
    display: none;
  }
}
</style>
