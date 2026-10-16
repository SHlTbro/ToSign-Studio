<!-- src/pages/P6_Styles.vue -->
<script setup lang="ts">
import { userError } from '@/utils/userLanguage'
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import type { StyleSourceType } from '@/types/domain'
import { request, ApiRequestError } from '@/api/http'
import { loadPresetFont } from '@/utils/presetFont'

const router = useRouter()

// 真数据源：GET /styles（owner-scoped；列表/创建/审批门状态展示 per T58B §5C）。
// 后端行形状以 styles service profile_dict 为锚（style_id 为数字 id）。
interface StyleRow {
  style_id: number
  name: string
  source_type: StyleSourceType
  status: 'draft' | 'pending_review' | 'approved' | 'rejected'
  quality_status?: string | null
  coverage: number | null
  model_version: string | null
  receipt_id: string | null
  font_url: string | null
  enabled: boolean
  reject_reason: string | null
  created_at: string | null
}

const stylesList = ref<StyleRow[]>([])
const loading = ref(true)
const loadError = ref<string | null>(null)

async function loadStyles(): Promise<void> {
  loading.value = true
  loadError.value = null
  try {
    const d = await request<{ items: StyleRow[] }>('/api/v1/styles?page=1&page_size=60')
    stylesList.value = d.items
    for (const s of d.items) {
      if (s.font_url) loadStyleFont(s)
    }
  } catch (e) {
    loadError.value = e instanceof Error && e.message ? e.message : '风格列表加载失败'
  } finally {
    loading.value = false
  }
}
onMounted(() => {
  void loadStyles()
})

type FilterKey = 'all' | StyleSourceType
const FILTERS: { key: FilterKey; label: string }[] = [
  { key: 'all', label: '全部' },
  { key: 'M0_SYSTEM', label: '系统字库' },
  { key: 'M1_FROZEN', label: '冻结字模' },
  { key: 'HANDWRITING_ASSET', label: '个人手写' },
  { key: 'M2_ADAPTER', label: '适配器' },
]
const activeFilter = ref<FilterKey>('all')

const filteredStyles = computed(() =>
  activeFilter.value === 'all'
    ? stylesList.value
    : stylesList.value.filter((s) => s.source_type === activeFilter.value),
)

const SOURCE_META: Record<StyleSourceType, { label: string; className: string }> = {
  M0_SYSTEM: { label: '系统字库', className: 'border border-ink bg-gray-100 text-ink' },
  M1_FROZEN: { label: '冻结字模', className: 'border border-ink bg-gray-100 text-ink' },
  HANDWRITING_ASSET: { label: '个人手写', className: 'border border-ink bg-blue-100 text-blue-800' },
  M2_ADAPTER: { label: 'Beta 适配器', className: 'border-2 border-amber bg-amber/20 font-black text-amber' },
}

const STATUS_META: Record<StyleRow['status'], { label: string; className: string }> = {
  approved: { label: '已准入', className: 'border-mint bg-mint/40 text-emerald-800' },
  pending_review: { label: '待审', className: 'border-amber bg-amber/15 text-amber' },
  rejected: { label: '已驳回', className: 'border-coral bg-blush text-danger' },
  draft: { label: '草稿', className: 'border-ink/20 bg-canvas text-ink-muted' },
}

function isReady(style: StyleRow): boolean {
  return style.status === 'approved' && style.enabled
}

// 启停 Switch（批次 B1-03 解灰，D-T69-2 关闭）：POST /styles/{id}/enable|disable
// 真端点；乐观翻转 + 失败回滚 + 行内错误提示（RFC 9457 code:detail）。
const togglingIds = ref<Set<number>>(new Set())
const toggleErrors = ref<Record<number, string>>({})

async function toggleEnabled(style: StyleRow): Promise<void> {
  const id = style.style_id
  if (togglingIds.value.has(id)) return
  togglingIds.value.add(id)
  delete toggleErrors.value[id]
  const target = !style.enabled
  style.enabled = target // 乐观翻转
  try {
    const d = await request<StyleRow>(`/api/v1/styles/${id}/${target ? 'enable' : 'disable'}`, {
      method: 'POST',
    })
    style.enabled = d.enabled // 以服务端为准
  } catch (e) {
    style.enabled = !target // 失败回滚
    toggleErrors.value[id] =
      e instanceof ApiRequestError ? userError(e) : e instanceof Error && e.message ? e.message : '启停失败'
  } finally {
    togglingIds.value.delete(id)
  }
}
// 新建手写资产 -> /styles/new（预设基底字库下载 + 个人风格字体训练入口）
function onCreateTap(): void {
  void router.push('/styles/new')
}

