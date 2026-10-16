<!-- src/pages/P0_Home.vue -->
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { http } from '@/api/http'

const router = useRouter()
const authStore = useAuthStore()

const loading = ref(false)
const errorMessage = ref<string | null>(null)
const reviewCount = ref<number | null>(null)
const trainingCount = ref<number | null>(null)

const profile = computed(() => authStore.me)
const avatarLetter = computed(() => profile.value?.display_name.charAt(0).toUpperCase() ?? '?')
const isAdmin = computed(() => authStore.isAdmin())

// 真数据源：列表端点 meta.total（raw 资产 / 启用风格 / 训练图）
const assetTotal = ref<number | null>(null)
const enabledStyleCount = ref<number | null>(null)

// 列表数量来自已发布 API 信封。训练集接口将 total 放在 data，其余列表常用 meta。
async function fetchTotal(path: string): Promise<number> {
  const body = await http.getEnvelope<{ total?: number }>(path)
  const total = body.meta?.total ?? body.data?.total
  if (typeof total !== 'number' || !Number.isFinite(total) || total < 0) {
    throw new Error(`${path} missing total`)
  }
  return total
}

interface ReviewJob { job_id: string; status?: string }
interface ReviewItem { status: string }
const REVIEW_JOB_STATUSES = ['succeeded', 'partial'] as const
const REVIEW_ITEM_STATUSES = new Set(['completed', 'completed_with_warning'])
const REVIEW_PAGE_SIZE = 100
const REVIEW_DETAIL_CONCURRENCY = 8

async function fetchReviewJobs(status: typeof REVIEW_JOB_STATUSES[number]): Promise<ReviewJob[]> {
  const first = await http.getEnvelope<{ items?: ReviewJob[] }>(
    `/api/v1/render-jobs?status=${status}&page=1&page_size=${REVIEW_PAGE_SIZE}`,
  )
  const total = first.meta?.total
  const firstItems = first.data?.items
  if (typeof total !== 'number' || !Number.isSafeInteger(total) || total < 0 || !Array.isArray(firstItems)) {
    throw new Error(`render-jobs ${status} list has invalid pagination data`)
  }
  const jobs = [...firstItems]
  const pages = Math.ceil((total as number) / REVIEW_PAGE_SIZE)
  for (let page = 2; page <= pages; page += 1) {
    const next = await http.getEnvelope<{ items?: ReviewJob[] }>(
      `/api/v1/render-jobs?status=${status}&page=${page}&page_size=${REVIEW_PAGE_SIZE}`,
    )
    if (!Array.isArray(next.data?.items)) throw new Error(`render-jobs ${status} page ${page} is invalid`)
    jobs.push(...next.data.items)
  }
  if (jobs.length !== total) throw new Error(`render-jobs ${status} total changed during review count`)
  return jobs
}

async function fetchPendingReviewCount(): Promise<number> {
  const jobGroups = await Promise.all(REVIEW_JOB_STATUSES.map(fetchReviewJobs))
  const jobs = jobGroups.flat()
  let pending = 0
  for (let offset = 0; offset < jobs.length; offset += REVIEW_DETAIL_CONCURRENCY) {
    const chunk = jobs.slice(offset, offset + REVIEW_DETAIL_CONCURRENCY)
    const details = await Promise.all(chunk.map((job) => {
      if (!job.job_id) throw new Error('render-jobs list item is missing job_id')
      return http.getEnvelope<{ job?: { status?: string }; items?: ReviewItem[] }>(
        `/api/v1/render-jobs/${encodeURIComponent(job.job_id)}`,
      )
    }))
    for (const detail of details) {
      const status = detail.data?.job?.status
      const items = detail.data?.items
      if (!Array.isArray(items)) throw new Error('render job detail is missing items')
      if (status !== 'succeeded' && status !== 'partial') continue
      pending += items.filter((item) => REVIEW_ITEM_STATUSES.has(item.status)).length
    }
  }
  return pending
}

