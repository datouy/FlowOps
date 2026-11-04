import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'
import AutoImport from 'unplugin-auto-import/vite'
import Components from 'unplugin-vue-components/vite'
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers'
import { resolve } from 'path'

// https://vite.dev/config/
function localAccessTokenPlugin(token) {
  return {
    name: 'yunce-local-access-token',
    configureServer(server) {
      server.middlewares.use('/__yunce_local_token', (req, res) => {
        const address = String(req.socket?.remoteAddress || '')
        if (!['127.0.0.1', '::1', '::ffff:127.0.0.1'].includes(address)) {
          res.statusCode = 403
          res.setHeader('Content-Type', 'application/json; charset=utf-8')
          res.end(JSON.stringify({ error: 'loopback only' }))
          return
        }
        res.statusCode = token ? 200 : 503
        res.setHeader('Content-Type', 'application/json; charset=utf-8')
        res.setHeader('Cache-Control', 'no-store')
        res.end(JSON.stringify(token ? { token } : { error: 'not configured' }))
      })
    },
  }
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, resolve(__dirname, '..'), '')
  return {
  plugins: [
    localAccessTokenPlugin(env.YUNCE_ACCESS_TOKEN),
    vue(),
    // Element Plus 按需引入：只打包用到的组件和样式。
    // 之前是 `app.use(ElementPlus)` 全量引入，产物里有 1MB 的
    // element-plus.js 和 390KB 的完整 CSS，用户首屏要全量下载。
    AutoImport({
      resolvers: [ElementPlusResolver()],
      dts: false,
    }),
    Components({
      resolvers: [
        // importStyle: 'sass' —— 用按需 sass，产物体积比全量 css 更小
        ElementPlusResolver({ importStyle: 'sass' }),
      ],
      dts: false,
    }),
  ],
  resolve: {
    alias: {
      '@': resolve(__dirname, 'src'),
    },
  },
  css: {
    preprocessorOptions: {
      scss: {
        // 移除自动导入，改用@use语法
      }
    }
  },
  server: {
    // 局域网同事通过本机 IP 访问，Vite 继续把 API 请求代理到回环地址上的后端。
    host: '0.0.0.0',
    port: 5173,
    open: true,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:5409',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, '')
      }
    }
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    chunkSizeWarningLimit: 1600,
    rollupOptions: {
      output: {
        manualChunks: {
          vue: ['vue', 'vue-router', 'pinia'],
          utils: ['axios']
        }
      }
    }
  }
  }
})
