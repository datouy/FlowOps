<template>
  <div class="material-management">
    <div class="page-header">
      <h1>素材管理</h1>
    </div>
    
    <div class="material-list-container">
      <div class="material-search">
        <el-input
          v-model="searchKeyword"
          placeholder="输入文件名搜索"
          prefix-icon="Search"
          clearable
          @clear="handleSearch"
          @input="handleSearch"
        />
        <el-input v-model="tagKeyword" placeholder="按标签筛选" clearable style="width:180px" />
        <div class="status-filter">
          <el-radio-group v-model="statusFilter" @change="handleSearch">
            <el-radio-button label="all">全部</el-radio-button>
            <el-radio-button label="published">已发布</el-radio-button>
            <el-radio-button label="unpublished">未发布</el-radio-button>
          </el-radio-group>
        </div>
        <div class="action-buttons">
          <el-button type="primary" @click="handleUploadMaterial">上传素材</el-button>
          <el-button type="warning" @click="openCleanup">清理未使用素材</el-button>
          <el-button type="info" @click="fetchMaterials" :loading="false">
            <el-icon :class="{ 'is-loading': isRefreshing }"><Refresh /></el-icon>
            <span v-if="isRefreshing">刷新中</span>
          </el-button>
        </div>
      </div>
      
      <div v-if="filteredMaterials.length > 0" class="material-list">
        <el-table :data="filteredMaterials" style="width: 100%">
          <el-table-column prop="uuid" label="UUID" width="180" />
          <el-table-column prop="filename" label="文件名" width="300" />
          <el-table-column label="发布状态" width="100">
            <template #default="scope">
              <el-tag size="small" :type="publishedFiles.has(scope.row.filename) ? 'success' : 'info'">
                {{ publishedFiles.has(scope.row.filename) ? '已发布' : '未发布' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="tags" label="标签" width="150" show-overflow-tooltip />
          <el-table-column label="重复" width="80">
            <template #default="scope">
              <el-tag v-if="scope.row.duplicate" size="small" type="warning">重复</el-tag>
              <span v-else>—</span>
            </template>
          </el-table-column>
          <el-table-column prop="filesize" label="文件大小" width="120">
            <template #default="scope">
              {{ scope.row.filesize }} MB
            </template>
          </el-table-column>
          <el-table-column prop="upload_time" label="上传时间" width="180" />
          <el-table-column label="操作">
            <template #default="scope">
              <el-button size="small" @click="handlePreview(scope.row)">预览</el-button>
              <el-button size="small" @click="openTags(scope.row)">标签</el-button>
              <el-button size="small" type="danger" @click="handleDelete(scope.row)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
      </div>
      
      <div v-else class="empty-data">
        <el-empty description="暂无素材数据" />
      </div>
    </div>
    
    <!-- 上传对话框 -->
    <el-dialog
      v-model="uploadDialogVisible"
      title="上传素材"
      width="40%"
      @close="handleUploadDialogClose"
    >
      <div class="upload-form">
        <el-form label-width="80px">
          <el-form-item label="文件名称:">
            <el-input
              v-model="customFilename"
              placeholder="选填 (仅单个文件时生效)"
              :disabled="customFilenameDisabled"
              clearable
            />
          </el-form-item>
          <el-form-item label="选择文件">
            <el-upload
              class="upload-demo"
              drag
              multiple
              :auto-upload="false"
              :on-change="handleFileChange"
              :on-remove="handleFileRemove"
              :file-list="fileList"
            >
              <el-icon class="el-icon--upload"><Upload /></el-icon>
              <div class="el-upload__text">
                将文件拖到此处，或<em>点击上传</em>
              </div>
              <template #tip>
                <div class="el-upload__tip">
                  支持视频、图片等格式文件，可一次选择多个文件
                </div>
              </template>
            </el-upload>
          </el-form-item>
          <el-form-item label="上传列表" v-if="fileList.length > 0">
            <div class="upload-file-list">
              <div v-for="file in fileList" :key="file.uid" class="upload-file-item">
                <span class="file-name">{{ file.name }}</span>
                <el-progress
                  :percentage="uploadProgress[file.uid]?.percentage || 0"
                  :text-inside="true"
                  :stroke-width="20"
                  style="width: 100%; margin-top: 5px;"
                >
                  <span>{{ uploadProgress[file.uid]?.speed || '' }}</span>
                </el-progress>
              </div>
            </div>
          </el-form-item>
        </el-form>
      </div>
      <template #footer>
        <div class="dialog-footer">
          <el-button @click="uploadDialogVisible = false">取消</el-button>
          <el-button type="primary" @click="submitUpload" :loading="isUploading">
            {{ isUploading ? '上传中' : '确认上传' }}
          </el-button>
        </div>
      </template>
    </el-dialog>
    
    <!-- 预览对话框 -->
    <el-dialog
      v-model="previewDialogVisible"
      title="素材预览"
      width="50%"
      :top="'10vh'"
    >
      <div class="preview-container" v-if="currentMaterial">
        <div v-if="isVideoFile(currentMaterial.filename)" class="video-preview">
          <video controls style="max-width: 100%; max-height: 60vh;">
            <source :src="getPreviewUrl(currentMaterial.file_path)" type="video/mp4">
            您的浏览器不支持视频播放
          </video>
        </div>
        <div v-else-if="isImageFile(currentMaterial.filename)" class="image-preview">
          <img :src="getPreviewUrl(currentMaterial.file_path)" style="max-width: 100%; max-height: 60vh;" />
        </div>
        <div v-else class="file-info">
          <p>文件名: {{ currentMaterial.filename }}</p>
          <p>文件大小: {{ currentMaterial.filesize }} MB</p>
          <p>上传时间: {{ currentMaterial.upload_time }}</p>
          <el-button type="primary" @click="downloadFile(currentMaterial)">下载文件</el-button>
        </div>
      </div>
    </el-dialog>
    <el-dialog v-model="tagDialog" title="编辑素材标签" width="420px">
      <el-input v-model="tagText" placeholder="多个标签用逗号分隔，例如：桂林,秋季,纯玩" />
      <template #footer>
        <el-button @click="tagDialog=false">取消</el-button>
        <el-button type="primary" @click="saveTags">保存</el-button>
      </template>
    </el-dialog>
    <el-dialog v-model="cleanupDialog" title="清理未使用素材" width="620px">
      <div class="cleanup-head">
        <span>清理超过</span>
        <el-input-number v-model="cleanupDays" :min="0" :max="365" @change="loadUnused" />
        <span>天且没有被发布记录使用的素材</span>
        <el-button size="small" @click="loadUnused" :loading="cleanupLoading">刷新</el-button>
      </div>
      <p class="cleanup-total">共 {{ unusedItems.length }} 个文件，合计 {{ formatSize(unusedTotal) }}</p>
      <el-table :data="unusedItems" size="small" max-height="320">
        <el-table-column prop="filename" label="文件" min-width="180" show-overflow-tooltip />
        <el-table-column prop="upload_time" label="上传时间" width="150" />
        <el-table-column label="大小" width="90"><template #default="s">{{ formatSize(s.row.size) }}</template></el-table-column>
      </el-table>
      <template #footer>
        <el-button @click="cleanupDialog=false">取消</el-button>
        <el-button type="danger" :disabled="!unusedItems.length" @click="doCleanup">确认清理</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, watch } from 'vue'
import { Refresh, Upload } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { materialApi } from '@/api/material'
import { useAppStore } from '@/stores/app'
import { http } from '@/utils/request'

// 获取应用状态管理
const appStore = useAppStore()

// 搜索和状态控制
const searchKeyword = ref('')
const tagKeyword = ref('')
const statusFilter = ref('all')
const tagDialog = ref(false)
const tagText = ref('')
const currentMaterialId = ref(null)
const cleanupDialog = ref(false)
const cleanupDays = ref(30)
const cleanupLoading = ref(false)
const unusedItems = ref([])
const unusedTotal = ref(0)
const publishedFiles = ref(new Set())
async function loadPublished() {
  try {
    const r = await http.get('/material-status')
    publishedFiles.value = new Set(r.data)
  } catch(e) { console.error(e) }
}
const isRefreshing = ref(false)
const isUploading = ref(false)

// 对话框控制
const uploadDialogVisible = ref(false)
const previewDialogVisible = ref(false)
const currentMaterial = ref(null)

// 文件上传
const fileList = ref([])
const customFilename = ref('')
const customFilenameDisabled = computed(() => fileList.value.length > 1)
const uploadProgress = ref({}); // { [uid]: { percentage: 0, speed: '' } }


watch(fileList, (newList) => {
  if (newList.length <= 1) {
    // If you want to clear the custom name when going back to single file, uncomment below
    // customFilename.value = ''
  }
});


// 获取素材列表
const fetchMaterials = async () => {
  isRefreshing.value = true
  try {
    const response = await materialApi.getAllMaterials()
    
    if (response.code === 200) {
      appStore.setMaterials(response.data)
      ElMessage.success('刷新成功')
    } else {
      ElMessage.error('获取素材列表失败')
    }
  } catch (error) {
    console.error('获取素材列表出错:', error)
    ElMessage.error('获取素材列表失败')
  } finally {
    isRefreshing.value = false
  }
}

// 过滤素材
const filteredMaterials = computed(() => {
  let list = appStore.materials
  if (searchKeyword.value) {
    const keyword = searchKeyword.value.toLowerCase()
    list = list.filter(material => material.filename.toLowerCase().includes(keyword))
  }
  if (tagKeyword.value) {
    const tag = tagKeyword.value.toLowerCase()
    list = list.filter(material => String(material.tags || '').toLowerCase().includes(tag))
  }
  if (statusFilter.value === 'published') {
    list = list.filter(material => publishedFiles.value.has(material.filename))
  } else if (statusFilter.value === 'unpublished') {
    list = list.filter(material => !publishedFiles.value.has(material.filename))
  }
  return list
})

function openTags(row) {
  currentMaterialId.value = row.id
  tagText.value = row.tags || ''
  tagDialog.value = true
}

async function saveTags() {
  await http.put('/materials/tags', { id: currentMaterialId.value, tags: tagText.value })
  tagDialog.value = false
  ElMessage.success('标签已保存')
  fetchMaterials()
  loadPublished()
}

function formatSize(bytes) {
  const n = Number(bytes || 0)
  if (n >= 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + ' MB'
  if (n >= 1024) return (n / 1024).toFixed(1) + ' KB'
  return n + ' B'
}

async function openCleanup() {
  cleanupDialog.value = true
  await loadUnused()
}

async function loadUnused() {
  cleanupLoading.value = true
  try {
    const r = await http.get('/materials/unused', { days: cleanupDays.value })
    unusedItems.value = Array.isArray(r.data) ? r.data : []
    unusedTotal.value = r.total_size || 0
  } catch (e) { ElMessage.error('加载未使用素材失败') }
  finally { cleanupLoading.value = false }
}

async function doCleanup() {
  if (!unusedItems.value.length) return
  try {
    await ElMessageBox.confirm('确认删除这些未使用素材？删除后不可恢复。', '确认清理', { type: 'warning' })
    const r = await http.post('/materials/cleanup', { days: cleanupDays.value, ids: unusedItems.value.map(i => i.id) })
    ElMessage.success('已清理 ' + (r.data.removed || []).length + ' 个文件')
    cleanupDialog.value = false
    fetchMaterials()
    loadPublished()
  } catch (e) {
    if (e !== 'cancel') ElMessage.error(e.message || '清理失败')
  }
}

// 搜索处理
const handleSearch = () => {
  // 搜索逻辑已通过计算属性实现
}

// 上传素材
const handleUploadMaterial = () => {
  // 清空变量
  fileList.value = []
  customFilename.value = ''
  uploadProgress.value = {};
  uploadDialogVisible.value = true
}

// 关闭上传对话框时清空变量
const handleUploadDialogClose = () => {
  fileList.value = []
  customFilename.value = ''
  uploadProgress.value = {};
}

// 文件选择变更
const handleFileChange = (file, uploadFileList) => {
  fileList.value = uploadFileList;
  const newProgress = {};
  for (const f of uploadFileList) {
    newProgress[f.uid] = { percentage: 0, speed: '' };
  }
  uploadProgress.value = newProgress;
}

const handleFileRemove = (file, uploadFileList) => {
  fileList.value = uploadFileList;
  const newProgress = { ...uploadProgress.value };
  delete newProgress[file.uid];
  uploadProgress.value = newProgress;
}

// 提交上传
const submitUpload = async () => {
  if (fileList.value.length === 0) {
    ElMessage.warning('请选择要上传的文件')
    return
  }
  
  isUploading.value = true
  
  for (const file of fileList.value) {
    try {
      // 确保文件对象存在
      if (!file || !file.raw) {
        ElMessage.warning(`文件 ${file.name} 对象无效，已跳过`)
        continue
      }
      
      const formData = new FormData()
      formData.append('file', file.raw)
      
      // 只有当只有一个文件时，自定义文件名才生效
      if (fileList.value.length === 1 && customFilename.value.trim()) {
        formData.append('filename', customFilename.value.trim())
      }
      
      let lastLoaded = 0;
      let lastTime = Date.now();

      const response = await materialApi.uploadMaterial(formData, (progressEvent) => {
        if (response.data && response.data.duplicate) {
          ElMessage.warning('检测到重复素材：' + file.name)
        }
        const progressData = uploadProgress.value[file.uid];
        if (!progressData) return;

        const progress = Math.round((progressEvent.loaded * 100) / progressEvent.total)
        progressData.percentage = progress;

        const currentTime = Date.now();
        const timeDiff = (currentTime - lastTime) / 1000; // in seconds
        const loadedDiff = progressEvent.loaded - lastLoaded;

        if (timeDiff > 0.5) { // Update speed every 0.5 seconds
          const speed = loadedDiff / timeDiff; // bytes per second
          if (speed > 1024 * 1024) {
            progressData.speed = (speed / (1024 * 1024)).toFixed(2) + ' MB/s';
          } else {
            progressData.speed = (speed / 1024).toFixed(2) + ' KB/s';
          }
          lastLoaded = progressEvent.loaded;
          lastTime = currentTime;
        }
      })
      
      if (response.code === 200) {
        ElMessage.success(`文件 ${file.name} 上传成功`)
        const progressData = uploadProgress.value[file.uid];
        if(progressData) progressData.speed = '完成';
      } else {
        ElMessage.error(`文件 ${file.name} 上传失败: ${response.msg || '未知错误'}`)
      }
    } catch (error) {
      console.error(`上传文件 ${file.name} 出错:`, error)
      ElMessage.error(`文件 ${file.name} 上传失败: ${error.message || '未知错误'}`)
    }
  }
  
  isUploading.value = false
  // Keep dialog open to show results
  // uploadDialogVisible.value = false 
  await fetchMaterials()
}

// 预览素材
const handlePreview = async (material) => {
  currentMaterial.value = null
  previewDialogVisible.value = true
  ElMessage.info('加载中...')
  try {
    // 等待一小段时间以确保对话框已打开
    await new Promise(resolve => setTimeout(resolve, 100))
    currentMaterial.value = material
  } catch (error) {
    console.error('预览素材出错:', error)
    ElMessage.error('预览加载失败')
    previewDialogVisible.value = false
  }
}

// 删除素材
const handleDelete = (material) => {
  ElMessageBox.confirm(
    `确定要删除素材 ${material.filename} 吗？`,
    '警告',
    {
      confirmButtonText: '确定',
      cancelButtonText: '取消',
      type: 'warning',
    }
  )
    .then(async () => {
      try {
        const response = await materialApi.deleteMaterial(material.id)
        
        if (response.code === 200) {
          appStore.removeMaterial(material.id)
          ElMessage.success('删除成功')
        } else {
          ElMessage.error(response.msg || '删除失败')
        }
      } catch (error) {
        console.error('删除素材出错:', error)
        ElMessage.error('删除失败')
      }
    })
    .catch(() => {
      // 取消删除
    })
}

// 获取预览URL
const getPreviewUrl = (filePath) => {
  const filename = filePath.split('/').pop()
  return materialApi.getMaterialPreviewUrl(filename)
}

// 下载文件
const downloadFile = (material) => {
  const url = materialApi.downloadMaterial(material.file_path)
  window.open(url, '_blank')
}

// 判断文件类型
const isVideoFile = (filename) => {
  const videoExtensions = ['.mp4', '.avi', '.mov', '.wmv', '.flv', '.mkv']
  return videoExtensions.some(ext => filename.toLowerCase().endsWith(ext))
}

const isImageFile = (filename) => {
  const imageExtensions = ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp']
  return imageExtensions.some(ext => filename.toLowerCase().endsWith(ext))
}

// 组件挂载时获取素材列表

// 组件挂载时获取素材列表与发布状态
onMounted(() => {
  fetchMaterials()
  loadPublished()
})
</script>

<style scoped>
.material-management {
  padding: 8px 4px;
}

.page-header {
  margin-bottom: 18px;
}

.material-search {
  display: flex;
  align-items: center;
  gap: 14px;
  margin-bottom: 16px;
}

.status-filter {
  display: flex;
  align-items: center;
}

.material-search .el-input {
  width: 320px;
}

.action-buttons {
  display: flex;
  gap: 10px;
}

.material-list-container {
  background: #fff;
  padding: 16px;
  border-radius: 8px;
}

.empty-data {
  padding: 40px 0;
}

.upload-file-list {
  width: 100%;
}

.upload-file-item {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 6px;
}

.file-name {
  min-width: 160px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.cleanup-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 10px; }
.cleanup-total { color: #909399; margin: 6px 0 10px; }
</style>
