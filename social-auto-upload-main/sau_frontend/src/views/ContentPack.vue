<template>
  <div class="content-pack">
    <div class="page-head">
      <div>
        <h2>多平台内容包</h2>
        <p class="sub">输入一次产品或服务信息，生成 6 个平台的差异化标题、正文和话题，并可生成 3:4 / 9:16 封面。</p>
      </div>
    </div>

    <el-row :gutter="18">
      <el-col :xs="24" :lg="9">
        <el-card shadow="never">
          <template #header><span>内容条件</span></template>
          <el-form label-width="96px">
            <el-form-item label="行业/类型">
              <el-select v-model="form.industry" style="width:100%">
                <el-option v-for="item in industries" :key="item" :label="item" :value="item" />
              </el-select>
            </el-form-item>
            <el-form-item v-if="form.industry === '其他'" label="自定义行业">
              <el-input v-model="form.custom_industry" placeholder="例如：宠物服务、软件、家居、教育" />
            </el-form-item>
            <el-form-item label="产品/服务名称">
              <el-input v-model="form.business_name" placeholder="例如：新品名称、课程名称、服务套餐" />
            </el-form-item>
            <el-form-item label="主题">
              <el-input v-model="form.topic" placeholder="例如：新品上市、使用场景、活动推广、品牌介绍" />
            </el-form-item>
            <el-form-item label="目标人群">
              <el-input v-model="form.audience" placeholder="例如：年轻上班族、新手父母、企业客户" />
            </el-form-item>
            <el-form-item label="规格/周期">
              <el-input v-model="form.specification" placeholder="选填，例如：标准版、3个月、单次服务" />
            </el-form-item>
            <el-form-item label="价格/定位">
              <el-input v-model="form.price" placeholder="选填，例如：299元、面议、中高端" />
            </el-form-item>
            <el-form-item label="核心卖点">
              <el-input v-model="form.highlights" type="textarea" :rows="3" placeholder="填写真实功能、服务内容、使用体验或差异化价值" />
            </el-form-item>
            <el-form-item label="素材">
              <el-select v-model="sourceFile" placeholder="选择一条素材用于生成封面" clearable style="width:100%">
                <el-option v-for="file in materials" :key="file.file_path" :label="file.filename" :value="file.file_path" />
              </el-select>
            </el-form-item>
            <el-form-item>
              <el-button type="primary" :loading="loading" @click="generate" style="width:100%">生成 6 平台内容包</el-button>
              <el-progress v-if="packRunning" :percentage="packProgress" :stroke-width="8" style="width:100%;margin-top:10px" />
              <small v-if="packRunning" class="progress-text">正在并行生成各平台内容，请稍候…</small>
            </el-form-item>
            <el-form-item v-if="sourceFile">
              <el-button @click="makeCovers" :loading="coverLoading" style="width:100%">生成 3:4 / 9:16 封面</el-button>
            </el-form-item>
          </el-form>
        </el-card>
        <el-card v-if="covers.length" shadow="never" style="margin-top:14px">
          <template #header><span>自动封面</span></template>
          <div class="cover-grid">
            <div v-for="cover in covers" :key="cover.name" class="cover-item">
              <img :src="fileUrl(cover.url)" :alt="cover.name" />
              <span>{{ cover.name }}</span>
            </div>
          </div>
        </el-card>
      </el-col>

      <el-col :xs="24" :lg="15">
        <el-card shadow="never">
          <template #header><span>平台内容</span></template>
          <el-tabs v-if="items.length" v-model="activePlatform">
            <el-tab-pane v-for="item in items" :key="item.platform" :label="item.platform" :name="item.platform">
              <el-form label-width="56px">
                <el-form-item label="标题"><el-input v-model="item.title" /></el-form-item>
                <el-form-item label="正文"><el-input v-model="item.body" type="textarea" :rows="10" /></el-form-item>
                <el-form-item label="话题"><el-input v-model="tagsText[item.platform]" /></el-form-item>
                <el-form-item>
                  <el-button @click="copyItem(item)">复制</el-button>
                  <el-button type="success" @click="publishItem(item)">带去发布中心</el-button>
                </el-form-item>
              </el-form>
            </el-tab-pane>
          </el-tabs>
          <el-empty v-else description="生成后会在这里按平台分栏展示" :image-size="80" />
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { http } from '@/utils/request'

