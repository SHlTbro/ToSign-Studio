import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'
export default defineConfig({
 root: fileURLToPath(new URL('.', import.meta.url)),
 cacheDir: fileURLToPath(new URL('../../private-font-cache', import.meta.url)),
 plugins: [vue()],
 resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } },
 test: { environment: 'jsdom', globals: true }
})
