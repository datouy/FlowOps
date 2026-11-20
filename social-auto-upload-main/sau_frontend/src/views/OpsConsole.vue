<template>
  <section class="diagnostics">
    <div class="diagnostics-head">
      <h2>运行状态</h2>
      <el-button :loading="loading" @click="load">重新检查</el-button>
    </div>

    <div class="health-list">
      <div v-for="item in healthItems" :key="item.key" class="health-row">
        <span class="health-dot" :class="`is-${item.tone}`" aria-hidden="true" />
        <span class="health-copy">
          <strong>{{ item.label }}</strong>
          <span>{{ item.detail }}</span>
        </span>
        <span class="health-state">{{ item.state }}</span>
      </div>
    </div>

    <div class="diagnostics-actions">
      <el-button @click="refreshAdapters">刷新平台适配</el-button>
      <el-button @click="migrate(true)">检查数据库迁移</el-button>
      <el-button @click="migrate(false)">执行迁移</el-button>
    </div>

    <div class="audit-section">
      <div class="section-title">
        <h3>最近操作</h3>
        <span class="mono">{{ audits.length }}</span>
      </div>
      <el-table :data="audits.slice(0, 10)" size="small">
        <el-table-column prop="action" label="操作" min-width="180" />
        <el-table-column prop="user" label="用户" width="120" />
        <el-table-column prop="created_at" label="时间" width="170" />
      </el-table>
    </div>

    <details class="technical-details">
      <summary>技术详情</summary>
      <div class="details-grid">
        <div><strong>执行器</strong><pre>{{ pretty(execData) }}</pre></div>
        <div><strong>运行资源</strong><pre>{{ pretty(serialData) }}</pre></div>
        <div><strong>环境检查</strong><pre>{{ pretty(fpData) }}</pre></div>
        <div><strong>权限与审计</strong><pre>{{ pretty(rbacData) }}</pre></div>
      </div>
    </details>
  </section>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { opsApi } from '@/api/ops'

defineProps({ embedded: Boolean })
const loading = ref(false)
const execData = ref({})
const serialData = ref({})
const fpData = ref({})
const rbacData = ref({})
const audits = ref([])

const healthItems = computed(() => {
  const running = execData.value.running || []
  const pool = execData.value.pool || serialData.value.pool || {}
  const problems = fpData.value.problems || []
  const rate = serialData.value.rateLimit || {}
  return [
    { key: 'executor', label: '发布执行器', detail: running.length ? `${running.length} 个任务运行中` : '空闲', state: running.length ? '运行中' : '正常', tone: 'good' },
    { key: 'browser', label: '账号浏览器', detail: `${Number(pool.active || pool.inUse || 0)} 个使用中`, state: '正常', tone: 'good' },
    { key: 'fingerprint', label: '账号环境', detail: problems.length ? problems.join('、') : '检查通过', state: problems.length ? '需检查' : '正常', tone: problems.length ? 'warning' : 'good' },
    { key: 'rate', label: '发布频率', detail: rate.reason || '未触发限制', state: rate.blocked ? '受限' : '正常', tone: rate.blocked ? 'warning' : 'good' },
    { key: 'permissions', label: '权限与审计', detail: rbacData.value.ready ? '已启用' : '读取中', state: rbacData.value.ready ? '正常' : '未知', tone: rbacData.value.ready ? 'good' : 'neutral' }
  ]
})

function pretty(value) {
  return JSON.stringify(value || {}, null, 2)
}

async function load() {
  loading.value = true
  try {
    const results = await Promise.allSettled([
      opsApi.executors(), opsApi.serialStatus(), opsApi.fingerprintProbe({}),
      opsApi.rbacStatus(), opsApi.auditLogs(50)
    ])
    if (results[0].status === 'fulfilled') execData.value = results[0].value.data || {}
    if (results[1].status === 'fulfilled') serialData.value = results[1].value.data || {}
    if (results[2].status === 'fulfilled') fpData.value = results[2].value.data || {}
    if (results[3].status === 'fulfilled') rbacData.value = results[3].value.data || {}
    if (results[4].status === 'fulfilled') audits.value = results[4].value.data || []
  } finally {
    loading.value = false
  }
}

async function refreshAdapters() {
  await opsApi.selectorsRefresh()
  ElMessage.success('平台适配已刷新')
}

async function migrate(dry) {
  const response = await opsApi.migrationsRun(dry)
  ElMessage.success(dry ? '数据库结构已检查' : '数据库迁移已完成')
  if (response.data?.pending) console.info('迁移详情', response.data)
  load()
}

onMounted(load)
</script>

<style scoped lang="scss">
@use '@/styles/variables.scss' as *;
.diagnostics { display: flex; flex-direction: column; gap: 18px; }
.diagnostics-head, .section-title { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.diagnostics-head h2, .section-title h3 { color: $text-primary; font-size: 15px; font-weight: 500; }
.health-list { overflow: hidden; border: 1px solid $border-lighter; border-radius: 12px; background: $paper; }
.health-row { display: grid; grid-template-columns: 8px minmax(0, 1fr) 70px; align-items: center; gap: 12px; padding: 14px 16px; border-bottom: 1px solid $border-lighter; }
.health-row:last-child { border-bottom: 0; }
.health-dot { width: 6px; height: 6px; border-radius: 50%; background: $ink-300; }
.health-dot.is-good { background: $success-color; }
.health-dot.is-warning { background: $warning-color; }
.health-copy { min-width: 0; display: flex; flex-direction: column; gap: 3px; }
.health-copy strong { font-size: 13px; font-weight: 500; }
.health-copy span { overflow: hidden; color: $text-secondary; font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
.health-state { color: $text-secondary; font-size: 12px; text-align: right; }
.diagnostics-actions { display: flex; gap: 8px; flex-wrap: wrap; }
.audit-section { padding-top: 4px; }
.section-title { margin-bottom: 10px; }
.section-title .mono { color: $text-placeholder; font-size: 12px; }
.technical-details { padding-top: 8px; border-top: 1px solid $border-lighter; }
.technical-details summary { color: $text-secondary; font-size: 12px; cursor: pointer; }
.details-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; margin-top: 12px; }
.details-grid strong { font-size: 12px; font-weight: 500; }
pre { margin-top: 6px; padding: 10px; overflow: auto; max-height: 220px; color: $text-secondary; border-radius: 8px; background: $bg-color-raised; font-size: 11px; line-height: 1.5; }
@media (max-width: 720px) { .details-grid { grid-template-columns: 1fr; } }
</style>