async function loadStats(): Promise<void> {
  const results = await Promise.allSettled([
    fetchTotal('/api/v1/assets?type=cos&page=1&page_size=1'),
    fetchTotal('/api/v1/styles?enabled=true&page=1&page_size=1'),
    fetchPendingReviewCount(),
    fetchTotal('/api/v1/training-sets?page=1&page_size=1'),
  ])
  assetTotal.value = results[0].status === 'fulfilled' ? results[0].value : null
  enabledStyleCount.value = results[1].status === 'fulfilled' ? results[1].value : null
  reviewCount.value = results[2].status === 'fulfilled' ? results[2].value : null
  trainingCount.value = results[3].status === 'fulfilled' ? results[3].value : null
  if (results.every((r) => r.status === 'rejected')) {
    throw new Error('all stats failed')
  }
}

onMounted(() => {
  loading.value = true
  loadStats()
    .catch(() => {
      // PRD §5.0：指标拉取失败静默降级 --，不阻塞、不白屏。
      errorMessage.value = '待办数据暂时不可用，已安全降级'
      reviewCount.value = null
      trainingCount.value = null
    })
    .finally(() => {
      loading.value = false
    })
})

async function handleLogout(): Promise<void> {
  await authStore.logout()
  await router.replace('/login')
}
</script>

