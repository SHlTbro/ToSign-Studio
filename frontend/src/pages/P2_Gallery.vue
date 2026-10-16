<!-- src/pages/P2_Gallery.vue -->
<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import type { Asset, AssetStatus } from '@/types/domain'
import { request } from '@/api/http'
import { xhrUpload } from '@/api/xhrUpload'
import { ApiRequestError } from '@/api/http'
import { ENDPOINT_ASSET_APPROVE, ENDPOINT_ASSET_DELETE, ENDPOINT_ASSET_REJECT, fill } from '@/api/endpoints'
import { useAuthStore } from '@/stores/auth'
import { aspectRatioValue, isSupportedAspect, orientationOf } from '@/utils/aspect'
import {
  drainUploadQueue,
  resumeUploadQueue,
  type UploadQueueTask,
} from '@/api/uploadQueue'

const authStore = useAuthStore()
const route = useRoute()
// D-T12-3 审批入口：仅 admin 可见（G58B-02），非 admin 不渲染决策按钮。
const isAdmin = computed(() => authStore.isAdmin())

// ---------- 资产数据集（真数据源 GET /assets；审批动作走真端点） ----------

const assetsList = ref<Asset[]>([])
const loadError = ref<string | null>(null)
const reviewing = ref(false)

type FilterKey = 'all' | 'approved' | 'pending_review' | 'rejected'
const FILTERS: { key: FilterKey; label: string }[] = [
  { key: 'all', label: '全部' },
  { key: 'approved', label: '已通过' },
  { key: 'pending_review', label: '待审批' },
  { key: 'rejected', label: '已驳回' },
]
function filterFromRoute(value: unknown): FilterKey {
  const candidate = Array.isArray(value) ? value[0] : value
  return FILTERS.some((filter) => filter.key === candidate) ? candidate as FilterKey : 'all'
}
const activeFilter = ref<FilterKey>(filterFromRoute(route.query.filter))
watch(() => route.query.filter, (filter) => {
  activeFilter.value = filterFromRoute(filter)
})

const filteredAssets = computed(() =>
  activeFilter.value === 'all'
    ? assetsList.value
    : activeFilter.value === 'pending_review'
      ? assetsList.value.filter((a) => a.status === 'pending' || a.status === 'pending_review')
      : assetsList.value.filter((a) => a.status === activeFilter.value),
)

const isEmpty = computed(() => filteredAssets.value.length === 0)
const failedPreviewIds = ref(new Set<string>())
const fullSizePreviewIds = ref(new Set<string>())

function previewUrl(assetId: string): string {
  const encodedId = encodeURIComponent(assetId)
  return fullSizePreviewIds.value.has(assetId)
    ? `/api/v1/assets/${encodedId}/file`
    : `/api/v1/assets/${encodedId}/thumbnail`
}

function markPreviewFailed(assetId: string): void {
  if (!fullSizePreviewIds.value.has(assetId)) {
    fullSizePreviewIds.value = new Set([...fullSizePreviewIds.value, assetId])
    return
  }
  failedPreviewIds.value = new Set([...failedPreviewIds.value, assetId])
}

const STATUS_META: Record<AssetStatus, { label: string; className: string }> = {
  pending: { label: '待审核', className: 'bg-amber text-ink' },
  approved: { label: '已通过', className: 'bg-mint text-ink' },
  pending_review: { label: '待审批', className: 'bg-amber text-ink' },
  rejected: { label: '已驳回', className: 'bg-coral text-white' },
  draft: { label: '草稿', className: 'bg-surface text-ink' },
}

const FRAME_TINTS = ['bg-sky', 'bg-blush', 'bg-lilac']
function frameTint(index: number): string {
  return FRAME_TINTS[index % FRAME_TINTS.length]
}

