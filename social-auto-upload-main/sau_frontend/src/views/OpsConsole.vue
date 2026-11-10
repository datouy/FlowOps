<template>
  <div class="ops-page">
    <div class="page-head"><div><h2>运维台面</h2><p class="sub">执行层租约 / 串行 / 指纹 / 适配包 / 审计 / 迁移，一屏可查</p></div>
      <div class="head-actions"><el-button @click="load" :loading="loading">刷新</el-button>
        <el-button @click="refreshAdapters">适配包远端刷新</el-button>
        <el-button @click="migrate(true)">迁移 dry-run</el-button>
        <el-button @click="migrate(false)">执行迁移</el-button>
      </div>
    </div>
    <el-row :gutter="14">
      <el-col :span="12"><el-card shadow="never"><template #header><span>执行器租约（P0-1）</span></template>
        <pre class="json">{{ JSON.stringify(execData, null, 2) }}</pre></el-card></el-col>
      <el-col :span="12"><el-card shadow="never"><template #header><span>串行 + 浏览器池（P0-1/P0-3）</span></template>
        <pre class="json">{{ JSON.stringify(serialData, null, 2) }}</pre></el-card></el-col>
    </el-row>
    <el-row :gutter="14" style="margin-top:14px">
      <el-col :span="12"><el-card shadow="never"><template #header><span>指纹探针 12 项（P0-2 fail-closed）</span></template>
        <pre class="json">{{ JSON.stringify(fpData, null, 2) }}</pre></el-card></el-col>
      <el-col :span="12"><el-card shadow="never"><template #header><span>RBAC / 审计（P0-5）</span></template>
        <pre class="json">{{ JSON.stringify(rbacData, null, 2) }}</pre>
        <el-table :data="audits" size="small" style="margin-top:8px"><el-table-column prop="action" label="动作" /><el-table-column prop="user" label="用户" width="90" /><el-table-column prop="created_at" label="时间" width="170" /></el-table>
      </el-card></el-col>
    </el-row>
  </div>
</template>
<script setup>
import { ref, onMounted } from 'vue'
import { opsApi } from '@/api/ops'
const loading = ref(false)
const execData = ref({})
const serialData = ref({})
const fpData = ref({})
const rbacData = ref({})
const audits = ref([])
async function load() {
  loading.value = true
  try {
    const [e, s, f, r, a] = await Promise.allSettled([opsApi.executors(), opsApi.serialStatus(), opsApi.fingerprintProbe({}), opsApi.rbacStatus(), opsApi.auditLogs(50)])
    if (e.status === 'fulfilled') execData.value = e.value.data
    if (s.status === 'fulfilled') serialData.value = s.value.data
    if (f.status === 'fulfilled') fpData.value = f.value.data
    if (r.status === 'fulfilled') rbacData.value = r.value.data
    if (a.status === 'fulfilled') audits.value = a.value.data || []
  } finally { loading.value = false }
}
async function refreshAdapters() { await opsApi.selectorsRefresh(); ElMessage.success('适配包已尝试远端刷新（未配置则本地回退）'); }
async function migrate(dry) { const r = await opsApi.migrationsRun(dry); ElMessage.success(JSON.stringify(r.data)); load(); }
onMounted(load)
</script>
<style scoped>
.ops-page { padding: 8px 4px; }
.page-head { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 14px; }
.page-head h2 { margin: 0 0 6px; }
.sub { color: #909399; font-size: 13px; margin: 0; }
.head-actions { display: flex; gap: 8px; flex-wrap: wrap; }
.json { background: #f5f7fa; border-radius: 6px; padding: 10px; max-height: 320px; overflow: auto; font-size: 12px; }
</style>
