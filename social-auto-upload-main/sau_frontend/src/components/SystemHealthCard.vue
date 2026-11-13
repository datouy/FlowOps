<template>
  <el-card class="health-card">
    <template #header>
      <div class="health-header">
        <div class="health-title">
          <el-icon><Monitor /></el-icon>
          <span>运行状态</span>
          <el-tag
            v-if="loaded"
            size="small"
            :type="summary.ready ? 'success' : 'danger'"
            effect="dark"
          >
            {{ summary.ready ? '服务正常' : '有阻塞项' }}
          </el-tag>
          <el-tag v-else size="small" type="info">检测中</el-tag>
        </div>
        <div class="health-actions">
          <span class="checked-at">{{ summary.checkedAt || '' }}</span>
          <el-button link :loading="loading" @click="load">刷新</el-button>
        </div>
      </div>
    </template>

    <div v-if="error" class="health-error">
      <el-icon><WarningFilled /></el-icon>
      <span>{{ error }}</span>
    </div>

    <template v-else>
      <el-row :gutter="16" class="health-summary">
        <el-col :span="6">
          <div class="summary-item">
            <div class="summary-value">{{ summary.checks.length }}</div>
            <div class="summary-label">检查项</div>
          </div>
        </el-col>
        <el-col :span="6">
          <div class="summary-item">
            <div class="summary-value" :class="{ bad: summary.blockers.length }">
              {{ summary.blockers.length }}
            </div>
            <div class="summary-label">阻塞</div>
          </div>
        </el-col>
        <el-col :span="6">
          <div class="summary-item">
            <div class="summary-value" :class="{ warn: summary.warnings.length }">
              {{ summary.warnings.length }}
            </div>
            <div class="summary-label">提醒</div>
          </div>
        </el-col>
        <el-col :span="6">
          <div class="summary-item">
            <div class="summary-value" :class="{ bad: tasks.degraded.length }">
              {{ tasks.running }}/{{ tasks.total }}
            </div>
            <div class="summary-label">后台任务</div>
          </div>
        </el-col>
      </el-row>

      <!-- 只显示有问题的项：全绿的时候列十几条只会让人懒得看 -->
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
          <span class="row-name">{{ item.name }}</span>
          <span class="row-detail">{{ item.detail }}</span>
        </div>
      </div>
      <div v-else-if="loaded" class="health-ok">
        <el-icon><CircleCheckFilled /></el-icon>
        <span>所有检查项通过</span>
      </div>

      <div v-if="taskRows.length" class="health-tasks">
        <div class="tasks-title">后台任务（线程活着 + 心跳正常才算健康）</div>
        <div
          v-for="task in taskRows"
          :key="task.name"
          class="task-row"
          :class="task.bad ? 'bad' : 'ok'"
        >
          <span class="task-name">{{ task.name }}</span>
          <span class="task-desc">{{ task.description }}</span>
          <span class="task-meta">
            <template v-if="task.bad">已停/卡死</template>
            <template v-else>心跳 {{ formatAge(task.beatAge) }}</template>
            <template v-if="task.restarts"> · 重启 {{ task.restarts }} 次</template>
          </span>
        </div>
      </div>
    </template>
  </el-card>
</template>

<script setup>
import { computed, ref } from 'vue'
import {
  CircleCheckFilled,
  InfoFilled,
  Monitor,
  WarningFilled
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
  checkedAt: ready.value.checkedAt || ''
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
    list
  }
})

const taskRows = computed(() =>
  tasks.value.list.map(item => ({
    name: item.name,
    description: item.description || '',
    beatAge: item.beatAge,
    restarts: item.restarts,
    bad: item.alive === false || item.stalled
  }))
)

const formatAge = (seconds) => {
  if (seconds === null || seconds === undefined) return '—'
  if (seconds < 60) return `${Math.round(seconds)} 秒前`
  if (seconds < 3600) return `${Math.round(seconds / 60)} 分钟前`
  return `${Math.round(seconds / 3600)} 小时前`
}

// 用 distinct 的接口：健康快照永远 200，未就绪不会触发全局错误弹窗
const load = async () => {
  loading.value = true
  error.value = ''
  try {
    const [healthRes, runtimeRes] = await Promise.allSettled([
      http.get('/api/v2/system/health', null, { silent: true }),
      http.get('/api/v2/runtime/status', null, { silent: true })
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
  margin-bottom: 20px;

  .health-header {
    display: flex;
    align-items: center;
    justify-content: space-between;

    .health-title {
      display: flex;
      align-items: center;
      gap: 8px;
      font-weight: 600;
      color: $text-primary;
    }

    .health-actions {
      display: flex;
      align-items: center;
      gap: 10px;

      .checked-at {
        font-size: 12px;
        color: $text-secondary;
      }
    }
  }

  .health-error {
    display: flex;
    align-items: center;
    gap: 8px;
    color: $danger-color;
    font-size: 13px;
  }

  .health-summary {
    .summary-item {
      text-align: center;
      padding: 8px 0;

      .summary-value {
        font-size: 22px;
        font-weight: 600;
        color: $text-primary;

        &.bad {
          color: $danger-color;
        }

        &.warn {
          color: $warning-color;
        }
      }

      .summary-label {
        font-size: 12px;
        color: $text-secondary;
      }
    }
  }

  .health-list {
    border-top: 1px solid $border-lighter;
    padding-top: 10px;

    .health-row {
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: 13px;
      padding: 4px 0;

      .row-name {
        min-width: 160px;
        color: $text-primary;
      }

      .row-detail {
        color: $text-secondary;
      }

      &.bad {
        color: $danger-color;

        .row-name,
        .row-detail {
          color: $danger-color;
        }
      }

      &.warn {
        color: $warning-color;

        .row-name,
        .row-detail {
          color: $warning-color;
        }
      }
    }
  }

  .health-ok {
    display: flex;
    align-items: center;
    gap: 8px;
    border-top: 1px solid $border-lighter;
    padding-top: 10px;
    color: $success-color;
    font-size: 13px;
  }

  .health-tasks {
    margin-top: 12px;
    border-top: 1px solid $border-lighter;
    padding-top: 10px;

    .tasks-title {
      font-size: 12px;
      color: $text-secondary;
      margin-bottom: 6px;
    }

    .task-row {
      display: flex;
      align-items: center;
      gap: 10px;
      font-size: 12px;
      padding: 3px 0;

      .task-name {
        min-width: 150px;
        color: $text-primary;
      }

      .task-desc {
        flex: 1;
        color: $text-secondary;
      }

      .task-meta {
        color: $text-secondary;
      }

      &.bad {
        .task-name,
        .task-meta {
          color: $danger-color;
        }
      }
    }
  }
}
</style>
