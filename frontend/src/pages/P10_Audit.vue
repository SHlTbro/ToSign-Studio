<!-- src/pages/P10_Audit.vue -->
<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

const route = useRoute()
const router = useRouter()

// 真数据源：GET /audit-logs（T58B 新只读 admin 端点；服务端筛选 + 分页）。
// 铁律不变：筛选条件只序列化为 URL Query 回显给服务端，严禁前端拉全量后过滤。
interface AuditEntry {
  log_id: number
  actor: string
  timestamp: string
  action: string
  object_ref: string
  result: string
  error_code: string | null
}

const entries = ref<AuditEntry[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = 20
const loading = ref(false)
const loadError = ref<string | null>(null)

const drawerOpen = ref(false)
const applying = ref(false)

// job_id/item_id/asset_id 三个输入统一映射为服务端 object_ref 精确过滤
const filterForm = ref({ object_ref: '', action: '', since: '' })

const ACTION_OPTIONS = [
  'asset_approve', 'asset_reject', 'asset_soft_delete', 'asset_hard_delete',
  'template_create', 'template_approved', 'template_rejected',
  'render_job_created', 'draft_job_created', 'render_idempotency_hit',
  'register', 'style_publish',
]

const activeParams = computed<[string, string][]>(() =>
  Object.entries(route.query)
    .filter(([k, v]) => k !== 'page' && typeof v === 'string' && v !== '')
    .map(([k, v]) => [k, v as string]),
)

const totalPages = computed(() => Math.max(1, Math.ceil(total.value / pageSize)))

// request() 拆信封只回 data，分页 total 在 meta 里——这里直接 fetch 手拆信封
async function fetchLogs(): Promise<void> {
  loading.value = true
  loadError.value = null
  try {
    const q = new URLSearchParams()
    const routePage = Number(route.query.page)
    page.value = Number.isFinite(routePage) && routePage >= 1 ? Math.floor(routePage) : 1
    q.set('page', String(page.value))
    q.set('page_size', String(pageSize))
    for (const key of ['object_ref', 'action', 'since'] as const) {
      const v = route.query[key]
      if (typeof v === 'string' && v) q.set(key, v)
    }
    const res = await fetch(`/api/v1/audit-logs?${q.toString()}`, {
      headers: { Accept: 'application/json' },
      credentials: 'same-origin',
    })
    if (!res.ok) {
      const problem = (await res.json().catch(() => null)) as { detail?: string; code?: string } | null
      throw new Error(problem?.detail ?? problem?.code ?? `查询失败 ${res.status}`)
    }
    const body = (await res.json()) as { data: { items: AuditEntry[] }; meta: { total: number } }
    entries.value = body.data.items
    total.value = body.meta.total
  } catch (e) {
    entries.value = []
    total.value = 0
    loadError.value = e instanceof Error && e.message ? e.message : '审计日志加载失败'
  } finally {
    loading.value = false
  }
}

watch(() => route.query, () => void fetchLogs(), { immediate: false })
onMounted(() => {
  void fetchLogs()
})

function applyFilters(): void {
  applying.value = true
  const query: Record<string, string> = {}
  for (const [k, v] of Object.entries(filterForm.value)) {
    const t = v.trim()
    if (t) query[k] = t
  }
  // 条件仅映射 URL，页面不做内存筛选；watch 触发真服务端查询
  applying.value = false
  drawerOpen.value = false
  void router.replace({ path: '/audit', query })
}

function removeParam(key: string): void {
  const query = { ...route.query }
  delete query[key]
  void router.replace({ path: '/audit', query })
}

function clearAll(): void {
  filterForm.value = { object_ref: '', action: '', since: '' }
  void router.replace({ path: '/audit', query: {} })
}

function gotoPage(next: number): void {
  if (next < 1 || next > totalPages.value) return
  const query: Record<string, string> = {}
  for (const [k, v] of Object.entries(route.query)) {
    if (k !== 'page' && typeof v === 'string') query[k] = v
  }
  if (next > 1) query.page = String(next)
  void router.replace({ path: '/audit', query })
}

// 展开查看原始行 JSON（局部交互状态）
const expandedIds = ref<Set<number>>(new Set())
function toggleExpand(id: number): void {
  const next = new Set(expandedIds.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  expandedIds.value = next
}
</script>

<template>
  <main class="bg-blueprint flex h-[100dvh] flex-col overflow-hidden text-ink select-none">
    <h1 class="sr-only">全操作审计追溯日志</h1>

    <header class="safe-t z-20 flex h-14 shrink-0 items-center justify-between gap-2 border-b border-ink/15 px-4">
      <div class="leading-none">
        <h2 class="text-lg font-black tracking-tight">审计追溯日志</h2>
        <p class="mt-1 font-mono text-[10px] uppercase tracking-[0.25em] text-ink-muted">Audit / Server-side Filter</p>
      </div>
      <button type="button" class="btn-hard btn-hard--compact" @click="drawerOpen = true">
        <svg aria-hidden="true" class="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
          <circle cx="11" cy="11" r="8"></circle>
          <path d="m21 21-4.35-4.35"></path>
        </svg>
        组合筛选
      </button>
    </header>

    <!-- 活跃查询参数（=已下发服务端的条件） -->
    <div v-if="activeParams.length > 0" class="flex shrink-0 items-center gap-2 overflow-x-auto border-b border-ink/15 px-4 py-2 [&::-webkit-scrollbar]:hidden">
      <span class="shrink-0 font-mono text-[10px] font-black uppercase tracking-widest text-ink-muted">query:</span>
      <span
        v-for="[key, value] in activeParams"
        :key="key"
        class="flex shrink-0 items-center gap-1 rounded-md border border-ink bg-sky px-1.5 py-0.5 font-mono text-[10px] font-black"
      >
        {{ key }}: {{ value }}
        <button type="button" class="flex h-4 w-4 items-center justify-center rounded-sm hover:bg-ink hover:text-white" :aria-label="`移除筛选 ${key}`" @click="removeParam(key)">
          <svg aria-hidden="true" class="h-2.5 w-2.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"></path></svg>
        </button>
      </span>
      <button type="button" class="shrink-0 font-mono text-[10px] font-black text-danger underline" @click="clearAll">清空</button>
    </div>

    <!-- 列表（服务端分页返回；不本地过滤） -->
    <section class="flex-1 space-y-3 overflow-y-auto overflow-x-hidden px-4 pb-10 pt-4">
      <p class="font-mono text-[10px] font-black uppercase tracking-widest text-ink-muted">
        server response · page {{ page }} / {{ totalPages }} · {{ total }} rows
      </p>

      <!-- 加载态 -->
      <div
        v-if="loading"
        class="flex items-center justify-center gap-2 rounded-2xl border-2 border-dashed border-ink/30 bg-surface/70 px-6 py-14 text-center"
        role="status"
      >
        <svg aria-hidden="true" class="h-4 w-4 motion-safe:animate-spin" viewBox="0 0 24 24" fill="none">
          <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
          <path class="opacity-90" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z"></path>
        </svg>
        <p class="text-sm font-black text-ink-muted">审计日志查询中…</p>
      </div>

      <!-- 错误态 -->
      <div
        v-else-if="loadError"
        class="flex items-center justify-between gap-3 rounded-2xl border-2 border-coral bg-blush px-4 py-3"
        role="alert"
      >
        <p class="text-xs font-black text-danger">审计日志加载失败：{{ loadError }}</p>
        <button type="button" class="btn-hard btn-hard--compact shrink-0" @click="fetchLogs">重试</button>
      </div>

      <!-- 空态 -->
      <div
        v-else-if="entries.length === 0"
        class="rounded-2xl border-2 border-dashed border-ink/30 bg-surface/70 px-6 py-14 text-center"
      >
        <p class="text-sm font-black">没有匹配的审计记录</p>
        <p class="mt-1.5 text-xs font-bold text-ink-muted">调整筛选条件后重新查询</p>
      </div>

      <template v-else>
        <article
          v-for="entry in entries"
          :key="entry.log_id"
          class="rounded-2xl border-2 border-ink bg-surface p-3.5 shadow-[2px_2px_0_var(--color-ink)]"
        >
          <button
            type="button"
            class="flex min-h-[44px] w-full items-center justify-between gap-2 text-left"
            :aria-expanded="expandedIds.has(entry.log_id)"
            @click="toggleExpand(entry.log_id)"
          >
            <div class="flex min-w-0 items-center gap-2">
              <span class="shrink-0 rounded-md border border-ink bg-surface px-1.5 py-0.5 font-mono text-[10px] font-black text-ink">{{ entry.actor }}</span>
              <span class="truncate text-xs font-black">{{ entry.action }}</span>
              <span class="truncate font-mono text-[10px] font-bold text-ink-muted">→ {{ entry.object_ref }}</span>
            </div>
            <span class="shrink-0 font-mono text-[9px] font-bold text-ink-muted">{{ entry.timestamp.slice(0, 10) }}</span>
          </button>
          <pre
            v-if="expandedIds.has(entry.log_id)"
            class="motion-fade mt-2 overflow-x-auto rounded-xl border-2 border-ink bg-gray-900 p-2.5 font-mono text-[10px] leading-relaxed text-green-400"
          >{{ JSON.stringify(entry, null, 2) }}</pre>
        </article>

        <!-- 分页 -->
        <div class="flex items-center justify-between gap-3 pb-2">
          <button type="button" class="btn-hard btn-hard--compact" :disabled="page <= 1" @click="gotoPage(page - 1)">上一页</button>
          <span class="font-mono text-[10px] font-black text-ink-muted">{{ page }} / {{ totalPages }}</span>
          <button type="button" class="btn-hard btn-hard--compact" :disabled="page >= totalPages" @click="gotoPage(page + 1)">下一页</button>
        </div>
      </template>
    </section>

    <!-- 组合筛选抽屉 -->
    <div v-if="drawerOpen" class="motion-fade fixed inset-0 z-40 bg-ink/50" @click.self="drawerOpen = false">
      <div class="sheet-panel safe-b fixed inset-x-0 bottom-0 z-50 space-y-4 p-5" role="dialog" aria-modal="true" aria-label="组合筛选">
        <div class="flex items-center justify-between">
          <h2 class="flex items-center gap-2 text-lg font-black tracking-tight">组合筛选 <span class="tag-code">GET /audit-logs</span></h2>
          <button type="button" aria-label="关闭" class="flex h-11 w-11 items-center justify-center rounded-full border-2 border-ink bg-surface transition-all hover:bg-ink hover:text-white active:scale-95" @click="drawerOpen = false">
            <svg aria-hidden="true" class="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"></path></svg>
          </button>
        </div>
        <p class="text-[11px] font-bold text-ink-muted">条件将序列化为 URL Query 回显并直通服务端查询，本页不在前端执行全表过滤。</p>

        <div class="space-y-3">
          <input v-model="filterForm.object_ref" class="input-hard font-mono text-xs" placeholder="对象 ID（job / item / asset / template …）" aria-label="对象 ID">
          <select v-model="filterForm.action" class="input-hard min-h-[44px] text-xs" aria-label="动作类型">
            <option value="">动作类型（全部）</option>
            <option v-for="a in ACTION_OPTIONS" :key="a" :value="a">{{ a }}</option>
          </select>
          <input v-model="filterForm.since" type="date" class="input-hard min-h-[44px] font-mono text-xs" aria-label="起始日期">
        </div>

        <button type="button" class="btn-hard btn-hard--primary w-full !min-h-[48px]" :disabled="applying" @click="applyFilters">
          {{ applying ? '查询中…' : '应用筛选条件' }}
        </button>
      </div>
    </div>
  </main>
</template>

<!-- 验收: 真端点GET /audit-logs(admin only); 无前端全表过滤(仅URL query映射: object_ref/action/since/page); 服务端分页上下页; JSON横向滚动; 加载/错误/空态三态 -->
