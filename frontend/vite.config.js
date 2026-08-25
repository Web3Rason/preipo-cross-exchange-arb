import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: {
    // dev server 用 5173，不要跟後端的 5032 撞（start.bat 會 taskkill 佔用 5032 的程序）
    port: 5173,
    proxy: {
      // 後端跑在 5032（backend/main.py 的 uvicorn.run）
      '/api': 'http://localhost:5032',
      '/ws': { target: 'ws://localhost:5032', ws: true },
    },
  },
})