// 字体展示集：卡片标题即字样 —— 有 font_url 的风格（HANDWRITING_ASSET / M2 类，
// D-T65-1 口径）加载真字体后用真字体渲染；无 font_url 或加载失败/票据过期时
// 回退该风格对应的系统字体栈近似。
function fontStackFor(style: StyleRow): string {
  switch (style.source_type) {
    case 'M0_SYSTEM':
      return '"Kaiti SC","STKaiti","KaiTi",serif'
    case 'M1_FROZEN':
      return '"STXingkai","Xingkai SC","Segoe Script",cursive'
    case 'HANDWRITING_ASSET':
      return '"Segoe Print","Yuanti SC","YouYuan",cursive'
    case 'M2_ADAPTER':
      return 'Georgia,"Times New Roman",serif'
    default:
      return 'inherit'
  }
}

// FontFace loading is shared with the preset picker; failures keep this list usable.
const fontEpoch = ref(0) // 加载完成时间戳，驱动预览字体族重算
const loadedFontFamilies = new Map<string, string>()
const failedFontUrls = new Set<string>()
const pendingFontLoads = new Set<string>()

function loadStyleFont(style: StyleRow): void {
  const fontUrl = style.font_url
  if (!fontUrl) return
  if (loadedFontFamilies.has(fontUrl) || failedFontUrls.has(fontUrl) || pendingFontLoads.has(fontUrl)) return
  pendingFontLoads.add(fontUrl)
  void loadPresetFont(fontUrl).then(
    (family) => { loadedFontFamilies.set(fontUrl, family) },
    () => { failedFontUrls.add(fontUrl) },
  ).finally(() => {
    pendingFontLoads.delete(fontUrl)
    fontEpoch.value += 1
  })
}

function displayFontFamily(style: StyleRow): string {
  void fontEpoch.value // 响应式依赖：字体加载完成后重渲染
  const fam = style.font_url ? loadedFontFamilies.get(style.font_url) : undefined
  return fam ? `"${fam}",${fontStackFor(style)}` : fontStackFor(style)
}
</script>

