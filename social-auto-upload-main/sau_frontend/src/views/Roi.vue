<template>
  <div class="roi-page">
    <div v-if="!embedded" class="page-head">
      <div><h2>线索 ROI</h2></div>
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
            <el-button size="small" @click="openDeal(s.row)">记成交</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
    <el-dialog v-model="dealDialog" title="记录成交" width="480px">
      <el-form label-position="top">
        <el-form-item label="成交线索">
          <el-select
            v-model="dealForm.contactId"
            filterable
            remote
            :remote-method="searchContacts"
            :loading="contactLoading"
            placeholder="搜索联系人"
            style="width:100%"
          >
            <el-option
              v-for="contact in contacts"
              :key="contact.id"
              :label="contactLabel(contact)"
              :value="contact.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="成交金额">
          <el-input-number v-model="dealForm.amount" :min="0" controls-position="right" style="width:100%" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dealDialog = false">取消</el-button>
        <el-button type="primary" :loading="dealSaving" @click="saveDeal">记录成交</el-button>
      </template>
    </el-dialog>
  </div>
</template>
<script setup>
defineProps({ embedded: Boolean })
import { ref, onMounted } from 'vue'
import { opsApi } from '@/api/ops'
import { dmApi } from '@/api/dm'
import { API_BASE, withToken } from '@/config'

const loading = ref(false)
const pushing = ref(false)
const rows = ref([])
const form = ref({ name: '', budget: 0 })
const dealDialog = ref(false)
const dealSaving = ref(false)
const contactLoading = ref(false)
const contacts = ref([])
const dealForm = ref({ campaignId: null, contactId: null, amount: 0 })
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
function openDeal(row) {
  dealForm.value = { campaignId: row.id, contactId: null, amount: Number(row._amount || 0) }
  dealDialog.value = true
  searchContacts('')
}
function contactLabel(contact) {
  return [contact.nickname, contact.platform, contact.account].filter(Boolean).join(' · ') || `线索 #${contact.id}`
}
async function searchContacts(query) {
  contactLoading.value = true
  try {
    const response = await dmApi.getConversations({ q: query || '', page: 1, page_size: 30 })
    contacts.value = response.data?.items || response.data || []
  } catch (error) {
    console.warn('加载线索失败:', error)
  } finally {
    contactLoading.value = false
  }
}
async function saveDeal() {
  if (!dealForm.value.contactId) return ElMessage.warning('请选择成交线索')
  dealSaving.value = true
  try {
    await opsApi.roiDealClose({
      contactId: dealForm.value.contactId,
      amount: Number(dealForm.value.amount || 0),
      campaignId: dealForm.value.campaignId
    })
    ElMessage.success('已记成交并归因')
    dealDialog.value = false
    load()
  } finally {
    dealSaving.value = false
  }
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
