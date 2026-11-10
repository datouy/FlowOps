<template>
  <div class="roi-page">
    <div class="page-head">
      <div><h2>线索 ROI 闭环</h2><p class="sub">单线索成本 = 预算 / 成交数，一键导出 CSV/XLSX 并推送企微/飞书</p></div>
      <div class="head-actions">
        <el-button @click="load" :loading="loading">刷新</el-button>
        <el-button @click="doExport('csv')">导出CSV</el-button>
        <el-button @click="doExport('xlsx')">导出XLSX</el-button>
        <el-button type="primary" @click="push" :loading="pushing">推送日报</el-button>
      </div>
    </div>
    <el-card shadow="never">
      <template #header><span>新建投放 campaign</span></template>
      <el-form inline>
        <el-form-item label="名称"><el-input v-model="form.name" placeholder="如 双11-抖音-测品" /></el-form-item>
        <el-form-item label="预算"><el-input-number v-model="form.budget" :min="0" /></el-form-item>
        <el-button type="primary" @click="create">创建</el-button>
      </el-form>
    </el-card>
    <el-card shadow="never" style="margin-top:14px">
      <el-table :data="rows" v-loading="loading">
        <el-table-column prop="name" label="Campaign" />
        <el-table-column prop="budget" label="预算" width="110" />
        <el-table-column prop="aiCost" label="AI成本" width="110" />
        <el-table-column label="单线索成本" width="130">
          <template #default="s">{{ costPerLead(s.row) }}</template>
        </el-table-column>
        <el-table-column label="成交" width="220">
          <template #default="s">
            <el-input-number v-model="s.row._amount" :min="0" size="small" style="width:120px" />
            <el-button size="small" @click="closeDeal(s.row)">记成交</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>
<script setup>
import { ref, onMounted } from 'vue'
import { http } from '@/utils/request'
import { opsApi } from '@/api/ops'
import { API_BASE, withToken, authHeaders } from '@/config'

const loading = ref(false)
const pushing = ref(false)
const rows = ref([])
const form = ref({ name: '', budget: 0, contentId: 0 })
async function load() {
  loading.value = true
  try { const r = await opsApi.roiSummary(); rows.value = (r.data || []).map(x => ({ ...x, _amount: 0 })) }
  catch (e) { console.error(e) } finally { loading.value = false }
}
function costPerLead(r) {
  const amt = Number(r.budget || 0)
  const n = Number(r.deals || r.n || 0)
  if (!n) return '—'
  return '￥' + (amt / n).toFixed(2)
}
async function create() {
  if (!form.value.name.trim()) return ElMessage.warning('请填写名称')
  await opsApi.roiCampaignCreate(form.value)
  ElMessage.success('已创建')
  form.value.name = ''
  load()
}
async function closeDeal(r) {
  const contactId = Number(prompt('成交线索 contactId（dm_contacts.id）:', '1') || 0)
  if (!contactId) return
  await opsApi.roiDealClose({ contactId, amount: Number(r._amount || 0), campaignId: r.id })
  ElMessage.success('已记成交并归因')
  load()
}
function doExport(fmt) {
  const url = withToken(API_BASE + opsApi.roiExport(fmt))
  window.open(url, '_blank')
}
async function push() {
  pushing.value = true
  try { const r = await opsApi.roiPush(); ElMessage.success(r.data?.pushed ? '已推送' : '推送通道未配置，已记审计') }
  finally { pushing.value = false }
}
onMounted(load)
</script>
<style scoped>
.roi-page { padding: 8px 4px; }
.page-head { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 14px; }
.page-head h2 { margin: 0 0 6px; }
.sub { color: #909399; font-size: 13px; margin: 0; }
.head-actions { display: flex; gap: 8px; }
</style>