<template>
  <main class="bg-blueprint flex h-[100dvh] flex-col overflow-hidden text-ink select-none">
    <h1 class="sr-only">风格资产库</h1>

    <header class="safe-t z-20 flex h-14 shrink-0 items-center justify-between gap-2 border-b border-ink/15 px-4">
      <h2 class="text-xl font-black tracking-tight">风格资产库</h2>
      <button type="button" class="btn-hard btn-hard--primary btn-hard--compact" @click="onCreateTap">
        <svg aria-hidden="true" class="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round">
          <path d="M12 5v14M5 12h14"></path>
        </svg>
        新建手写资产
      </button>
    </header>

    <!-- 分类过滤 -->
    <div class="shrink-0 px-4 pt-3">
      <div class="flex gap-2 overflow-x-auto pb-1 [&::-webkit-scrollbar]:hidden" role="group" aria-label="风格分类筛选">
        <button
          v-for="filter in FILTERS"
          :key="filter.key"
          type="button"
          class="pill-hard shrink-0 !px-3.5 text-xs"
          :aria-pressed="activeFilter === filter.key"
          @click="activeFilter = filter.key"
        >
          {{ filter.label }}
        </button>
      </div>
    </div>

    <section class="flex-1 space-y-4 overflow-y-auto overflow-x-hidden px-4 pb-28 pt-4">
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
        <p class="text-sm font-black text-ink-muted">风格列表加载中…</p>
      </div>

      <!-- 错误态 -->
      <div
        v-else-if="loadError"
        class="flex items-center justify-between gap-3 rounded-2xl border-2 border-coral bg-blush px-4 py-3"
        role="alert"
      >
        <p class="text-xs font-black text-danger">风格列表加载失败：{{ loadError }}</p>
        <button type="button" class="btn-hard btn-hard--compact shrink-0" @click="loadStyles">重试</button>
      </div>

      <!-- 空态 -->
      <div
        v-else-if="filteredStyles.length === 0"
        class="rounded-2xl border-2 border-dashed border-ink/30 bg-surface/70 px-6 py-14 text-center"
      >
        <p class="text-sm font-black">该分类暂无风格资产</p>
        <p class="mt-1.5 text-xs font-bold text-ink-muted">切换分类，或创建新的手写资产</p>
      </div>

      <!-- 双列卡片 -->
      <div v-else class="grid grid-cols-2 items-start gap-3.5">
        <article
          v-for="style in filteredStyles"
          :key="style.style_id"
          class="relative flex flex-col gap-3 rounded-2xl border-2 border-ink bg-surface p-3.5 shadow-[3px_3px_0_var(--color-ink)] transition-all duration-150 hover:-translate-y-0.5 hover:shadow-[5px_5px_0_var(--color-ink)]"
        >
          <!-- 头部 -->
          <div class="flex items-start justify-between gap-2">
            <h3 class="min-h-[2.5rem] text-base leading-snug break-words" :style="{ fontFamily: displayFontFamily(style) }">{{ style.name }}</h3>
            <span :class="['shrink-0 rounded-md px-1.5 py-0.5 text-[10px] font-black', SOURCE_META[style.source_type].className]">
              {{ SOURCE_META[style.source_type].label }}
            </span>
          </div>

          <!-- 字样预览（标题同字体；有 font_url 时 @font-face 真字体，失败回退系统栈） -->
          <p
            aria-hidden="true"
            class="truncate border-y border-ink/10 py-1.5 text-lg leading-snug text-ink"
            :style="{ fontFamily: displayFontFamily(style) }"
          >
            星河鹭起 · To签 0123
          </p>

          <!-- 覆盖率 -->
          <p v-if="!style.font_url" class="text-[10px] text-ink-muted">界面占位字样，非个人模型效果</p>
          <RouterLink v-if="style.source_type === 'HANDWRITING_ASSET' && !style.receipt_id && ['draft', 'approved'].includes(style.status)" :to="{ path: '/styles/new', query: { style_id: String(style.style_id) } }" class="text-xs font-bold underline">{{ style.status === 'draft' ? '继续确认手写来源' : '继续采集手写字符' }}</RouterLink>
          <div>
            <div class="flex items-center justify-between font-mono text-[10px] font-bold">
              <span class="text-ink-muted">字形覆盖率</span>
              <span class="font-black">{{ style.coverage === null ? '—' : `${Math.round(style.coverage * 100)}%` }}</span>
            </div>
            <div
              class="mt-1.5 h-2.5 w-full overflow-hidden rounded-full border-2 border-ink bg-gray-200"
              role="progressbar"
              :aria-valuenow="style.coverage === null ? undefined : Math.round(style.coverage * 100)"
              aria-valuemin="0"
              aria-valuemax="100"
              :aria-label="`${style.name} 字形覆盖率`"
            >
              <div v-if="style.coverage !== null" class="h-full rounded-full bg-primary transition-all duration-500 motion-reduce:transition-none" :style="{ width: `${style.coverage * 100}%` }"></div>
            </div>
          </div>

          <!-- 状态 + Switch -->
          <div class="flex items-center justify-between gap-2 border-t border-ink/10 pt-2.5">
            <span :class="['rounded-full border-2 px-2 py-0.5 text-[10px] font-black', STATUS_META[style.status].className]">
              {{ STATUS_META[style.status].label }}
            </span>
            <button
              type="button"
              role="switch"
              :aria-checked="style.enabled"
              :aria-busy="togglingIds.has(style.style_id)"
              :aria-label="`${style.name} 启停`"
              class="flex min-h-[44px] min-w-[44px] items-center justify-end"
              :class="togglingIds.has(style.style_id) ? 'opacity-50' : ''"
              @click="toggleEnabled(style)"
            >
              <span
                class="relative inline-flex h-6 w-11 rounded-full border-2 border-ink transition-colors duration-150"
                :class="style.enabled ? 'bg-mint' : 'bg-gray-200'"
              >
                <span
                  class="absolute top-0.5 h-4 w-4 rounded-full border border-ink bg-white transition-all duration-150"
                  :style="{ left: style.enabled ? '1.375rem' : '2px' }"
                ></span>
              </span>
            </button>
          </div>

          <!-- 启停失败行内错误（RFC 9457 code:detail） -->
          <p
            v-if="toggleErrors[style.style_id]"
            class="rounded-lg border-2 border-coral bg-blush px-2 py-1 text-[10px] font-black text-danger"
            role="alert"
          >
            启停失败：{{ toggleErrors[style.style_id] }}
          </p>

          <!-- 准入角标 -->
          <div
            v-if="isReady(style)"
            class="flex items-center gap-1.5 rounded-lg border-2 border-ink bg-mint px-2 py-1 text-[10px] font-black"
          >
            <svg aria-hidden="true" class="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">
              <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10Z"></path>
              <path d="m9 12 2 2 4-4"></path>
            </svg>
            {{ style.source_type === 'HANDWRITING_ASSET' && !style.receipt_id ? '来源档案可采集字符' : '可用于批量生成' }}
          </div>
        </article>
      </div>
    </section>
  </main>
</template>

<!-- 验收: 真端点GET /styles; M2仅Beta标识差异; coverage百分比进度条; 启停Switch接POST enable/disable真端点(乐观翻转+失败回滚+行内错误, B1-03解灰D-T69-2); 仅准入风格标绿盾; snake_case字段; 加载/错误/空态三态; 有font_url风格走票据签发->FontFace注入真字体(失败回退系统栈,B1-08 D-T65-1) -->
