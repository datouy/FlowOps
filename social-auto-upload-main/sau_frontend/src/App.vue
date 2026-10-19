<template>
  <div class="main-layout">
    <el-container>
      <el-aside :width="isCollapse ? '64px' : '220px'" class="sidebar">
        <div class="logo-container">
          <h2 v-if="!isCollapse">汇流运营台</h2>
          <el-icon class="toggle-sidebar" @click="toggleSidebar"><Fold /></el-icon>
        </div>
        <el-menu
          :default-active="route.path"
          class="sidebar-menu"
          :router="true"
          :collapse="isCollapse"
        >
          <el-menu-item index="/">
            <el-icon><HomeFilled /></el-icon>
            <span>首页</span>
          </el-menu-item>
          <el-menu-item index="/todo">
            <el-icon><Bell /></el-icon>
            <span>今日待办</span>
          </el-menu-item>
          <el-menu-item index="/messages">
            <el-icon><ChatDotRound /></el-icon>
            <span>私信中心</span>
          </el-menu-item>
          <el-menu-item index="/account-management">
            <el-icon><User /></el-icon>
            <span>账号管理</span>
          </el-menu-item>
          <el-menu-item index="/material-management">
            <el-icon><Picture /></el-icon>
            <span>素材管理</span>
          </el-menu-item>
          <el-menu-item index="/publish-center">
            <el-icon><Upload /></el-icon>
            <span>发布中心</span>
          </el-menu-item>
          <el-menu-item index="/ai-content">
            <el-icon><MagicStick /></el-icon>
            <span>AI 文案</span>
          </el-menu-item>
          <el-menu-item index="/content-pack">
            <el-icon><Files /></el-icon>
            <span>内容包</span>
          </el-menu-item>
          <el-menu-item index="/review">
            <el-icon><Checked /></el-icon>
            <span>审核中心</span>
          </el-menu-item>
          <el-menu-item index="/weekly-report">
            <el-icon><DataLine /></el-icon>
            <span>运营周报</span>
          </el-menu-item>
          <el-menu-item index="/publish-records">
            <el-icon><List /></el-icon>
            <span>发布记录</span>
          </el-menu-item>
          <el-menu-item index="/publish-calendar">
            <el-icon><Calendar /></el-icon>
            <span>发布日历</span>
          </el-menu-item>
          <el-menu-item index="/about">
            <el-icon><DataAnalysis /></el-icon>
            <span>关于</span>
          </el-menu-item>
        </el-menu>
      </el-aside>
      <el-container>
        <el-main class="main-content">
          <router-view />
        </el-main>
      </el-container>
    </el-container>
  </div>
</template>

<script setup>
import { ref, computed } from 'vue'
import { useRoute } from 'vue-router'
import {
  HomeFilled, User, DataAnalysis, List, Calendar, MagicStick, Bell, ChatDotRound, Files, Checked, DataLine,
  Fold, Picture, Upload
} from '@element-plus/icons-vue'

const route = useRoute()
const isCollapse = ref(false)

const toggleSidebar = () => {
  isCollapse.value = !isCollapse.value
}
</script>

<style>
* {
  margin: 0;
  padding: 0;
  box-sizing: border-box;
}

html, body, #app {
  height: 100%;
  font-family: "Helvetica Neue", Helvetica, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", Arial, sans-serif;
}
</style>

<style scoped>
.main-layout {
  height: 100%;
}

.sidebar {
  background-color: #304156;
  transition: width 0.3s;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}

.logo-container {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 16px;
  color: #fff;
}

.logo-container h2 {
  font-size: 18px;
  white-space: nowrap;
}

.toggle-sidebar {
  cursor: pointer;
  font-size: 18px;
}

.sidebar-menu {
  border-right: none;
  flex: 1;
}

.main-content {
  background-color: #f0f2f5;
  padding: 20px;
  overflow-y: auto;
}
</style>