<template>
  <main class="bg-blueprint relative flex h-[100dvh] flex-col overflow-hidden text-ink select-none">
    <h1 class="sr-only">博主工作台</h1>

    <!-- Y2K 悬浮贴纸组（全页唯一无限动效组，reduced-motion 自动静止） -->
    <div aria-hidden="true" class="pointer-events-none absolute right-5 top-20 z-0 hidden">
      <span class="motion-float absolute -right-1 top-0 flex h-11 w-11 items-center justify-center rounded-xl border-2 border-ink bg-amber text-base shadow-hard" style="--float-tilt:-10deg">
        <svg viewBox="0 0 24 24" class="h-5 w-5" fill="currentColor"><path d="m12 2 2.9 6.3L22 9.2l-5 4.9 1.2 7L12 17.8 5.8 21.1 7 14.1l-5-4.9 7.1-.9z"></path></svg>
      </span>
      <span class="motion-float-slow absolute right-12 top-12 rounded-lg border-2 border-ink bg-sky px-2 py-1 font-mono text-[11px] font-black shadow-hard" style="--float-tilt:6deg">&lt;/&gt;</span>
    </div>

    <header class="safe-t z-20 flex h-14 shrink-0 items-center justify-between border-b border-ink/15 px-4">
      <div class="flex items-center gap-2.5">
        <div
          aria-hidden="true"
          class="flex h-10 w-10 items-center justify-center rounded-xl border-2 border-ink bg-sky text-base font-black shadow-[2px_2px_0_var(--color-ink)]"
        >
          {{ avatarLetter }}
        </div>
        <div class="leading-none">
          <p class="text-sm font-black">{{ profile?.display_name ?? '未登录' }}</p>
          <p class="mt-1 font-mono text-[10px] uppercase tracking-widest text-ink-muted">
            {{ isAdmin ? '&lt;admin&gt;' : '&lt;creator&gt;' }}
          </p>
        </div>
      </div>
      <button
        type="button"
        class="flex min-h-[44px] items-center rounded-full border-2 border-ink bg-surface px-4 text-xs font-black shadow-[2px_2px_0_var(--color-ink)] transition-all duration-150 ease-[cubic-bezier(0.22,1,0.36,1)] hover:-translate-y-0.5 hover:bg-ink hover:text-white active:translate-y-0 active:shadow-none"
        @click="handleLogout"
      >
        退出
      </button>
    </header>

    <section class="relative z-10 flex-1 space-y-5 overflow-y-auto overflow-x-hidden p-4 pb-10">
      <!-- Hero -->
      <div class="motion-rise">
        <div class="flex items-center justify-between">
          <p class="font-mono text-[10px] font-black uppercase tracking-[0.3em] text-ink-muted">To-sign Studio / Workspace</p>
          <span class="rounded-md border border-ink/25 bg-surface px-1.5 py-0.5 font-mono text-[10px] font-black text-ink-muted">&lt;date&gt; 2025</span>
        </div>
        <h2 class="mt-3 text-[44px] font-black leading-[0.9] tracking-tighter">
          To-sign<br>
          <span class="relative inline-block">
            <span aria-hidden="true" class="absolute inset-x-[-4px] bottom-1 -z-0 h-3.5 bg-sky"></span>
            <span class="relative">Studio</span>
          </span>
        </h2>
        <p class="mt-3 max-w-[280px] text-xs font-bold leading-relaxed text-ink-muted">
          干净 Cos 原图、专属笔迹与批量 To 签生产的统一工作台。
        </p>
      </div>

      <div
        v-if="errorMessage"
        role="alert"
        class="motion-fade flex items-center justify-between rounded-2xl border-2 border-amber bg-amber/10 px-4 py-3 text-xs font-bold"
      >
        <span>{{ errorMessage }}</span>
        <button type="button" class="shrink-0 font-black underline" @click="errorMessage = null">知道了</button>
      </div>

      <!-- KPI 双层卡 -->
      <div class="motion-rise grid grid-cols-2 gap-3.5" style="animation-delay:40ms">
        <div class="relative">
          <div aria-hidden="true" class="absolute inset-0 translate-x-1 translate-y-1 rounded-2xl bg-ink"></div>
          <div class="relative flex h-full flex-col rounded-2xl border-2 border-ink bg-surface p-4">
            <div class="flex items-center justify-between">
              <p class="font-mono text-[10px] uppercase tracking-widest text-ink-muted">&lt;queue&gt; 待审</p>
              <span aria-hidden="true" class="h-2.5 w-2.5 rounded-full" :class="reviewCount ? 'bg-amber' : 'bg-ink/20'"></span>
            </div>
            <p class="mt-3 font-mono text-4xl font-black leading-none tracking-tighter">
              <template v-if="loading">··</template>
              <template v-else-if="reviewCount === null">--</template>
              <template v-else>{{ String(reviewCount).padStart(2, '0') }}</template>
            </p>
            <p class="mt-2 text-[11px] font-bold text-ink-muted">件等待质检出仓</p>
          </div>
        </div>

        <div class="relative">
          <div aria-hidden="true" class="absolute inset-0 translate-x-1 translate-y-1 rounded-2xl bg-ink-soft"></div>
          <RouterLink
            to="/training"
            aria-label="进入模型训练控制台（含训练图上传）"
            class="group relative flex h-full flex-col rounded-2xl border-2 border-ink bg-sky p-4 transition-all duration-150 ease-[cubic-bezier(0.22,1,0.36,1)] hover:-translate-y-0.5 hover:shadow-[4px_4px_0_var(--color-ink)] active:translate-y-0 active:shadow-none"
          >
            <div class="flex items-center justify-between">
              <p class="font-mono text-[10px] uppercase tracking-widest text-ink-muted">&lt;engine&gt; 训练</p>
              <span aria-hidden="true" class="h-2.5 w-2.5 rounded-full" :class="trainingCount ? 'motion-safe:animate-pulse bg-primary' : 'bg-ink/20'"></span>
            </div>
            <p class="mt-3 font-mono text-4xl font-black leading-none tracking-tighter text-primary">
              <template v-if="loading">··</template>
              <template v-else-if="trainingCount === null">--</template>
              <template v-else>{{ String(trainingCount).padStart(2, '0') }}</template>
            </p>
            <p class="mt-2 text-[11px] font-bold text-ink-muted">个训练图已入库</p>
            <span class="mt-2 flex items-center justify-between border-t border-ink/15 pt-2 text-[10px] font-black text-ink">
              训练图上传 · 控制台
              <svg aria-hidden="true" class="h-3 w-3 transition-transform duration-150 group-hover:translate-x-0.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="m9 18 6-6-6-6"></path></svg>
            </span>
          </RouterLink>
        </div>
      </div>

      <!-- 主入口 -->
      <p class="pt-1 text-sm font-black tracking-tight">开始创作</p>
      <div class="motion-rise grid grid-cols-2 gap-3.5" style="animation-delay:80ms">
        <div class="relative">
          <div aria-hidden="true" class="absolute inset-0 translate-x-1 translate-y-1 rounded-2xl bg-ink"></div>
          <RouterLink
            to="/assets"
            class="group relative flex aspect-[4/3.4] cursor-pointer flex-col justify-between rounded-2xl border-2 border-ink bg-surface p-3.5 transition-all duration-150 ease-[cubic-bezier(0.22,1,0.36,1)] hover:-translate-y-0.5 hover:shadow-[6px_6px_0_var(--color-ink)] active:translate-x-0 active:translate-y-0 active:shadow-none"
          >
            <div class="flex items-start justify-between">
              <span aria-hidden="true" class="flex h-[42px] w-[42px] items-center justify-center rounded-xl border-2 border-ink bg-ink text-white transition-transform duration-150 group-hover:rotate-[-6deg] group-hover:scale-105">
                <svg class="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"></path>
                  <circle cx="12" cy="13" r="4"></circle>
                </svg>
              </span>
              <span class="rounded-md border border-ink/25 bg-canvas px-1.5 py-0.5 font-mono text-[10px] font-black">{{ assetTotal ?? '--' }} raw</span>
            </div>
            <div>
              <h3 class="text-sm font-black">生成底图资产台</h3>
              <p class="mt-0.5 text-[11px] font-bold leading-snug text-ink-muted">生成用无字底图；训练参考图在训练页</p>
            </div>
          </RouterLink>
        </div>

        <div class="relative">
          <div aria-hidden="true" class="absolute inset-0 translate-x-1 translate-y-1 rounded-2xl bg-ink-soft"></div>
          <RouterLink
            to="/batch"
            class="group relative flex aspect-[4/3.4] cursor-pointer flex-col justify-between rounded-2xl border-2 border-ink bg-sky p-3.5 transition-all duration-150 ease-[cubic-bezier(0.22,1,0.36,1)] hover:-translate-y-0.5 hover:shadow-[6px_6px_0_var(--color-ink)] active:translate-x-0 active:translate-y-0 active:shadow-none"
          >
            <div class="flex items-start justify-between">
              <span aria-hidden="true" class="flex h-[42px] w-[42px] items-center justify-center rounded-xl border-2 border-ink bg-primary text-white transition-transform duration-150 group-hover:rotate-[6deg] group-hover:scale-105">
                <svg class="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M12.83 2.18a2 2 0 0 0-1.66 0L2.6 6.08a1 1 0 0 0 0 1.83l8.58 3.91a2 2 0 0 0 1.66 0l8.58-3.9a1 1 0 0 0 0-1.83Z"></path>
                  <path d="m22 17.65-9.17 4.16a2 2 0 0 1-1.66 0L2 17.65"></path>
                  <path d="m22 12.65-9.17 4.16a2 2 0 0 1-1.66 0L2 12.65"></path>
                </svg>
              </span>
              <span class="rounded-md border border-ink/25 bg-surface px-1.5 py-0.5 font-mono text-[10px] font-black">x200</span>
            </div>
            <div>
              <h3 class="text-sm font-black">批量生成中心</h3>
              <p class="mt-0.5 text-[11px] font-bold leading-snug text-ink-muted">粉丝名单一键出签</p>
            </div>
          </RouterLink>
        </div>
      </div>

      <!-- 次级流水线 -->
      <p class="pt-1 text-sm font-black tracking-tight">生产流水线</p>
      <div class="motion-rise space-y-2.5" style="animation-delay:120ms">
        <RouterLink
          to="/styles"
          class="flex items-center justify-between rounded-2xl border-2 border-ink bg-surface p-3.5 shadow-[2px_2px_0_var(--color-ink)] transition-all duration-150 ease-[cubic-bezier(0.22,1,0.36,1)] hover:-translate-y-0.5 hover:bg-sky/40 hover:shadow-[4px_4px_0_var(--color-ink)] active:translate-y-0 active:shadow-none"
        >
          <div class="flex items-center gap-3">
            <span aria-hidden="true" class="flex h-11 w-11 items-center justify-center rounded-xl border-2 border-ink bg-ink-soft text-white">
              <svg class="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="m12 19 7-7 3 3-7 7-3-3z"></path>
                <path d="m18 13-1.5-7.5L2 2l3.5 14.5L13 18l5-5z"></path>
                <path d="m2 2 7.586 7.586"></path>
                <circle cx="11" cy="11" r="2"></circle>
              </svg>
            </span>
            <div>
              <p class="text-sm font-black">风格资产库</p>
              <p class="text-[11px] font-bold text-ink-muted">LoRA 模型与手写笔迹</p>
            </div>
          </div>
          <div class="flex items-center gap-2.5">
            <span class="rounded-full bg-canvas px-2.5 py-1 text-[11px] font-black">{{ enabledStyleCount ?? '--' }} 个启用</span>
            <span aria-hidden="true" class="flex h-9 w-9 items-center justify-center rounded-full border-2 border-ink bg-surface">
              <svg class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="m9 18 6-6-6-6"></path></svg>
            </span>
          </div>
        </RouterLink>

        <RouterLink
          to="/batch"
          class="flex items-center justify-between rounded-2xl border-2 border-ink bg-surface p-3.5 shadow-[2px_2px_0_var(--color-ink)] transition-all duration-150 ease-[cubic-bezier(0.22,1,0.36,1)] hover:-translate-y-0.5 hover:bg-sky/40 hover:shadow-[4px_4px_0_var(--color-ink)] active:translate-y-0 active:shadow-none"
        >
          <div class="flex items-center gap-3">
            <span aria-hidden="true" class="flex h-11 w-11 items-center justify-center rounded-xl border-2 border-ink bg-sky">
              <svg class="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M3 7V5a2 2 0 0 1 2-2h2"></path>
                <path d="M17 3h2a2 2 0 0 1 2 2v2"></path>
                <path d="M21 17v2a2 2 0 0 1-2 2h-2"></path>
                <path d="M7 21H5a2 2 0 0 1-2-2v-2"></path>
                <circle cx="12" cy="12" r="3"></circle>
                <path d="m16 16 2 2"></path>
              </svg>
            </span>
            <div>
              <p class="text-sm font-black">成果沉浸审核</p>
              <p class="text-[11px] font-bold text-ink-muted">逐张大图质检与出仓</p>
            </div>
          </div>
          <div class="flex items-center gap-2.5">
            <span
              class="rounded-full border-2 px-2.5 py-1 text-[11px] font-black"
              :class="reviewCount !== null && reviewCount > 0 ? 'border-ink bg-amber text-ink' : 'border-ink/15 bg-canvas text-ink-muted'"
            >{{ reviewCount === null ? '--' : `${reviewCount} 待审` }}</span>
            <span aria-hidden="true" class="flex h-9 w-9 items-center justify-center rounded-full border-2 border-ink bg-surface">
              <svg class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="m9 18 6-6-6-6"></path></svg>
            </span>
          </div>
        </RouterLink>
      </div>

      <!-- 更多工具（P7/P8/P9 入口） -->
      <p class="pt-1 text-sm font-black tracking-tight">更多工具</p>
      <div class="motion-rise grid grid-cols-3 gap-3" style="animation-delay:160ms">
        <RouterLink
          to="/training"
          class="group flex flex-col items-center gap-2 rounded-2xl border-2 border-ink bg-surface p-3 text-center shadow-[2px_2px_0_var(--color-ink)] transition-all duration-150 ease-[cubic-bezier(0.22,1,0.36,1)] hover:-translate-y-0.5 hover:bg-sky/40 hover:shadow-[4px_4px_0_var(--color-ink)] active:translate-y-0 active:shadow-none"
        >
          <span aria-hidden="true" class="flex h-10 w-10 items-center justify-center rounded-xl border-2 border-ink bg-primary text-white transition-transform duration-150 group-hover:rotate-[-6deg] group-hover:scale-105">
            <svg class="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M22 12h-4l-3 9L9 3l-3 9H2"></path>
            </svg>
          </span>
          <span class="text-xs font-black leading-tight">训练监控</span>
        </RouterLink>

        <RouterLink
          to="/blessings"
          class="group flex flex-col items-center gap-2 rounded-2xl border-2 border-ink bg-surface p-3 text-center shadow-[2px_2px_0_var(--color-ink)] transition-all duration-150 ease-[cubic-bezier(0.22,1,0.36,1)] hover:-translate-y-0.5 hover:bg-sky/40 hover:shadow-[4px_4px_0_var(--color-ink)] active:translate-y-0 active:shadow-none"
        >
          <span aria-hidden="true" class="flex h-10 w-10 items-center justify-center rounded-xl border-2 border-ink bg-amber transition-transform duration-150 group-hover:rotate-[6deg] group-hover:scale-105">
            <svg class="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"></path>
            </svg>
          </span>
          <span class="text-xs font-black leading-tight">模版语库</span>
        </RouterLink>

        <RouterLink
          to="/me"
          class="group flex flex-col items-center gap-2 rounded-2xl border-2 border-ink bg-surface p-3 text-center shadow-[2px_2px_0_var(--color-ink)] transition-all duration-150 ease-[cubic-bezier(0.22,1,0.36,1)] hover:-translate-y-0.5 hover:bg-sky/40 hover:shadow-[4px_4px_0_var(--color-ink)] active:translate-y-0 active:shadow-none"
        >
          <span aria-hidden="true" class="flex h-10 w-10 items-center justify-center rounded-xl border-2 border-ink bg-ink-soft text-white transition-transform duration-150 group-hover:rotate-[-6deg] group-hover:scale-105">
            <svg class="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"></path>
              <circle cx="12" cy="7" r="4"></circle>
            </svg>
          </span>
          <span class="text-xs font-black leading-tight">个人中心</span>
        </RouterLink>
      </div>
      <!-- Admin -->
      <RouterLink
        v-if="isAdmin"
        to="/admin/backup"
        class="flex items-center justify-between rounded-2xl border-2 border-dashed border-ink-soft bg-sky/50 p-3.5 transition-all duration-150 ease-[cubic-bezier(0.22,1,0.36,1)] hover:-translate-y-0.5 hover:shadow-[4px_4px_0_var(--color-ink-soft)] active:translate-y-0 active:shadow-none"
      >
        <div class="flex items-center gap-3">
          <span aria-hidden="true" class="flex h-11 w-11 items-center justify-center rounded-xl border-2 border-ink bg-surface">
            <svg class="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <ellipse cx="12" cy="5" rx="9" ry="3"></ellipse>
              <path d="M3 5v14a9 3 0 0 0 18 0V5"></path>
              <path d="M3 12a9 3 0 0 0 18 0"></path>
            </svg>
          </span>
          <div>
            <p class="text-sm font-black">备份与 Worker 监控</p>
            <p class="text-[11px] font-bold text-ink-muted">冷备份快照与渲染队列遥测</p>
          </div>
        </div>
        <span class="rounded-full border-2 border-ink bg-amber px-2 py-0.5 text-[10px] font-black">仅管理员</span>
      </RouterLink>
    </section>
  </main>
</template>

<!-- 验收: 整卡热区≥44px; admin才渲染备份卡; 指标失败降级--; 蓝图网格+圆润双层卡+浅蓝淡黑+贴纸焦点色; hover/active/入场/悬浮动效且支持reduced-motion -->
