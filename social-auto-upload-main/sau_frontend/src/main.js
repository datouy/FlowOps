import { createApp } from 'vue'
import App from './App.vue'
import router from './router'
import pinia from './stores'
import './styles/index.scss'

const app = createApp(App)

// Element Plus 组件与图标都改为按需引入（见 vite.config.js 的
// ElementPlusResolver）。这里不再做 `app.use(ElementPlus)`，也不再
// `import * as ElementPlusIconsVue` 全量注册图标——
// 那两行会把整个组件库（~1MB JS）和全部 586 个图标（~200KB）打进产物。
//
// 各组件在 <script setup> 里显式 import 自己用到的图标即可，
// 例如：import { Refresh, Upload } from '@element-plus/icons-vue'
//
// 如果某个图标是运行时动态渲染的（:is="变量"），在这里按需补注册。

app.use(router)
app.use(pinia)
app.mount('#app')
