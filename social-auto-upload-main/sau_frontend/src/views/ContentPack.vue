<template>
  <div class="content-pack">
    <div class="page-head">
      <div>
        <h2>多平台内容包</h2>
        <p class="sub">输入一次主题，生成 6 个平台的标题、正文和话题，并自动生成 3:4 / 9:16 封面。</p>
      </div>
    </div>

    <el-row :gutter="18">
      <el-col :xs="24" :lg="9">
        <el-card shadow="never">
          <template #header><span>内容条件</span></template>
          <el-form label-width="80px">
            <el-form-item label="主题">
              <el-input v-model="form.topic" placeholder="例如：桂林秋季纯玩、亲子研学" />
            </el-form-item>
            <el-form-item label="线路名">
              <el-input v-model="form.tour_name" placeholder="选填" />
            </el-form-item>
            <el-form-item label="天数/报价">
              <el-input-number v-model="form.days" :min="1" style="width:110px" />
              <el-input-number v-model="form.price" :min="0" style="width:140px;margin-left:10px" />
            </el-form-item>
            <el-form-item label="亮点">
              <el-input v-model="form.highlights" type="textarea" :rows="3" placeholder="0购物、漓江竹筏、四钻住宿" />
            </el-form-item>
            <el-form-item label="素材">
              <el-select v-model="sourceFile" placeholder="选择一条素材用于生成封面" clearable style="width:100%">
                <el-option v-for="f in materials" :key="f.file_path" :label="f.filename" :value="f.file_path" />
              </el-select>
            </el-form-item>
            <el-form-item>
              <el-button type="primary" :loading="loading" @click="generate" style="width:100%">生成 6 平台内容包</el-button>
            </el-form-item>
            <el-form-item v-if="sourceFile">
              <el-button @click="makeCovers" :loading="coverLoading" style="width:100%">生成 3:4 / 9:16 封面</el-button>
            </el-form-item>
          </el-form>
        </el-card>
        <el-card v-if="covers.length" shadow="never" style="margin-top:14px">
          <template #header><span>自动封面</span></template>
          <div class="cover-grid">
            <div v-for="c in covers" :key="c.name" class="cover-item">
              <img :src="fileUrl(c.url)" :alt="c.name" />
              <span>{{ c.name }}</span>
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
const form = ref({ topic: '', tour_name: '', days: 3, price: 0, highlights: '' })
const materials = ref([])
const sourceFile = ref('')
const loading = ref(false)
const coverLoading = ref(false)
const covers = ref([])
const items = ref([])
const activePlatform = ref('小红书')
const tagsText = ref({})
const apiBase = import.meta.env.VITE_API_BASE_URL || 'http://localhost:5409'

function fileUrl(u) { return u.startsWith('http') ? u : apiBase.replace('/api', '') + u }

async function loadMaterials() {
  try {
    const r = await http.get('/getFiles')
    materials.value = r.data || []
  } catch (e) { console.error(e) }
}

async function generate() {
  if (!form.value.topic && !form.value.tour_name) return ElMessage.warning('请填写主题或线路名')
  loading.value = true
  try {
    const r = await http.post('/ai/generate-pack', form.value)
    items.value = r.data.items
    tagsText.value = Object.fromEntries(r.data.items.map(i => [i.platform, (i.tags || []).join('，')]))
    activePlatform.value = '小红书'
    ElMessage.success('6 平台内容已生成')
  } catch (e) { ElMessage.error(e.message || '生成失败') }
  finally { loading.value = false }
}

async function makeCovers() {
  coverLoading.value = true
  try {
    const r = await http.post('/media/covers', { file: sourceFile.value })
    covers.value = r.data.covers
    ElMessage.success('封面已生成')
  } catch (e) { ElMessage.error(e.message || '封面生成失败') }
  finally { coverLoading.value = false }
}

function syncTags(item) {
  item.tags = String(tagsText.value[item.platform] || '').split(/[,，\s]+/).map(t => t.trim().replace(/^#/, '')).filter(Boolean)
}

async function copyItem(item) {
  syncTags(item)
  const text = [item.title, '', item.body, '', (item.tags || []).map(t => '#' + t).join(' ')].join('\n')
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
</style>