function formatBytes(bytes: number): string {
  if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(1)} GB`
  if (bytes >= 1024 ** 2) return `${(bytes / 1024 ** 2).toFixed(1)} MB`
  return `${(bytes / 1024).toFixed(1)} KB`
}

// ---------- 配额徽章（B1-07）：真端点 GET /api/v1/usage，失败降级 --（不阻塞列表） ----------
const usageText = ref<string>('-- / --')
async function refreshUsageBadge(): Promise<void> {
  try {
    const d = await request<{ total_bytes: number; limit_bytes: number }>('/api/v1/usage')
    usageText.value = `${formatBytes(d.total_bytes)} / ${formatBytes(d.limit_bytes)}`
  } catch {
    usageText.value = '-- / --'
  }
}

// ---------- 详情 BottomSheet 与审核决策 ----------

const selectedAsset = ref<Asset | null>(null)
const rejectMode = ref(false)
const rejectReason = ref('')
const deleteMode = ref(false)
const sheetOpen = computed(() => selectedAsset.value !== null)
const rejectValid = computed(() => rejectReason.value.trim().length >= 5)
const closeButton = ref<HTMLButtonElement | null>(null)
let lastTrigger: HTMLElement | null = null

const REJECT_PRESETS = ['分辨率不足', '画面含水印或文字', '非 Cos 原图', '边缘裁切严重']

function openDetail(asset: Asset, trigger?: EventTarget | null): void {
  lastTrigger = trigger instanceof HTMLElement ? trigger : null
  selectedAsset.value = asset
  rejectMode.value = false
  rejectReason.value = ''
  deleteMode.value = false
}

function applyReason(tag: string): void {
  rejectReason.value = tag
}

function closeDetail(): void {
  selectedAsset.value = null
  rejectMode.value = false
  rejectReason.value = ''
  deleteMode.value = false
}

async function confirmApprove(): Promise<void> {
  const asset = selectedAsset.value
  if (!asset || reviewing.value) return
  reviewing.value = true
  try {
    await request(fill(ENDPOINT_ASSET_APPROVE.path, { id: asset.asset_id }), { method: 'POST' })
    asset.status = 'approved'
    asset.approved_at = new Date().toISOString()
    showToast('已通过入库')
    void refreshAssets()
    closeDetail()
  } catch (e) {
    showToast(e instanceof Error && e.message ? `审批失败：${e.message}` : '审批失败，请重试')
  } finally {
    reviewing.value = false
  }
}

async function confirmReject(): Promise<void> {
  const asset = selectedAsset.value
  if (!asset || !rejectValid.value || reviewing.value) return
  reviewing.value = true
  try {
    await request(fill(ENDPOINT_ASSET_REJECT.path, { id: asset.asset_id }), {
      method: 'POST',
      body: { reason: rejectReason.value.trim() },
    })
    asset.status = 'rejected'
    showToast('已驳回')
    void refreshAssets()
    closeDetail()
  } catch (e) {
    showToast(e instanceof Error && e.message ? `驳回失败：${e.message}` : '驳回失败，请重试')
  } finally {
    reviewing.value = false
  }
}

async function confirmDelete(): Promise<void> {
  const asset = selectedAsset.value
  if (!asset || reviewing.value) return
  reviewing.value = true
  try {
    await request(fill(ENDPOINT_ASSET_DELETE.path, { id: asset.asset_id }), { method: 'DELETE' })
    assetsList.value = assetsList.value.filter((item) => item.asset_id !== asset.asset_id)
    failedPreviewIds.value.delete(asset.asset_id)
    fullSizePreviewIds.value.delete(asset.asset_id)
    closeDetail()
    showToast('原图已从资产库移除')
  } catch (e) {
    showToast(e instanceof Error && e.message ? `删除失败：${e.message}` : '删除失败，请重试')
  } finally {
    reviewing.value = false
  }
}

// 复制 SHA256 的本地 Toast 反馈
const toast = ref<string | null>(null)
let toastTimer: number | null = null
function showToast(message: string): void {
  toast.value = message
  if (toastTimer !== null) window.clearTimeout(toastTimer)
  toastTimer = window.setTimeout(() => {
    toast.value = null
    toastTimer = null
  }, 1600)
}

async function copyHash(): Promise<void> {
  const asset = selectedAsset.value
  if (!asset) return
  const hash = asset.sha256
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(hash)
    } else {
      const ta = document.createElement('textarea')
      ta.value = hash
      ta.style.position = 'fixed'
      ta.style.opacity = '0'
      document.body.appendChild(ta)
      ta.select()
      document.execCommand('copy')
      document.body.removeChild(ta)
    }
    showToast('SHA256 已复制到剪贴板')
  } catch {
    showToast('复制失败，请手动选择代码块复制')
  }
}

// 弹层打开时锁背景滚动、焦点进入；关闭归还焦点
watch(sheetOpen, async (open) => {
  if (typeof document !== 'undefined') {
    document.body.style.overflow = open ? 'hidden' : ''
  }
  if (open) {
    await nextTick()
    closeButton.value?.focus()
  } else {
    lastTrigger?.focus()
    lastTrigger = null
  }
})

function onKeydown(e: KeyboardEvent): void {
  if (e.key === 'Escape' && sheetOpen.value) closeDetail()
}

async function refreshAssets(): Promise<void> {
  loadError.value = null
  try {
    const d = await request<{ items: Asset[] }>('/api/v1/assets?type=cos&page=1&page_size=60')
    assetsList.value = d.items.filter(asset => asset.type === 'cos')
  } catch (e) {
    if (e instanceof ApiRequestError && e.status === 401) return
    loadError.value = e instanceof Error && e.message ? e.message : '资产列表加载失败'
  }
}

onMounted(() => {
  window.addEventListener('keydown', onKeydown)
  if (route.query.from === 'batch-upload') {
    showToast('新上传底图已进入待审批；审核通过后会出现在批量生成中')
  }
  void refreshAssets()
  void refreshUsageBadge()
})
onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKeydown)
  if (typeof document !== 'undefined') document.body.style.overflow = ''
  if (toastTimer !== null) window.clearTimeout(toastTimer)
})

// ---------- 上传队列（原型红线：不发真实网络；同构模拟 runOne，接真后端仅替换为 UploadSession.start） ----------

interface GalleryUploadTask extends UploadQueueTask {
  file: File
  fileName: string
}

const queue = ref<GalleryUploadTask[]>([])
const queueCollapsed = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)

let draining = false
let pausedAtIndex: number | null = null
const cancelledIds = new Set<string>()

const hasQueue = computed(() => queue.value.length > 0)
const queuePaused = computed(() => queue.value.some((t) => t.status === 'paused'))
const allSettled = computed(() =>
  queue.value.every((t) => t.status === 'done' || t.status === 'error'),
)

function pickFiles(): void {
  fileInput.value?.click()
}

function onPickFiles(event: Event): void {
  const input = event.target as HTMLInputElement
  const files = input.files
  if (!files || files.length === 0) return
  const stamp = Date.now()
  const tasks: GalleryUploadTask[] = Array.from(files).map((file, idx) => ({
    localId: `up-${stamp}-${idx}`,
    file,
    fileName: file.name,
    status: 'pending',
    progress: 0,
  }))
  queue.value.push(...tasks)
  queueCollapsed.value = false
  input.value = ''
  void drain(0)
}

async function runOne(task: GalleryUploadTask): Promise<void> {
  if (!task.file) throw new Error('missing file')
  const form = new FormData()
  form.append('files', task.file, task.fileName)
  await xhrUpload<{ items: { asset_id: string }[] }>(
    '/api/v1/assets',
    form,
    (p) => {
      task.progress = p
    },
  ).start()
  await refreshAssets()
  void refreshUsageBadge() // 上传完成刷新配额徽章（B1-07）
}

async function drain(startIndex: number): Promise<void> {
  if (draining) return
  draining = true
  try {
    const result = await drainUploadQueue(queue.value, runOne, startIndex)
    if (result.outcome === 'paused' && result.pausedAtIndex !== undefined) {
      pausedAtIndex = result.pausedAtIndex
    } else {
      pausedAtIndex = null
      pruneCancelled()
    }
  } finally {
    draining = false
  }
}

function pruneCancelled(): void {
  queue.value = queue.value.filter((t) => !(t.status === 'error' && t.error === '已取消'))
}

function togglePause(): void {
  if (queuePaused.value) {
    void resumeUploadQueue(queue.value, runOne, pausedAtIndex ?? 0).then((r) => {
      if (r.outcome !== 'paused') {
        pausedAtIndex = null
        pruneCancelled()
      }
    })
    return
  }
}

function cancelQueue(): void {
  queue.value.forEach((t) => {
    if (t.status !== 'done') cancelledIds.add(t.localId)
  })
  if (queuePaused.value) {
    void drain(pausedAtIndex ?? 0)
  }
}

function retryTask(task: GalleryUploadTask): void {
  cancelledIds.delete(task.localId)
  const idx = queue.value.indexOf(task)
  task.status = 'pending'
  task.progress = 0
  task.error = undefined
  void drain(idx)
}

function clearFinished(): void {
  queue.value = queue.value.filter((t) => t.status !== 'done')
}

const TASK_STATUS_META: Record<UploadQueueTask['status'], { label: string; tone: string; dot: string }> = {
  pending: { label: '等待中', tone: 'text-ink-muted', dot: 'bg-ink-muted' },
  uploading: { label: '上传中', tone: 'text-primary font-black', dot: 'bg-primary' },
  done: { label: '已完成', tone: 'text-emerald-700 font-black', dot: 'bg-mint' },
  error: { label: '失败', tone: 'text-danger font-black', dot: 'bg-coral' },
  paused: { label: '已暂停', tone: 'text-amber font-black', dot: 'bg-amber' },
}
</script>

<template>
  <main class="bg-blueprint flex h-[100dvh] flex-col overflow-hidden text-ink select-none">
    <h1 class="sr-only">生成底图资产台</h1>

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
      <div class="min-w-0 flex-1 leading-none">
        <h2 class="flex items-center gap-2 text-lg font-black tracking-tight">
          生成底图资产台
          <span class="rounded-md border border-ink/25 bg-surface px-1.5 py-0.5 font-mono text-[10px] font-black text-ink-muted">&lt;assets&gt;</span>
        </h2>
        <p class="mt-1 font-mono text-[10px] uppercase tracking-[0.25em] text-ink-muted">生成用无字底图</p>
      </div>
      <span class="flex shrink-0 items-center gap-1.5 rounded-full border-2 border-ink bg-surface px-2.5 py-1.5 font-mono text-[10px] font-black shadow-[2px_2px_0_var(--color-ink)] sm:px-3 sm:text-[11px]">
        <svg aria-hidden="true" class="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
          <ellipse cx="12" cy="5" rx="9" ry="3"></ellipse>
          <path d="M3 5v14a9 3 0 0 0 18 0V5"></path>
          <path d="M3 12a9 3 0 0 0 18 0"></path>
        </svg>
        {{ usageText }}
      </span>
    </header>

    <!-- 圆润分段筛选 -->
    <div class="shrink-0 px-4 pt-3">
      <p class="mb-2 text-[11px] text-ink-muted">这里只展示生成底图，需无 To签等文字。训练参考图请到训练页查看。</p>
      <RouterLink to="/training" class="mb-2 block min-h-11 text-xs underline">查看训练参考图</RouterLink>
      <div class="flex gap-2 overflow-x-auto pb-1 [&::-webkit-scrollbar]:hidden" role="group" aria-label="资产状态筛选">
        <button
          v-for="filter in FILTERS"
          :key="filter.key"
          type="button"
          :aria-pressed="activeFilter === filter.key"
          :class="[
            'min-h-[44px] shrink-0 rounded-full border-2 px-4.5 py-2 text-sm font-black transition-all duration-150 ease-[cubic-bezier(0.22,1,0.36,1)] active:scale-95',
            activeFilter === filter.key
              ? 'border-ink bg-ink text-white shadow-[2px_2px_0_var(--color-ink-soft)]'
              : 'border-ink bg-surface shadow-[2px_2px_0_var(--color-ink)] hover:-translate-y-0.5 hover:bg-sky active:translate-y-0 active:shadow-none',
          ]"
          @click="activeFilter = filter.key"
        >
          {{ filter.label }}
        </button>
      </div>
    </div>

    <section class="flex-1 space-y-4 overflow-y-auto overflow-x-hidden px-4 pb-28 pt-3">
      <div
        v-if="route.query.from === 'batch-upload'"
        class="rounded-xl border-2 border-amber bg-amber/15 px-3 py-2 text-xs font-bold"
        role="status"
      >
        新上传底图已列在「待审批」中；审核通过后会自动出现在批量生成的底图列表。
      </div>
      <input
        ref="fileInput"
        type="file"
        accept="image/*"
        multiple
        aria-label="选择要上传的原图文件"
        class="hidden"
        @change="onPickFiles"
      >

      <!-- 上传队列 -->
      <div v-if="hasQueue" class="motion-fade rounded-2xl border-2 border-ink bg-surface shadow-[3px_3px_0_var(--color-ink)]">
        <div class="flex items-center justify-between border-b border-ink/10 px-4 py-3">
          <p class="flex items-center gap-2 font-mono text-[11px] font-black uppercase tracking-widest">
            <span class="rounded-md border-2 border-ink bg-sky px-1.5 py-0.5">Upload</span>
            队列 <span class="text-primary">[{{ queue.length }}]</span>
          </p>
          <button
            type="button"
            class="flex h-11 min-w-[44px] items-center gap-1 rounded-full px-2 font-mono text-[11px] font-black text-ink-muted"
            :aria-expanded="!queueCollapsed"
            @click="queueCollapsed = !queueCollapsed"
          >
            {{ queueCollapsed ? '展开' : '收起' }}
            <svg aria-hidden="true" class="h-3 w-3 transition-transform duration-150" :class="queueCollapsed ? 'rotate-180' : ''" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round">
              <path d="m6 9 6 6 6-6"></path>
            </svg>
          </button>
        </div>

        <div v-if="!queueCollapsed" class="space-y-2.5 p-3.5">
          <div
            v-for="task in queue"
            :key="task.localId"
            class="rounded-xl border-2 border-ink/10 bg-canvas p-3 transition-colors"
          >
            <div class="flex items-center justify-between gap-2">
              <p class="min-w-0 flex-1 truncate font-mono text-[11px] font-bold">{{ task.fileName }}</p>
              <span :class="['flex shrink-0 items-center gap-1.5 text-[11px] font-black', TASK_STATUS_META[task.status].tone]">
                <span aria-hidden="true" :class="['h-2 w-2 rounded-full', TASK_STATUS_META[task.status].dot]"></span>
                {{ TASK_STATUS_META[task.status].label }}
                <template v-if="task.status === 'uploading' || task.status === 'paused'">{{ task.progress }}%</template>
              </span>
              <button
                v-if="task.status === 'error' && task.error !== '已取消'"
                type="button"
                aria-label="重试该上传任务"
                class="flex h-11 w-11 items-center justify-center rounded-full border-2 border-ink bg-surface transition-all hover:bg-ink hover:text-white active:scale-95"
                @click="retryTask(task)"
              >
                <svg aria-hidden="true" class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M21 12a9 9 0 1 1-2.64-6.36"></path>
                  <path d="M21 3v6h-6"></path>
                </svg>
              </button>
            </div>
            <div
              class="mt-2.5 h-2.5 w-full overflow-hidden rounded-full border border-ink/20 bg-surface"
              role="progressbar"
              :aria-valuenow="task.progress"
              aria-valuemin="0"
              aria-valuemax="100"
              :aria-label="`${task.fileName} 上传进度`"
            >
              <div class="h-full rounded-full bg-primary transition-all duration-150 motion-reduce:transition-none" :style="{ width: `${task.progress}%` }"></div>
            </div>
            <p v-if="task.status === 'error'" class="mt-1.5 font-mono text-[10px] font-black text-danger">{{ task.error }}</p>
          </div>

          <div class="flex items-center gap-2.5 pt-1">
            <button
              v-if="!allSettled"
              type="button"
              class="min-h-[44px] flex-1 rounded-full border-2 border-ink bg-surface px-3 font-mono text-[11px] font-black uppercase tracking-widest transition-all hover:bg-ink hover:text-white active:scale-95"
              @click="togglePause"
            >
              {{ queuePaused ? '继续上传' : '暂停' }}
            </button>
            <button
              v-if="!allSettled"
              type="button"
              class="min-h-[44px] flex-1 rounded-full border-2 border-danger bg-surface px-3 font-mono text-[11px] font-black uppercase tracking-widest text-danger transition-all hover:bg-danger hover:text-white active:scale-95"
              @click="cancelQueue"
            >
              取消队列
            </button>
            <button
              v-if="allSettled"
              type="button"
              class="min-h-[44px] flex-1 rounded-full border-2 border-ink bg-surface px-3 font-mono text-[11px] font-black uppercase tracking-widest transition-all hover:bg-ink hover:text-white active:scale-95"
              @click="clearFinished"
            >
              清空记录
            </button>
          </div>
        </div>
      </div>

      <!-- 错误态 -->
      <div
        v-if="loadError"
        class="motion-fade flex items-center justify-between gap-3 rounded-2xl border-2 border-coral bg-blush px-4 py-3"
        role="alert"
      >
        <p class="text-xs font-black text-danger">资产列表加载失败：{{ loadError }}</p>
        <button type="button" class="btn-hard btn-hard--compact shrink-0" @click="refreshAssets">重试</button>
      </div>

      <!-- 空态 -->
      <div
        v-if="!loadError && isEmpty"
        class="motion-rise flex flex-col items-center rounded-2xl border-2 border-dashed border-ink/30 bg-surface/70 px-6 py-16 text-center"
      >
        <span aria-hidden="true" class="flex h-14 w-14 items-center justify-center rounded-2xl border-2 border-ink bg-sky shadow-[3px_3px_0_var(--color-ink)]">
          <svg class="h-7 w-7" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"></path>
            <line x1="12" y1="10" x2="12" y2="17"></line>
            <line x1="9" y1="13.5" x2="15" y2="13.5"></line>
          </svg>
        </span>
        <p class="mt-4 text-sm font-black">当前视图暂无资产</p>
        <p class="mt-1 font-mono text-[11px] font-bold text-ink-muted">点右下角圆形按钮，上传第一张干净原图</p>
      </div>

      <!-- 拍立得网格 -->
      <div v-else-if="!loadError" class="motion-rise grid grid-cols-2 items-start gap-3.5">
        <button
          v-for="(asset, idx) in filteredAssets"
          :key="asset.asset_id"
          type="button"
          :aria-label="`资产 ${asset.asset_id}，用途 ${asset.type === 'sample_source' ? '训练参考图' : '生成底图'}，状态 ${STATUS_META[asset.status].label}，${asset.width} 乘 ${asset.height}`"
          class="group rounded-2xl border-2 border-ink bg-surface p-2 pb-3 text-left shadow-[3px_3px_0_var(--color-ink)] transition-all duration-150 ease-[cubic-bezier(0.22,1,0.36,1)] hover:-translate-y-0.5 hover:shadow-[5px_5px_0_var(--color-ink)] active:translate-y-0 active:shadow-none"
          @click="openDetail(asset, $event.currentTarget)"
        >
          <div
            :style="{ aspectRatio: aspectRatioValue(asset.width, asset.height) }"
            :class="[
              'relative flex w-full items-center justify-center overflow-hidden rounded-xl border-2 border-ink transition-transform duration-150 group-hover:scale-[1.01]',
              frameTint(idx),
            ]"
          >
            <img
              v-if="!failedPreviewIds.has(asset.asset_id)"
              :src="previewUrl(asset.asset_id)"
              :alt="`原图预览 ${asset.width}×${asset.height}`"
              class="absolute inset-0 h-full w-full object-contain"
              loading="lazy"
              decoding="async"
              draggable="false"
              @error="markPreviewFailed(asset.asset_id)"
            >
            <!-- 异常比例角标 -->
            <span
              v-if="!isSupportedAspect(asset.width, asset.height)"
              class="absolute bottom-2 left-2 z-10 rounded-md border-2 border-coral bg-surface px-1.5 py-0.5 font-mono text-[8px] font-black text-coral"
            >比例异常</span>
            <span
              :class="[
                'absolute left-2 top-2 z-10 rounded-full border-2 border-ink px-2 py-0.5 text-[10px] font-black',
                STATUS_META[asset.status].className,
              ]"
            >{{ STATUS_META[asset.status].label }}</span>
            <span
              aria-hidden="true"
              class="absolute right-2 top-2 rounded-md border-2 border-ink bg-surface/85 px-1.5 py-0.5 font-mono text-[9px] font-black"
            >{{ asset.width >= 2000 ? '2K' : 'HD' }}</span>
            <div v-if="failedPreviewIds.has(asset.asset_id)" class="absolute inset-0 flex flex-col items-center justify-center gap-2 text-ink/35" role="img" aria-label="原图预览加载失败">
              <svg aria-hidden="true" class="max-h-2/5 max-w-2/5" viewBox="0 0 100 100" fill="none" stroke="currentColor" stroke-width="3.5" :class="orientationOf(asset.width, asset.height) === 'landscape' ? 'w-1/3' : 'w-2/5'">
                <circle cx="50" cy="38" r="17"></circle>
                <path d="M18 92c4-20 17-30 32-30s28 10 32 30" stroke-linecap="round"></path>
              </svg>
              <span class="rounded bg-surface/85 px-2 py-1 text-[10px] font-bold">原图预览暂不可用</span>
            </div>
          </div>
          <p class="mt-2 px-1 text-[10px] font-bold">{{ asset.type === 'sample_source' ? '训练参考图 · 可含 To签' : '生成底图 · 需无 To签' }}</p>
          <div class="flex items-center justify-between px-1 pt-2.5">
            <span class="text-[11px] font-black">{{ asset.width }}×{{ asset.height }}<span class="ml-1 font-mono text-[9px] font-bold text-ink-muted">{{ orientationOf(asset.width, asset.height) === 'portrait' ? '竖' : orientationOf(asset.width, asset.height) === 'landscape' ? '横' : '方' }}</span></span>
            <span class="font-mono text-[11px] font-bold text-ink-muted">{{ formatBytes(asset.bytes) }}</span>
          </div>
        </button>
      </div>
    </section>

    <!-- FAB -->
    <button
      type="button"
      aria-label="上传生成底图"
      class="fixed right-5 z-30 flex h-14 w-14 items-center justify-center rounded-2xl border-2 border-ink bg-ink text-white shadow-[3px_3px_0_var(--color-ink-soft)] transition-all duration-150 ease-[cubic-bezier(0.22,1,0.36,1)] hover:-translate-y-0.5 hover:bg-primary hover:shadow-[5px_5px_0_var(--color-ink)] active:translate-y-0 active:shadow-none"
      style="bottom: calc(1.25rem + env(safe-area-inset-bottom));"
      @click="pickFiles"
    >
      <svg aria-hidden="true" class="h-6 w-6" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round">
        <path d="M12 5v14M5 12h14"></path>
      </svg>
    </button>

    <!-- 展册抽屉 -->
    <div
      v-if="sheetOpen && selectedAsset"
      class="motion-fade fixed inset-0 z-40 bg-ink/50"
      @click.self="closeDetail"
    >
      <div
        class="motion-sheet safe-b fixed inset-x-0 bottom-0 z-50 max-h-[88vh] overflow-y-auto rounded-t-2xl border-t-4 border-ink bg-surface"
        role="dialog"
        aria-modal="true"
        aria-label="资产详情与审核"
      >
        <div class="flex items-center justify-between border-b border-ink/10 px-5 py-4">
          <div>
            <h2 class="flex items-center gap-2 text-lg font-black tracking-tight">
              资产详情
              <span class="rounded-md border border-ink/25 bg-canvas px-1.5 py-0.5 font-mono text-[10px] font-black text-ink-muted">&lt;inspector&gt;</span>
            </h2>
            <p class="mt-0.5 font-mono text-[11px] font-bold text-ink-muted">{{ selectedAsset.asset_id }}</p>
          </div>
          <button
            ref="closeButton"
            type="button"
            aria-label="关闭详情"
            class="flex h-11 w-11 items-center justify-center rounded-full border-2 border-ink bg-surface transition-all hover:bg-ink hover:text-white active:scale-95"
            @click="closeDetail"
          >
            <svg aria-hidden="true" class="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round">
              <path d="M18 6 6 18M6 6l12 12"></path>
            </svg>
          </button>
        </div>

        <!-- 预览相纸 -->
        <!-- 灯箱：固定视口，内部按真实比例 contain，居中留边不拉伸 -->
        <div class="px-4 pt-4 pb-2">
          <div class="relative flex h-60 items-center justify-center overflow-hidden rounded-2xl border-2 border-ink bg-canvas shadow-[3px_3px_0_var(--color-ink)]">
            <div
              :style="{ aspectRatio: aspectRatioValue(selectedAsset.width, selectedAsset.height) }"
              :class="[
                'relative flex max-h-full max-w-full items-center justify-center overflow-hidden border-2 border-ink bg-sky',
                orientationOf(selectedAsset.width, selectedAsset.height) === 'landscape' ? 'w-full' : 'h-full',
              ]"
            >
              <img
                v-if="!failedPreviewIds.has(selectedAsset.asset_id)"
                :src="previewUrl(selectedAsset.asset_id)"
                :alt="`原图预览 ${selectedAsset.width}×${selectedAsset.height}`"
                class="absolute inset-0 h-full w-full object-contain"
                decoding="async"
                draggable="false"
                @error="markPreviewFailed(selectedAsset.asset_id)"
              >
              <div v-else class="flex flex-col items-center gap-2 text-ink/35" role="img" aria-label="原图预览加载失败">
                <svg aria-hidden="true" class="max-h-1/2 max-w-1/3" viewBox="0 0 100 100" fill="none" stroke="currentColor" stroke-width="3">
                  <circle cx="50" cy="38" r="17"></circle>
                  <path d="M18 92c4-20 17-30 32-30s28 10 32 30" stroke-linecap="round"></path>
                </svg>
                <span class="text-xs font-bold">原图预览暂不可用</span>
              </div>
              <span
                :class="[
                  'absolute left-3 top-3 rounded-full border-2 border-ink px-2.5 py-0.5 text-[10px] font-black',
                  STATUS_META[selectedAsset.status].className,
                ]"
              >{{ STATUS_META[selectedAsset.status].label }}</span>
            </div>
          </div>
        </div>

        <!-- 主动作区：紧随预览图，进入抽屉即可见 -->
        <div class="px-5 pt-3">
          <RouterLink
            :to="`/assets/${selectedAsset.asset_id}/annotate`"
            class="btn-hard btn-hard--primary h-[52px] w-full !rounded-2xl"
          >
            去标注落点
            <span class="rounded-full bg-white/15 px-2 py-0.5 font-mono text-[10px] font-black">全程人工 · 零 AI</span>
            <svg aria-hidden="true" class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round">
              <line x1="5" y1="12" x2="19" y2="12"></line>
              <polyline points="12 5 19 12 12 19"></polyline>
            </svg>
          </RouterLink>

          <!-- 驳回原因展开（仅 admin；D-T12-3 审批入口） -->
          <div v-if="isAdmin && rejectMode" class="motion-fade mt-3 rounded-2xl border-2 border-coral bg-blush/50 p-4">
            <p class="font-mono text-[11px] font-black uppercase tracking-widest text-danger">驳回原因 / ≥5 字符</p>
            <div class="mt-3 flex flex-wrap gap-2">
              <button
                v-for="tag in REJECT_PRESETS"
                :key="tag"
                type="button"
                :aria-pressed="rejectReason === tag"
                :class="[
                  'min-h-[44px] rounded-full border-2 px-3.5 text-[11px] font-black transition-all duration-150 active:scale-95',
                  rejectReason === tag
                    ? 'border-coral bg-coral text-white shadow-[2px_2px_0_var(--color-ink)]'
                    : 'border-ink bg-surface hover:bg-coral hover:text-white',
                ]"
                @click="applyReason(tag)"
              >
                {{ tag }}
              </button>
            </div>
            <textarea
              id="reject-reason"
              v-model="rejectReason"
              aria-describedby="reject-hint"
              class="mt-3 min-h-[70px] w-full rounded-xl border-2 border-coral bg-surface p-3 text-sm font-bold focus:outline-none focus:ring-2 focus:ring-coral"
              placeholder="点选上方词条，或自己写清驳回原因"
            ></textarea>
            <p id="reject-hint" class="mt-2 font-mono text-[10px] font-bold text-ink-muted" aria-live="polite">
              还需 {{ Math.max(0, 5 - rejectReason.trim().length) }} 个字符才能提交
            </p>
            <div class="mt-3 flex gap-3">
              <button
                type="button"
                class="min-h-[48px] w-[45%] rounded-2xl border-2 border-ink bg-surface px-3 text-sm font-black transition-all hover:bg-ink hover:text-white active:scale-95"
                @click="rejectMode = false; rejectReason = ''"
              >
                再看看
              </button>
              <button
                type="button"
                :disabled="!rejectValid || reviewing"
                class="min-h-[48px] w-[45%] rounded-2xl border-2 border-coral bg-coral px-3 text-sm font-black text-white shadow-[2px_2px_0_var(--color-ink)] transition-all active:scale-95 disabled:cursor-not-allowed disabled:opacity-40 disabled:shadow-none"
                @click="confirmReject"
              >
                {{ reviewing ? '提交中…' : '确认驳回' }}
              </button>
            </div>
          </div>

          <!-- 审核双决策（默认态并排；仅 admin 可见，非 admin 只读展示状态） -->
          <div v-else-if="isAdmin" class="mt-2.5 grid grid-cols-2 gap-2.5">
            <button
              type="button"
              :disabled="reviewing"
              class="flex h-[52px] items-center justify-center gap-1.5 rounded-2xl border-2 border-coral bg-surface px-2 text-sm font-black text-coral shadow-[2px_2px_0_var(--color-coral)] transition-all duration-150 hover:-translate-y-0.5 hover:bg-blush active:translate-y-0 active:shadow-none disabled:cursor-not-allowed disabled:opacity-50"
              @click="rejectMode = true"
            >
              <svg aria-hidden="true" class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round">
                <path d="M18 6 6 18M6 6l12 12"></path>
              </svg>
              驳回资产
            </button>
            <button
              type="button"
              :disabled="reviewing"
              class="flex h-[52px] items-center justify-center gap-1.5 rounded-2xl border-2 border-ink bg-primary px-2 text-sm font-black text-white shadow-[2px_2px_0_var(--color-ink)] transition-all duration-150 hover:-translate-y-0.5 active:translate-y-0 active:shadow-none disabled:cursor-not-allowed disabled:opacity-50"
              @click="confirmApprove"
            >
              <svg aria-hidden="true" class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.8" stroke-linecap="round" stroke-linejoin="round">
                <path d="M20 6 9 17l-5-5"></path>
              </svg>
              通过入库
            </button>
          </div>
          <p v-else class="mt-2.5 rounded-xl border border-ink/15 bg-canvas px-3 py-2.5 text-center font-mono text-[10px] font-bold text-ink-muted">
            审批由管理员执行 · 当前为只读视图
          </p>

          <div v-if="deleteMode" class="mt-3 rounded-2xl border-2 border-coral bg-blush/50 p-4" role="alertdialog" aria-label="确认删除资产">
            <p class="text-sm font-black text-danger">确认从资产库删除这张原图？</p>
            <p class="mt-1 text-[11px] font-bold text-ink-muted">删除后将从资产台和批量生成列表移除；系统保留原文件与审计记录。</p>
            <div class="mt-3 flex gap-3">
              <button
                type="button"
                class="min-h-[48px] flex-1 rounded-2xl border-2 border-ink bg-surface px-3 text-sm font-black"
                :disabled="reviewing"
                @click="deleteMode = false"
              >
                取消
              </button>
              <button
                type="button"
                aria-label="确认删除资产"
                class="min-h-[48px] flex-1 rounded-2xl border-2 border-coral bg-coral px-3 text-sm font-black text-white shadow-[2px_2px_0_var(--color-ink)] disabled:opacity-50"
                :disabled="reviewing"
                @click="confirmDelete"
              >
                {{ reviewing ? '删除中…' : '确认删除' }}
              </button>
            </div>
          </div>
          <button
            v-else
            type="button"
            aria-label="删除资产"
            class="mt-3 min-h-[44px] w-full rounded-xl border border-coral/50 px-3 text-xs font-black text-danger transition-colors hover:bg-blush"
            :disabled="reviewing"
            @click="deleteMode = true"
          >
            删除这张原图
          </button>
        </div>

        <!-- 参数卡（次要元数据） -->
        <div class="grid grid-cols-2 gap-2.5 px-5 pt-4">
          <div class="rounded-xl border border-ink/10 bg-canvas p-3">
            <p class="font-mono text-[9px] uppercase tracking-widest text-ink-muted">分辨率</p>
            <p class="mt-1 font-mono text-[12px] font-black">{{ selectedAsset.width }} × {{ selectedAsset.height }}</p>
          </div>
          <div class="rounded-xl border border-ink/10 bg-canvas p-3">
            <p class="font-mono text-[9px] uppercase tracking-widest text-ink-muted">体积</p>
            <p class="mt-1 font-mono text-[12px] font-black">{{ formatBytes(selectedAsset.bytes) }}</p>
          </div>
          <div class="col-span-2 rounded-xl border border-ink/10 bg-canvas p-3">
            <p class="font-mono text-[9px] uppercase tracking-widest text-ink-muted">创建时间</p>
            <p class="mt-1 font-mono text-[12px] font-black">{{ selectedAsset.created_at }}</p>
          </div>
          <div v-if="selectedAsset.approved_at" class="col-span-2 rounded-xl border-2 border-mint bg-mint/30 p-3">
            <p class="font-mono text-[9px] uppercase tracking-widest text-emerald-700">通过时间</p>
            <p class="mt-1 font-mono text-[12px] font-black text-emerald-800">{{ selectedAsset.approved_at }}</p>
          </div>
          <div class="col-span-2 rounded-xl border border-ink/10 bg-canvas p-3">
            <p class="font-mono text-[9px] uppercase tracking-widest text-ink-muted">缩略图路径</p>
            <p class="mt-1 break-all font-mono text-[11px] font-bold">{{ selectedAsset.thumbnail_path }}</p>
          </div>
        </div>

        <!-- SHA256 -->
        <div class="px-5 pt-4">
          <div class="rounded-2xl border-2 border-ink bg-ink-soft p-3.5">
            <div class="flex items-center justify-between gap-2">
              <p class="font-mono text-[10px] font-black uppercase tracking-widest text-white/70">SHA256 完整性校验</p>
              <button
                type="button"
                class="flex min-h-[44px] items-center gap-1.5 rounded-full border-2 border-white/40 bg-primary px-3.5 font-mono text-[11px] font-black text-white transition-all hover:-translate-y-0.5 active:translate-y-0"
                @click="copyHash"
              >
                <svg aria-hidden="true" class="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
                  <rect x="9" y="9" width="13" height="13" rx="2"></rect>
                  <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path>
                </svg>
                复制
              </button>
            </div>
            <p class="mt-2.5 break-all rounded-xl bg-black/25 p-2.5 font-mono text-[10px] leading-relaxed text-white/85">
              {{ selectedAsset.sha256 }}
            </p>
          </div>
        </div>
      </div>
    </div>

    <!-- Toast -->
    <div
      v-if="toast"
      role="status"
      class="pointer-events-none fixed inset-x-0 z-[60] flex justify-center px-4"
      style="bottom: calc(1.5rem + env(safe-area-inset-bottom));"
    >
      <span class="motion-fade flex items-center gap-2 rounded-full border-2 border-ink bg-ink px-4 py-2.5 font-mono text-[11px] font-black text-white shadow-hard">
        <span aria-hidden="true" class="flex h-4 w-4 items-center justify-center rounded-full bg-mint text-ink">
          <svg class="h-2.5 w-2.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="4" stroke-linecap="round" stroke-linejoin="round">
            <path d="M20 6 9 17l-5-5"></path>
          </svg>
        </span>
        {{ toast }}
      </span>
    </div>
  </main>
</template>

<!-- 蓝图网格+圆润双层拍立得+浅蓝淡黑; 抽屉motion-sheet上滑; Split糖果决策物理分列; 驳回≥5字门禁; 空态; safe-area; T35状态机不变 -->