const router = useRouter()
const industries = ['通用', '餐饮美食', '美妆个护', '教育培训', '健身运动', '本地生活', '电商零售', '房产家居', '旅行社', '其他']
const form = ref({
  industry: '通用',
  custom_industry: '',
  business_name: '',
  topic: '',
  audience: '',
  specification: '',
  price: '',
  highlights: ''
})
const materials = ref([])
const sourceFile = ref('')
const loading = ref(false)
const packRunning = ref(false)
const packProgress = ref(0)
const coverLoading = ref(false)
const covers = ref([])
const items = ref([])
const activePlatform = ref('小红书')
const tagsText = ref({})
const apiBase = import.meta.env.VITE_API_BASE_URL || 'http://localhost:5409'

function fileUrl(url) {
  return url.startsWith('http') ? url : apiBase.replace('/api', '') + url
}

async function loadMaterials() {
  try {
    const response = await http.get('/getFiles')
    materials.value = response.data || []
  } catch (error) {
    console.error(error)
  }
}

async function generate() {
  if (!form.value.topic && !form.value.business_name) {
    return ElMessage.warning('请填写主题或产品/服务名称')
  }
  if (form.value.industry === '其他' && !form.value.custom_industry.trim()) {
    return ElMessage.warning('请填写自定义行业')
  }
  loading.value = true
  packRunning.value = true
  packProgress.value = 0
  try {
    const start = await http.post('/ai/pack/start', form.value)
    if (start.data.cached) {
      applyPack(start.data.result)
      ElMessage.success('已使用 24 小时内的缓存结果')
      return
    }
    const jobId = start.data.id
    for (let index = 0; index < 150; index++) {
      await new Promise(resolve => setTimeout(resolve, 2000))
      const state = await http.get('/ai/pack/status/' + jobId)
      packProgress.value = state.data.progress || 0
      if (state.data.status === 'success') {
        applyPack(state.data.result)
        return
      }
      if (state.data.status === 'failed') {
        throw new Error(state.data.error || '生成失败')
      }
    }
    throw new Error('生成超时，请稍后重试')
  } catch (error) {
    ElMessage.error(error.message || '生成失败')
  } finally {
    loading.value = false
    packRunning.value = false
  }
}

function applyPack(data) {
  items.value = data.items || []
  tagsText.value = Object.fromEntries(items.value.map(item => [item.platform, (item.tags || []).join('，')]))
  activePlatform.value = '小红书'
  if (data.errors && data.errors.length) {
    ElMessage.warning('部分平台生成失败：' + data.errors.join('；'))
  } else {
    ElMessage.success('6 平台内容已生成')
  }
}

async function makeCovers() {
  coverLoading.value = true
  try {
    const response = await http.post('/media/covers', { file: sourceFile.value })
    covers.value = response.data.covers
    ElMessage.success('封面已生成')
  } catch (error) {
    ElMessage.error(error.message || '封面生成失败')
  } finally {
    coverLoading.value = false
  }
}

function syncTags(item) {
  item.tags = String(tagsText.value[item.platform] || '').split(/[,，\s]+/).map(tag => tag.trim().replace(/^#/, '')).filter(Boolean)
}

async function copyItem(item) {
  syncTags(item)
  const text = [item.title, '', item.body, '', (item.tags || []).map(tag => '#' + tag).join(' ')].join('\n')
  await navigator.clipboard.writeText(text)
  ElMessage.success(item.platform + ' 文案已复制')
}

function publishItem(item) {
  syncTags(item)
  router.push({ path: '/publish-center', query: { title: item.title, body: item.body, tags: (item.tags || []).join(','), platform: item.platform } })
}

onMounted(loadMaterials)
</script>

<style scoped>
.content-pack { padding: 8px 4px; }
.page-head { margin-bottom: 16px; }
.page-head h2 { margin: 0 0 6px; }
.sub { color: #909399; font-size: 13px; margin: 0; }
.cover-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; }
.cover-item { display: flex; flex-direction: column; gap: 6px; align-items: center; font-size: 12px; color: #909399; }
.cover-item img { width: 100%; border-radius: 6px; }
.progress-text { color: #909399; display: block; margin-top: 6px; }
</style>
