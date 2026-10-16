<!-- src/pages/P9_UserCenter.vue -->
<script setup lang="ts">
import { userError } from '@/utils/userLanguage'
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { ApiRequestError, request } from '@/api/http'
import { ENDPOINT_USER_EXPORT } from '@/api/endpoints'
import type { ProblemDetail } from '@/types/domain'

const router = useRouter()
const authStore = useAuthStore()

const profile = computed(() => authStore.me)
const isAdmin = computed(() => authStore.isAdmin())
const avatarLetter = computed(() => profile.value?.display_name.charAt(0).toUpperCase() ?? '?')

// ---------- 用量六池（真端点 GET /usage；patch-C §11.7） ----------
interface UsagePool {
  pool: string
  bytes: number
  file_count: number
}
interface UsageData {
  total_bytes: number
  limit_bytes: number
  pools: UsagePool[]
}
const usage = ref<UsageData | null>(null)
const usageError = ref<string | null>(null)
const loadingUsage = ref(true)

const POOL_LABELS: Record<string, string> = {
  assets: '原图资产',
  training_images: '训练图集',
  style_samples: '风格样本',
  outputs: '渲染产出',
  backups: '备份',
  models: '模型',
}

function formatBytes(bytes: number): string {
  if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(2)} GB`
  if (bytes >= 1024 ** 2) return `${(bytes / 1024 ** 2).toFixed(1)} MB`
  return `${(bytes / 1024).toFixed(0)} KB`
}
const usedPct = computed(() => {
  if (!usage.value || !usage.value.limit_bytes) return 0
  return Math.min(100, Math.round((usage.value.total_bytes / usage.value.limit_bytes) * 100))
})

onMounted(() => {
  void (async () => {
    try {
      usage.value = await request<UsageData>('/api/v1/usage')
    } catch (e) {
      usageError.value = e instanceof Error && e.message ? e.message : '用量数据加载失败'
    } finally {
      loadingUsage.value = false
    }
  })()
})

// 导出（B1-04 解灰 D-T10-6a）：POST /users/me/export 返回 ZIP（assets/jobs/outputs）。
// 小文件 blob 直下；大文件经 res.body 分块流式读取（浏览器边收边攒，URL 流式消费纪律）。
// 防连击：exporting 即真实请求进行中状态（disabled + 文案），失败走 RFC 9457 解析展示。
const exporting = ref(false)
const exportError = ref<string | null>(null)
const EXPORT_BLOB_LIMIT_BYTES = 64 * 1024 * 1024

async function downloadExportZip(res: Response): Promise<void> {
  const lenHeader = res.headers.get('content-length')
  const total = lenHeader !== null ? Number(lenHeader) : Number.NaN
  let blob: Blob
  if (Number.isFinite(total) && total > EXPORT_BLOB_LIMIT_BYTES) {
    const parts: Uint8Array[] = []
    const reader = res.body?.getReader()
    if (reader) {
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        if (value) parts.push(value)
      }
    }
    blob = new Blob(parts, { type: 'application/zip' })
  } else {
    blob = await res.blob()
  }
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `yy-export-${new Date().toISOString().slice(0, 10)}.zip`
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}

async function handleExport(): Promise<void> {
  if (exporting.value) return
  exporting.value = true
  exportError.value = null
  try {
    const res = await fetch(ENDPOINT_USER_EXPORT.path, {
      method: ENDPOINT_USER_EXPORT.method,
      headers: { 'X-Requested-With': 'XMLHttpRequest', Accept: 'application/zip, application/json' },
      credentials: 'same-origin',
    })
    if (!res.ok) {
      const problem = (await res.json().catch(() => null)) as Partial<ProblemDetail> | null
      throw new ApiRequestError(
        res.status,
        problem?.code ?? 'UNKNOWN',
        problem?.retryable ?? false,
        problem?.detail ?? `导出失败 ${res.status}`,
      )
    }
    await downloadExportZip(res)
  } catch (e) {
    // RFC 9457 解析惯例（B1-03 P6 同款）：ApiRequestError 展示 code: detail
    if (e instanceof ApiRequestError) {
      exportError.value = userError(e)
    } else {
      exportError.value = e instanceof Error && e.message ? e.message : '导出失败，请稍后重试'
    }
  } finally {
    exporting.value = false
  }
}

async function handleLogout(): Promise<void> {
  await authStore.logout()
  await router.replace('/login')
}
</script>

<template>
  <main class="bg-blueprint flex h-[100dvh] flex-col overflow-hidden text-ink select-none">
    <h1 class="sr-only">个人账户中心</h1>

    <header class="safe-t z-20 flex h-14 shrink-0 items-center gap-2 border-b border-ink/15 px-3">
      <RouterLink
        to="/"
        aria-label="返回首页"
        title="返回首页"
        class="flex h-9 shrink-0 items-center gap-1.5 rounded-xl border-2 border-ink bg-surface px-2 text-[11px] font-black shadow-[2px_2px_0_var(--color-ink)] transition-all hover:-translate-y-0.5 hover:bg-sky active:translate-y-0 active:shadow-none"
      >
        <svg aria-hidden="true" class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">
          <path d="m3 10 9-7 9 7"></path>
          <path d="M5 9v11h14V9"></path>
          <path d="M9 20v-6h6v6"></path>
        </svg>
        <span>首页</span>
      </RouterLink>
      <h2 class="min-w-0 flex-1 text-center text-lg font-black tracking-tight">个人账户中心</h2>
      <span class="flex shrink-0 items-center gap-1.5 rounded-full border-2 border-ink bg-mint px-2.5 py-1 text-[10px] font-black">
        <span aria-hidden="true" class="h-1.5 w-1.5 animate-pulse rounded-full bg-ink motion-reduce:animate-none"></span>
        网络同步正常
      </span>
    </header>

    <section class="flex-1 space-y-4 overflow-y-auto overflow-x-hidden px-4 pb-10 pt-4">
      <!-- Profile Banner -->
      <div class="motion-rise relative">
        <div aria-hidden="true" class="absolute inset-0 translate-x-1 translate-y-1 rounded-2xl bg-ink"></div>
        <div class="relative flex items-center gap-4 rounded-2xl border-2 border-ink bg-surface p-5">
          <div
            aria-hidden="true"
            class="flex h-14 w-14 shrink-0 items-center justify-center rounded-full border-2 border-ink bg-primary text-xl font-black text-white"
          >
            {{ avatarLetter }}
          </div>
          <div class="min-w-0">
            <p class="truncate text-lg font-black leading-tight">{{ profile?.display_name ?? '未登录' }}</p>
            <p class="mt-0.5 truncate font-mono text-xs font-bold text-ink-muted">@{{ profile?.username ?? '-' }}</p>
            <span
              class="mt-1.5 inline-block rounded-md border px-2 py-0.5 text-[10px] font-black"
              :class="isAdmin ? 'border-ink bg-amber text-ink' : 'border-ink/20 bg-gray-100 text-ink-muted'"
            >
              {{ isAdmin ? '系统超级管理员' : '创作者账号' }}
            </span>
          </div>
        </div>
      </div>

      <!-- 用量六池（GET /usage） -->
      <div class="motion-rise rounded-2xl border-2 border-ink bg-surface p-4 shadow-[2px_2px_0_var(--color-ink)]" style="animation-delay:40ms">
        <div class="flex items-center justify-between">
          <p class="text-sm font-black">存储用量</p>
          <span v-if="usage" class="font-mono text-[10px] font-bold text-ink-muted">
            {{ formatBytes(usage.total_bytes) }} / {{ formatBytes(usage.limit_bytes) }}
          </span>
        </div>
        <!-- 加载态 -->
        <p v-if="loadingUsage" class="mt-3 text-xs font-bold text-ink-muted" role="status">用量加载中…</p>
        <!-- 错误态 -->
        <div v-else-if="usageError" class="mt-3 flex items-center justify-between gap-2 rounded-xl border-2 border-coral bg-blush px-3 py-2" role="alert">
          <p class="text-xs font-black text-danger">{{ usageError }}</p>
        </div>
        <template v-else-if="usage">
          <div class="mt-3 h-2.5 w-full overflow-hidden rounded-full border-2 border-ink bg-gray-200" role="progressbar" :aria-valuenow="usedPct" aria-valuemin="0" aria-valuemax="100" aria-label="存储总用量">
            <div class="h-full rounded-full bg-primary transition-all duration-500" :style="{ width: `${usedPct}%` }"></div>
          </div>
          <div class="mt-3 grid grid-cols-3 gap-2">
            <div
              v-for="pool in usage.pools"
              :key="pool.pool"
              class="rounded-xl border border-ink/15 bg-canvas p-2.5 text-center"
            >
              <p class="font-mono text-[9px] uppercase tracking-widest text-ink-muted">{{ POOL_LABELS[pool.pool] ?? pool.pool }}</p>
              <p class="mt-1 font-mono text-xs font-black">{{ formatBytes(pool.bytes) }}</p>
              <p class="font-mono text-[9px] font-bold text-ink-muted">{{ pool.file_count }} 文件</p>
            </div>
          </div>
        </template>
      </div>

      <!-- 设置菜单 -->
      <div class="motion-rise divide-y-2 divide-ink overflow-hidden rounded-2xl border-2 border-ink bg-surface" style="animation-delay:60ms">
        <!-- 改密 -->
        <RouterLink
          to="/change-password"
          class="flex min-h-[56px] items-center justify-between p-4 transition-colors hover:bg-sky/40"
        >
          <div class="flex items-center gap-3">
            <span aria-hidden="true" class="flex h-9 w-9 items-center justify-center rounded-xl border-2 border-ink bg-sky">
              <svg class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round">
                <rect x="4" y="10" width="16" height="11" rx="2"></rect>
                <path d="M8 10V7a4 4 0 0 1 8 0v3"></path>
              </svg>
            </span>
            <div>
              <p class="text-sm font-black">修改登录密码</p>
              <p class="text-[11px] font-bold text-ink-muted">安全中心 / 强度实时校验</p>
            </div>
          </div>
          <svg aria-hidden="true" class="h-4 w-4 text-ink-muted" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="m9 18 6-6-6-6"></path></svg>
        </RouterLink>

        <!-- 数据导出（B1-04 解灰：真端点 POST /users/me/export，ZIP 下载） -->
        <div class="flex min-h-[56px] items-center justify-between gap-3 p-4">
          <div class="flex items-center gap-3">
            <span aria-hidden="true" class="flex h-9 w-9 items-center justify-center rounded-xl border-2 border-ink bg-lilac">
              <svg class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round">
                <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
                <polyline points="7 10 12 15 17 10"></polyline>
                <line x1="12" y1="15" x2="12" y2="3"></line>
              </svg>
            </span>
            <div>
              <p class="text-sm font-black">导出我的全部作品数据</p>
              <p class="text-[11px] font-bold text-ink-muted">打包 assets / jobs / outputs（ZIP）</p>
              <p v-if="exportError" class="mt-1 text-xs font-black text-danger" role="alert">{{ exportError }}</p>
            </div>
          </div>
          <button
            type="button"
            class="btn-hard btn-hard--compact shrink-0"
            :disabled="exporting"
            :aria-busy="exporting"
            @click="handleExport"
          >
            {{ exporting ? '打包中…' : '导出' }}
          </button>
        </div>

        <!-- 管理员特权：普通创作者彻底不可见 -->
        <RouterLink
          v-if="isAdmin"
          to="/admin/backup"
          class="flex min-h-[56px] items-center justify-between bg-amber/10 p-4 transition-colors hover:bg-amber/20"
        >
          <div class="flex items-center gap-3">
            <span aria-hidden="true" class="flex h-9 w-9 items-center justify-center rounded-xl border-2 border-ink bg-amber">
              <svg class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round">
                <ellipse cx="12" cy="5" rx="9" ry="3"></ellipse>
                <path d="M3 5v14a9 3 0 0 0 18 0V5"></path>
                <path d="M3 12a9 3 0 0 0 18 0"></path>
              </svg>
            </span>
            <div>
              <p class="text-sm font-black">备份与 Worker 监控</p>
              <p class="text-[11px] font-bold text-ink-muted">管理员特权运维入口</p>
            </div>
          </div>
          <svg aria-hidden="true" class="h-4 w-4 text-ink-muted" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="m9 18 6-6-6-6"></path></svg>
        </RouterLink>
      </div>
    </section>

    <!-- 退出登录 -->
    <div class="px-4">
      <button
        type="button"
        class="btn-hard btn-hard--danger mt-2 w-full !min-h-[52px]"
        @click="handleLogout"
      >
        <svg aria-hidden="true" class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"></path>
          <polyline points="16 17 21 12 16 7"></polyline>
          <line x1="21" y1="12" x2="9" y2="12"></line>
        </svg>
        退出登录
      </button>
    </div>
  </main>
</template>

<!-- 验收: usage六池真数据(总条+分池); 导出真端点ZIP下载+防连击+RFC9457错误(B1-04/D-T10-6a); 备份入口普通用户不可见; 退出醒目; 菜单热区充足; admin角色徽章 -->
