<!-- src/pages/P5_Review.vue -->
<script setup lang="ts">
import { userText } from '@/utils/userLanguage'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import type { RenderItem } from '@/types/domain'
import FrozenInkSummary from '@/components/FrozenInkSummary.vue'
import type { FrozenRenderSettings } from '@/types/renderControls'
import { request } from '@/api/http'
import { ENDPOINT_RENDER_ITEM_RETRY } from '@/api/endpoints'
import { useAuthStore } from '@/stores/auth'
import { usePolling } from '@/composables/usePolling'

const router = useRouter()
const route = useRoute()
const auth = useAuthStore()
const jobId = computed(() => String(route.params.jobId ?? ''))
// Owned review/proof requests can outlast the general HTTP budget. Never replay a QC action automatically.
const REVIEW_REQUEST_OPTIONS = { timeoutMs: 600000, retries: 0 } as const

// ---------- 真数据 + 乐观审核状态 ----------

const items = ref<RenderItem[]>([])
const frozenSettings = ref<(FrozenRenderSettings & { status: string }) | null>(null)
const loadError = ref(false)
const hasLoaded = ref(false)
const loadedIdentity = ref<string | null>(null)
const currentIndex = ref(0)
const currentItem = computed<RenderItem | null>(() => items.value[currentIndex.value] ?? null)
const cancelPending = ref(false)
const cancelSubmitted = ref(false)
const cancelStorageReady = ref(false)
const cancelMessage = ref<string | null>(null)
const cancelIdentity = computed(() => auth.me?.user_id && jobId.value
  ? `yy:render-cancel:${auth.me.user_id}:${jobId.value}` : null)
watch(cancelIdentity, key => {
  cancelPending.value = false
  cancelSubmitted.value = false
  cancelStorageReady.value = false
  try {
    cancelSubmitted.value = Boolean(key && sessionStorage.getItem(key) === 'submitted')
    cancelStorageReady.value = Boolean(key)
    cancelMessage.value = cancelSubmitted.value ? '取消结果尚未确认；请读取原任务状态，不会自动重发取消。' : null
  } catch {
    cancelMessage.value = '无法读取取消请求标记，已暂停提交；请恢复浏览器存储后刷新，并读取原任务状态。'
  }
}, { immediate: true })
const retryPending = ref(false)
const retryUnconfirmed = ref(false)
const retryError = ref<string | null>(null)
const retryRegionEnabled = ref(false)
const retryRegionKey = ref('')
const retryRegionWidth = ref<number | string>(0)
const retryRegion = computed(() => currentItem.value?.retry_regions?.[retryRegionKey.value])
const retryImageWidth = computed(() => currentItem.value?.image_size?.[0] ?? 0)
watch([currentItem, retryRegionKey], () => {
  const keys = Object.keys(currentItem.value?.retry_regions ?? {})
  if (!keys.includes(retryRegionKey.value)) retryRegionKey.value = keys.includes('blessing') ? 'blessing' : keys[0] ?? ''
  retryRegionWidth.value = Math.round((retryRegion.value?.w_norm ?? 0) * retryImageWidth.value)
}, { immediate: true })
const originalJobId = computed(() => String(route.query.retry_of ?? ''))
const retryStorageKey = computed(() => `yy:render-item-retry:${auth.me?.user_id ?? 'anonymous'}:${jobId.value}:${currentItem.value?.item_id ?? ''}`)
function savedRetryKey(): string | null {
  const value = localStorage.getItem(retryStorageKey.value)
  return value && /^[a-zA-Z0-9-]{1,128}$/.test(value) ? value : null
}
watch([retryStorageKey, currentItem], () => { retryUnconfirmed.value = Boolean(savedRetryKey()); retryError.value = null }, { immediate: true })
async function retryCurrentItem(restore = false): Promise<void> {
  const item = currentItem.value
  if (!item || !item.retryable || retryPending.value || (retryUnconfirmed.value && !restore) || !['failed', 'cancelled'].includes(item.status)) return
  const sourceJob = jobId.value
  const key = savedRetryKey() ?? crypto.randomUUID()
  let body: { region_override: NonNullable<RenderItem['retry_regions']> } | undefined
  try {
    const saved = restore ? localStorage.getItem(`${retryStorageKey.value}:body`) : null
    if (saved) body = JSON.parse(saved)
    else if (retryRegionEnabled.value) {
      const width = Number(retryRegionWidth.value)
      const region = retryRegion.value
      if (!region || !Number.isInteger(width) || width <= 0 || !retryImageWidth.value || region.x_norm + width / retryImageWidth.value > 1) {
        retryError.value = '文字区域宽度须为原图范围内的正整数。尚未提交。'; return
      }
      body = { region_override: { [retryRegionKey.value]: { ...region, w_norm: width / retryImageWidth.value } } }
    }
    localStorage.setItem(retryStorageKey.value,key)
    localStorage.setItem(`${retryStorageKey.value}:body`, JSON.stringify(body ?? null))
  } catch {
    retryError.value = '无法保存重试身份，尚未提交。请恢复本地存储后再试。'; return
  }
  retryPending.value = true; retryError.value = null
  try {
    const result = await request<{ job: { job_id: string }; retried_item_ids: string[] }>(
      ENDPOINT_RENDER_ITEM_RETRY.path.replace('{id}', encodeURIComponent(sourceJob)).replace('{itemId}', encodeURIComponent(item.item_id)),
      { ...REVIEW_REQUEST_OPTIONS, method: 'POST', idempotencyKey: key, ...(body ? { body } : {}) },
    )
    if (!result.job?.job_id || !result.retried_item_ids?.includes(item.item_id)) throw new Error('重试回复缺少任务身份')
    await router.push({ path: `/batch/${result.job.job_id}/review`, query: { retry_of: originalJobId.value || sourceJob } })
  } catch {
    retryUnconfirmed.value = true
    retryError.value = '重试结果尚未确认，请查看任务记录；不要重复提交。原批次和成品已保留。'
  } finally { retryPending.value = false }
}

const carouselRef = ref<HTMLDivElement | null>(null)
// 每张卡独立的回退字典展开态，避免与轮播索引耦合
const expandedIds = ref<Set<string>>(new Set())
function toggleFallback(id: string): void {
  const next = new Set(expandedIds.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  expandedIds.value = next
}
const expandedProvenanceIds = ref<Set<string>>(new Set())
function glyphProvenance(item: RenderItem): string[] {
  return (item.warnings ?? []).filter((warning) => warning.startsWith('GLYPH_STYLE'))
}
function toggleGlyphProvenance(id: string): void {
  const next = new Set(expandedProvenanceIds.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  expandedProvenanceIds.value = next
}

// snap 轮播：以卡片在视口内的实际可见比例定索引（吸附微调后依然准确）
let slideObserver: IntersectionObserver | null = null
function syncIndexFromScroll(): void {
  const el = carouselRef.value
  if (!el || el.children.length === 0) return
  const center = el.scrollLeft + el.clientWidth / 2
  let best = 0
  let bestDist = Number.POSITIVE_INFINITY
  Array.from(el.children).forEach((child, i) => {
    const c = child as HTMLElement
    const childCenter = c.offsetLeft + c.offsetWidth / 2
    const dist = Math.abs(childCenter - center)
    if (dist < bestDist) {
      bestDist = dist
      best = i
    }
  })
  currentIndex.value = best
}

// R14 代际隔离：真轮询 GET /render-jobs/{id}，旧响应自动丢弃；连续 3 次失败转 completed + 显式报错
const renderPhase = ref<'rendering' | 'completed'>('rendering')
const readFailed = ref(false)
const lastReadAt = ref<number | null>(null)
const readPendingAt = ref<number | null>(null)
const clockNow = ref(Date.now())
const pageWaitStartedAt = ref(Date.now())
const waitClock = setInterval(() => { clockNow.value = Date.now() }, 1000)
onBeforeUnmount(() => clearInterval(waitClock))
function elapsedLabel(milliseconds: number): string {
  const seconds = Math.max(0, Math.floor(milliseconds / 1000))
  return `${Math.floor(seconds / 60)}分${seconds % 60}秒`
}
const waitingTime = computed(() => {
  const created = Date.parse(String((frozenSettings.value as { created_at?: string } | null)?.created_at ?? ''))
  const start = Number.isFinite(created) ? created : pageWaitStartedAt.value
  return `${Number.isFinite(created) ? '已等待' : '本页等待'} ${elapsedLabel(clockNow.value - start)}`
})
const readFreshness = computed(() => lastReadAt.value === null ? '尚未读取到任务状态' : `最近成功读取：${elapsedLabel(clockNow.value - lastReadAt.value)}前`)
const recordFreshness = computed(() => {
  const updated = Date.parse(String((frozenSettings.value as { updated_at?: string } | null)?.updated_at ?? ''))
  return Number.isFinite(updated) ? `任务记录 ${elapsedLabel(clockNow.value - updated)}未更新` : ''
})
const JOB_ACTIVE = new Set(['queued', 'running'])
const JOB_TERMINAL = new Set(['succeeded', 'partial', 'failed', 'cancelled'])
const canCancelJob = computed(() => Boolean(cancelIdentity.value) && loadedIdentity.value === cancelIdentity.value && cancelStorageReady.value && hasLoaded.value && !loadError.value
  && JOB_ACTIVE.has(String(frozenSettings.value?.status)) && !cancelPending.value && !cancelSubmitted.value)
watch(() => frozenSettings.value?.status, status => {
  if (status === 'cancelled') cancelMessage.value = '任务已取消；已有成品和历史记录保留。'
})
let pollFailures = 0
const poller = usePolling<{ phase: 'rendering' | 'completed' }>(async () => {
  const requestedIdentity = cancelIdentity.value
  const requestedJobId = jobId.value
  try {
    if (!requestedJobId) {
      loadError.value = true
      return { done: true, data: { phase: 'completed' as const } }
    }
    readPendingAt.value = Date.now()
    const data = await request<{ job: FrozenRenderSettings & { status: string }; items: RenderItem[] }>(
      `/api/v1/render-jobs/${requestedJobId}`,
      REVIEW_REQUEST_OPTIONS,
    )
    if (requestedJobId !== jobId.value || requestedIdentity !== cancelIdentity.value) return { done: false }
    pollFailures = 0
    readFailed.value = false
    lastReadAt.value = Date.now()
    clockNow.value = Date.now()
    const status = data.job?.status
    if (typeof status !== 'string' || (!JOB_ACTIVE.has(status) && !JOB_TERMINAL.has(status))) {
      hasLoaded.value = true
      loadError.value = true
      items.value = []
      return { done: true, data: { phase: 'completed' as const } }
    }
    hasLoaded.value = true
    loadError.value = false
    frozenSettings.value = data.job
    loadedIdentity.value = requestedIdentity
    items.value = Array.isArray(data.items) ? data.items : []
    const done = JOB_TERMINAL.has(status)
    renderPhase.value = done ? 'completed' : 'rendering'
    return { done, data: { phase: done ? ('completed' as const) : ('rendering' as const) } }
  } catch {
    if (requestedJobId !== jobId.value || requestedIdentity !== cancelIdentity.value) return { done: false }
    pollFailures += 1
    readFailed.value = true
    if (pollFailures >= 3) {
      loadError.value = true
      return { done: true, data: { phase: 'completed' as const } }
    }
    return { done: false, data: { phase: 'rendering' as const } }
  } finally {
    if (requestedJobId === jobId.value && requestedIdentity === cancelIdentity.value) readPendingAt.value = null
  }
}, { base: 2000, cap: 4000, immediate: true })

watch([jobId, () => auth.me?.user_id], () => {
  readFailed.value = false
  lastReadAt.value = null
  readPendingAt.value = null
  pageWaitStartedAt.value = Date.now()
  items.value = []
  frozenSettings.value = null
  loadedIdentity.value = null
  currentIndex.value = 0
  hasLoaded.value = false
  loadError.value = false
  renderPhase.value = 'rendering'
  pollFailures = 0
  poller.start()
})

function retryLoad(): void {
  items.value = []
  frozenSettings.value = null
  loadedIdentity.value = null
  currentIndex.value = 0
  hasLoaded.value = false
  loadError.value = false
  pollFailures = 0
  poller.start()
}

async function cancelCurrentJob(): Promise<void> {
  if (!canCancelJob.value) return
  const target = jobId.value
  const identity = cancelIdentity.value
  try { if (identity) sessionStorage.setItem(identity, 'submitted') }
  catch {
    cancelStorageReady.value = false
    cancelMessage.value = '无法保存取消请求标记，尚未提交；请恢复浏览器存储后刷新，并读取原任务状态。'
    return
  }
  cancelPending.value = true
  cancelSubmitted.value = true
  cancelMessage.value = '正在提交取消请求；不会创建或重做任务。'
  try {
    const result = await request<{ job: { job_id: string; status: string } }>(
      `/api/v1/render-jobs/${encodeURIComponent(target)}/cancel`, { method: 'POST', timeoutMs: 60000, retries: 0 },
    )
    if (identity !== cancelIdentity.value) return
    if (result.job?.job_id !== target || !JOB_ACTIVE.has(result.job.status) && !JOB_TERMINAL.has(result.job.status)) {
      throw new Error('服务端未返回原任务的有效状态')
    }
    cancelMessage.value = '取消请求已提交，正在核对原任务状态。'
    poller.stop()
    poller.start()
  } catch {
    if (identity !== cancelIdentity.value) return
    cancelMessage.value = '取消结果尚未确认；请读取原任务状态，不会自动重发取消。'
  } finally {
    if (identity === cancelIdentity.value) cancelPending.value = false
  }
}

// 轻量提示（决策提交失败等）
const notice = ref<string | null>(null)
let noticeTimer: number | null = null
function flash(message: string): void {
  notice.value = message
  if (noticeTimer !== null) window.clearTimeout(noticeTimer)
  noticeTimer = window.setTimeout(() => {
    notice.value = null
    noticeTimer = null
  }, 2000)
}

// ---------- 决策 ----------

const showRejectSheet = ref(false)
const rejectReason = ref('')
const rejectValid = computed(() => rejectReason.value.trim().length >= 2)
const showComplete = ref(false)
watch([items, renderPhase], ([loadedItems, phase]) => {
  if (!loadError.value && phase === 'completed' && loadedItems.length > 0 && loadedItems.every(reviewSettled)) {
    showComplete.value = true
  }
})
const exporting = ref(false)
const exportedOnce = ref(false)
const exportError = ref<string | null>(null)
const exportFailures = ref<{ item_id: string; error: string }[]>([])
const approvedExportCount = computed(() => items.value.filter((item) => item.status === 'approved').length)
function reviewSettled(item: { status: string }): boolean {
  return ['approved', 'rejected', 'failed', 'error', 'cancelled'].includes(item.status)
}
const closeRejectButton = ref<HTMLButtonElement | null>(null)
const decisionPending = ref(false)
const approvalComment = ref('')
watch(() => currentItem.value?.item_id, () => { approvalComment.value = '' })

const statusMeta = computed(() => {
  switch (currentItem.value?.status) {
    case 'failed':
    case 'error':
      return { label: '生成失败，未生成成品', className: 'bg-blush text-danger' }
    case 'cancelled':
      return { label: '已取消，未生成成品', className: 'bg-canvas text-ink-muted' }
    case 'approved':
      return { label: '已通过', className: 'bg-mint text-ink' }
    case 'rejected':
      return { label: '已打回', className: 'bg-coral text-white' }
    default:
      return { label: renderPhase.value === 'rendering' ? '正在生成' : currentItem.value ? '待审核' : '等待任务', className: 'bg-sky text-ink' }
  }
})

const decidedCount = computed(() =>
  items.value.filter((i) => i.status === 'approved' || i.status === 'rejected').length,
)
// completed=渲染完成待审；仅已给出审核结论后锁定决策
const isCurrentDecided = computed(() =>
  !currentItem.value || currentItem.value.status === 'approved' || currentItem.value.status === 'rejected',
)
const canReviewCurrent = computed(() =>
  renderPhase.value === 'completed' && Boolean(currentItem.value) && !['failed', 'error', 'cancelled'].includes(currentItem.value?.status ?? ''),
)

function scrollToSlide(index: number): void {
  const el = carouselRef.value?.children[index] as HTMLElement | undefined
  el?.scrollIntoView({ behavior: 'smooth', inline: 'start', block: 'nearest' })
}
function goPrev(): void {
  if (currentIndex.value > 0) scrollToSlide(currentIndex.value - 1)
}
function goNext(): void {
  if (currentIndex.value < items.value.length - 1) scrollToSlide(currentIndex.value + 1)
}

function advanceAfterDecision(): void {
  const allDecided = items.value.every(reviewSettled)
  if (currentIndex.value < items.value.length - 1) {
    const next = currentIndex.value + 1
    const el = carouselRef.value?.children[next] as HTMLElement | undefined
    el?.scrollIntoView({ behavior: 'smooth', inline: 'start', block: 'nearest' })
    return
  }
  if (allDecided) {
    window.setTimeout(() => {
      showComplete.value = true
    }, 350)
  }
}

type ExportArtifact = { artifact_id: string; relative_path: string }
const knownExports = ref<{ identity: string; jobId: string; linked: boolean; artifacts: ExportArtifact[]; selected?: ExportArtifact } | null>(null)
const existingPngs = computed(() => knownExports.value?.artifacts.filter(a => a.relative_path.toLowerCase().endsWith('.png')) ?? [])
const existingZips = computed(() => knownExports.value?.artifacts.filter(a => a.relative_path.toLowerCase().endsWith('.zip')) ?? [])
watch([cancelIdentity, originalJobId], () => { knownExports.value = null; exportedOnce.value = false; exporting.value = false; exportError.value = null })

function currentExportTarget(linked: boolean): string { return linked ? originalJobId.value || jobId.value : jobId.value }
function exportContextMatches(identity: string, target: string, linked: boolean): boolean {
  return identity === cancelIdentity.value && target === currentExportTarget(linked)
}

function validExportArtifacts(value: unknown): value is ExportArtifact[] {
  return Array.isArray(value) && value.every(a => a && typeof a.artifact_id === 'string' && a.artifact_id
    && typeof a.relative_path === 'string' && /\.(png|zip)$/i.test(a.relative_path))
}

async function readExportFiles(target: string, identity: string, linked: boolean): Promise<ExportArtifact[] | null> {
  const result = await request<{ job_id: string; export_artifacts: ExportArtifact[] }>(
    `/api/v1/render-jobs/${encodeURIComponent(target)}/outputs`, REVIEW_REQUEST_OPTIONS)
  if (!exportContextMatches(identity, target, linked)) return null
  if (result.job_id !== target || !validExportArtifacts(result.export_artifacts)) {
    throw new Error('导出文件记录无效，未提交新导出')
  }
  const artifacts = result.export_artifacts.filter(a => a.relative_path.split('/').some(part => part.startsWith('linked-')) === linked)
  knownExports.value = { identity, jobId: target, linked, artifacts }
  exportedOnce.value = false
  return artifacts
}

async function downloadExportFile(artifact: ExportArtifact, target: string, identity: string, linked: boolean): Promise<boolean> {
  const controller = new AbortController()
  const timer = window.setTimeout(() => controller.abort(), REVIEW_REQUEST_OPTIONS.timeoutMs)
  try {
    const response = await fetch(`/api/v1/render-jobs/${encodeURIComponent(target)}/exports/${encodeURIComponent(artifact.artifact_id)}/file`,
      { credentials: 'same-origin', signal: controller.signal, headers: { Accept: 'image/png, application/zip', 'X-Requested-With': 'XMLHttpRequest' } })
    if (!response.ok) throw new Error('文件下载未完成')
    const blob = await response.blob()
    if (!exportContextMatches(identity, target, linked)) return false
    const objectUrl = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = objectUrl
    anchor.download = artifact.relative_path.split('/').pop()!
    document.body.appendChild(anchor); anchor.click(); anchor.remove()
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000)
    if (!linked) exportedOnce.value = true
    flash('已下载已有成品文件')
    return true
  } finally { window.clearTimeout(timer) }
}

async function downloadExistingExport(artifact: ExportArtifact): Promise<void> {
  const record = knownExports.value
  if (!record || exporting.value || !exportContextMatches(record.identity, record.jobId, record.linked)) return
  exporting.value = true; exportError.value = null
  try {
    if (await downloadExportFile(artifact, record.jobId, record.identity, record.linked)) record.selected = artifact
  }
  catch { if (exportContextMatches(record.identity, record.jobId, record.linked)) exportError.value = '下载未完成；文件记录保留，请下载已有文件，不会重新导出。' }
  finally { if (exportContextMatches(record.identity, record.jobId, record.linked)) exporting.value = false }
}

async function exportApprovedPngs(linked = false, readOnly = false): Promise<void> {
  const identity = cancelIdentity.value
  if (!identity || loadedIdentity.value !== identity || exporting.value || loadError.value || (!linked && !readOnly && approvedExportCount.value === 0)) return
  const target = currentExportTarget(linked)
  const saved = knownExports.value
  if (!readOnly && saved?.identity === identity && saved.jobId === target && saved.linked === linked && saved.selected) {
    await downloadExistingExport(saved.selected)
    return
  }
  exporting.value = true
  exportError.value = null
  exportFailures.value = []
  let stage: 'read' | 'submit' | 'download' = 'read'
  try {
    const previous = await readExportFiles(target, identity, linked)
    if (previous === null || readOnly || previous.length > 0) return
    if (!exportContextMatches(identity, target, linked)) return
    const key = `yy:render-export:${auth.me?.user_id}:${target}:${linked ? 'linked' : 'direct'}`
    try {
      if (localStorage.getItem(key)) {
        exportError.value = '导出结果尚未确认；请读取已有导出文件，不会重复提交。'
        return
      }
      localStorage.setItem(key, 'submitted')
    } catch {
      exportError.value = '无法保存导出请求标记，尚未提交；请恢复浏览器存储，或读取已有导出文件。'
      return
    }
    stage = 'submit'
    const result = await request<{
      status: string
      artifacts: ExportArtifact[]
      failures: { item_id: string; error: string }[]
    }>(`/api/v1/render-jobs/${encodeURIComponent(target)}/${linked ? 'export-linked' : 'export'}`, { method: 'POST', ...REVIEW_REQUEST_OPTIONS })
    if (!exportContextMatches(identity, target, linked)) return
    if (!validExportArtifacts(result.artifacts)) throw new Error('未确认导出文件')
    exportFailures.value = Array.isArray(result.failures) ? result.failures : []
    if (exportFailures.value.length > 0) {
      exportError.value = `${exportFailures.value.length} 项导出失败；其余已生成文件仍可下载。`
    }
    const pngArtifacts = result.artifacts.filter((artifact) => artifact.relative_path.toLowerCase().endsWith('.png'))
    const single = !linked && pngArtifacts.length === 1
    const artifact = single
      ? pngArtifacts[0]
      : result.artifacts.find((candidate) => candidate.relative_path.toLowerCase().endsWith('.zip'))
    if (!artifact) throw new Error('服务端没有返回可下载的已审核 PNG')
    knownExports.value = { identity, jobId: target, linked, artifacts: result.artifacts, selected: artifact }
    stage = 'download'
    await downloadExportFile(artifact, target, identity, linked)
  } catch {
    if (!exportContextMatches(identity, target, linked)) return
    exportError.value = stage === 'submit' ? '导出结果尚未确认；请读取已有导出文件，不会重复提交。'
      : stage === 'download' ? '文件已导出，下载未完成；请下载已有文件，不会重新导出。'
        : '暂时无法读取导出记录，未提交新导出；请稍后读取已有导出文件。'
  } finally {
    if (exportContextMatches(identity, target, linked)) exporting.value = false
  }
}

async function approveCurrent(): Promise<void> {
  const item = currentItem.value
  if (!item || !canReviewCurrent.value || decisionPending.value) return
  const reviewedJobId = jobId.value
  decisionPending.value = true
  try {
    const comment = approvalComment.value.trim()
    if (comment) await request(`/api/v1/render-jobs/${jobId.value}/review`, { method: 'POST', ...REVIEW_REQUEST_OPTIONS,
      body: { item_ids: [item.item_id], decision: 'approved', comment } })
    else await request(`/api/v1/render-jobs/${jobId.value}/items/${item.item_id}/approve`, { method: 'POST', ...REVIEW_REQUEST_OPTIONS })
    const data = await request<{ job: { status: string }; items: RenderItem[] }>(`/api/v1/render-jobs/${reviewedJobId}`, REVIEW_REQUEST_OPTIONS)
    if (jobId.value !== reviewedJobId) return
    if (!Array.isArray(data.items) || !data.items.some(current => current.item_id === item.item_id && current.status === 'approved')) throw new Error('服务端尚未确认本项审核通过，请刷新核对')
    items.value = data.items
  } catch (err) {
    flash(err instanceof Error && err.message ? `审核已提交但回读失败：${err.message}；请刷新核对` : '审核结果回读失败，请刷新核对')
    return
  } finally {
    decisionPending.value = false
  }
  advanceAfterDecision()
}

function openReject(): void {
  rejectReason.value = ''
  showRejectSheet.value = true
}

watch(showRejectSheet, async (open) => {
  if (typeof document !== 'undefined') document.body.style.overflow = open ? 'hidden' : ''
  if (open) {
    await nextTick()
    closeRejectButton.value?.focus()
  }
})
function onKeydown(e: KeyboardEvent): void {
  if (e.key === 'Escape') {
    showRejectSheet.value = false
    if (showComplete.value) showComplete.value = false
  }
}
function closeReject(): void {
  showRejectSheet.value = false
}
async function confirmReject(): Promise<void> {
  if (!rejectValid.value || decisionPending.value || !canReviewCurrent.value) return
  const item = currentItem.value
  if (!item) return
  decisionPending.value = true
  try {
    await request(`/api/v1/render-jobs/${jobId.value}/items/${item.item_id}/reject`, {
      method: 'POST',
      ...REVIEW_REQUEST_OPTIONS,
      body: { comment: rejectReason.value.trim() },
    })
    const data = await request<{ job: { status: string }; items: RenderItem[] }>(`/api/v1/render-jobs/${jobId.value}`, REVIEW_REQUEST_OPTIONS)
    if (Array.isArray(data.items)) items.value = data.items
  } catch (err) {
    flash(err instanceof Error && err.message ? `审核已提交但回读失败：${err.message}；请刷新核对` : '审核结果回读失败，请刷新核对')
    return
  } finally {
    decisionPending.value = false
  }
  showRejectSheet.value = false
  advanceAfterDecision()
}
onMounted(() => {
  const el = carouselRef.value
  if (el && typeof IntersectionObserver !== 'undefined') {
    slideObserver = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting && entry.intersectionRatio >= 0.6) {
            const idx = Number((entry.target as HTMLElement).dataset.idx)
            if (!Number.isNaN(idx)) currentIndex.value = idx
          }
        }
      },
      { root: el, threshold: [0.6] },
    )
    el.querySelectorAll('article').forEach((a) => slideObserver?.observe(a))
  }
})

onBeforeUnmount(() => {
  slideObserver?.disconnect()
  if (typeof document !== 'undefined') document.body.style.overflow = ''
})


</script>

<template>
  <main
    class="bg-blueprint flex h-[100dvh] flex-col overflow-hidden text-ink select-none"
    @keydown="onKeydown"
  >
    <h1 class="sr-only">成品沉浸审核</h1>

    <header class="safe-t z-20 flex h-14 shrink-0 items-center justify-between gap-2 border-b border-ink/15 px-3">
      <button
        type="button"
        aria-label="返回批量工作台"
        class="btn-hard btn-hard--compact !rounded-full !px-3"
        @click="router.push('/batch')"
      >
        <svg aria-hidden="true" class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">
          <path d="M19 12H5"></path>
          <path d="m12 19-7-7 7-7"></path>
        </svg>
      </button>
      <div class="flex min-w-0 flex-col items-center">
        <p class="font-mono text-sm font-black tracking-tight">
          审核质检 ( {{ items.length ? currentIndex + 1 : 0 }} / {{ items.length }} )
        </p>
        <span class="tag-code mt-0.5">已审核 {{ decidedCount }} 张</span>
      </div>
      <span
        class="rounded-full border-2 border-ink px-2.5 py-1 text-[10px] font-black shadow-[2px_2px_0_var(--color-ink)]"
        :class="statusMeta.className"
      >
        {{ statusMeta.label }}
      </span>
    </header>
    <div v-if="hasLoaded && !loadError && (renderPhase === 'rendering' || cancelSubmitted)" class="flex shrink-0 flex-wrap items-center gap-2 border-b border-ink/15 bg-surface px-4 py-2 text-xs">
      <button v-if="renderPhase === 'rendering'" type="button" aria-label="取消当前生成任务" class="btn-hard btn-hard--compact" :disabled="!canCancelJob" @click="cancelCurrentJob">
        {{ cancelPending ? '取消请求提交中…' : '取消当前生成' }}
      </button>
      <button v-if="cancelSubmitted || !cancelStorageReady" type="button" aria-label="读取原任务取消状态" class="min-h-11 font-bold underline" :disabled="cancelPending" @click="retryLoad">读取原任务取消状态</button>
      <p v-if="cancelMessage" role="status" class="w-full font-bold">{{ cancelMessage }}</p>
    </div>

    <div v-if="currentItem && ['failed', 'error', 'cancelled'].includes(currentItem.status)" class="shrink-0 border-b border-ink/15 bg-surface px-4 py-2 text-xs">
      <p>{{ userText(currentItem.last_error ?? statusMeta.label) }}</p>
      <fieldset v-if="currentItem.retryable && retryImageWidth && currentItem.retry_regions" :disabled="retryPending || retryUnconfirmed" class="flex flex-wrap items-center gap-3">
        <label><input v-model="retryRegionEnabled" type="checkbox" aria-label="调整这张文字区域"> 调整这张文字区域</label>
        <template v-if="retryRegionEnabled">
          <select v-model="retryRegionKey" aria-label="文字区域" class="input-hard">
            <option v-for="(_, key) in currentItem.retry_regions" :key="key" :value="key">{{ key === 'blessing' ? '文案' : key === 'to_cn' ? '抬头' : key }}</option>
          </select>
          <label>宽度 <input v-model="retryRegionWidth" type="number" min="1" :max="Math.floor(retryImageWidth * (1 - (retryRegion?.x_norm ?? 0)))" aria-label="文字区域宽度（像素）" class="input-hard w-28"> px</label>
          <span>左边界、高度、字号及其他设置保留；原任务保留。</span>
        </template>
      </fieldset>
      <button v-if="currentItem.retryable && currentItem.status !== 'error'" type="button" class="btn-hard min-h-11" :disabled="retryPending || retryUnconfirmed" @click="retryCurrentItem()">{{ retryPending ? '提交中…' : '重试此项' }}</button>
      <button v-if="retryUnconfirmed" type="button" class="btn-hard min-h-11" :disabled="retryPending" @click="retryCurrentItem(true)">确认并恢复原重试（复用同一身份）</button>
      <p v-if="retryError" role="alert">{{ retryError }}</p>
    </div>
    <button v-if="originalJobId" type="button" class="min-h-11 shrink-0 text-xs font-bold underline" @click="router.push(`/batch/${originalJobId}/review`)">返回原批次（原记录保留）</button>
    <button v-if="originalJobId || items.some(item => ['failed', 'cancelled'].includes(item.status))" type="button" class="btn-hard min-h-11 shrink-0" :disabled="exporting || loadError || renderPhase !== 'completed'" @click="exportApprovedPngs(true)">导出原批次关联成品 ZIP（仅已审核）</button>
    <details v-if="frozenSettings && !loadError" class="shrink-0 border-b border-ink/15 bg-surface px-4 py-2 text-[11px]" data-testid="frozen-render-settings">
      <summary class="min-h-8 cursor-pointer font-black">本次任务冻结的字形来源、排版与墨色</summary>
      <p>来源选择：{{ frozenSettings.style_mode === 'system' ? '系统字体 M0' : frozenSettings.style_mode === 'profile' ? `档案 #${frozenSettings.style_id ?? '未记录'}` : '历史任务未记录明确来源模式' }}</p>
      <p>来源要求：{{ frozenSettings.glyph_source_policy === 'm2_cjk_required' ? '汉字仅使用当前 M2；其他字符明确使用系统字体 M0' : frozenSettings.glyph_source_policy === 'm2_required' ? '仅使用当前 M2' : frozenSettings.glyph_source_policy === 'fallback_allowed' ? '允许回退，逐字保留实际来源' : '未记录' }}</p>
      <p>排版配置：{{ frozenSettings.layout_profile ?? '未记录' }} · 重复种子：{{ frozenSettings.layout_seed ?? '未记录' }}</p>
      <p v-if="frozenSettings.layout_profile_sha256" class="break-all">排版配置校验：{{ frozenSettings.layout_profile_sha256 }}</p>
      <div v-if="frozenSettings.non_cjk_font_binding" class="mt-1 text-xs" data-testid="frozen-mixed-fonts">
        <p>英文通用手写 M0：{{ frozenSettings.non_cjk_font_binding.fonts.latin.name }} · {{ frozenSettings.non_cjk_font_binding.fonts.latin.id }}</p>
        <p class="break-all">英文文件校验：{{ frozenSettings.non_cjk_font_binding.fonts.latin.sha256 }}</p>
        <p>标点及其他 M0：{{ frozenSettings.non_cjk_font_binding.fonts.other.name }} · {{ frozenSettings.non_cjk_font_binding.fonts.other.id }}</p>
        <p class="break-all">其他文件校验：{{ frozenSettings.non_cjk_font_binding.fonts.other.sha256 }}</p>
        <p>中文必须 M2 成功；英文与标点不标为个人 M2。</p>
      </div>
      <FrozenInkSummary :settings="frozenSettings" />
      <p class="mt-1 text-ink-muted">这里显示服务端保存的本次任务设置；实际字形来源以逐字记录为准。</p>
    </details>

    <!-- 渲染阶段提示（轮询驱动，完成后消失） -->
    <div v-if="renderPhase === 'rendering' && !loadError" data-testid="generation-wait-status" role="status" class="flex shrink-0 items-center gap-2 border-b border-ink/15 bg-sky/50 px-4 py-2">
      <svg aria-hidden="true" class="h-3.5 w-3.5 motion-safe:animate-spin text-primary" viewBox="0 0 24 24" fill="none">
        <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
        <path class="opacity-90" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z"></path>
      </svg>
      <div class="text-xs text-ink-muted">
        <p class="font-bold">{{ readFailed ? '读取失败，当前进度未确认；正在重新读取，不会重建任务。' : hasLoaded ? '服务最近有响应；任务记录仍为生成中。' : '正在读取任务状态…' }}</p>
        <p>{{ waitingTime }} · {{ readFreshness }}</p>
        <p v-if="readPendingAt !== null && clockNow - readPendingAt >= 15000">本次状态读取已等待 {{ elapsedLabel(clockNow - readPendingAt) }}，尚未返回；当前进度待确认。</p>
        <p v-if="recordFreshness">{{ recordFreshness }}</p>
        <p>暂无模型阶段进度，服务有响应不代表推理仍在推进。完成后可以审核。</p>
      </div>
    </div>

    <!-- 横向 snap 轮播（触摸滑动 + 两侧切换箭头） -->
    <div class="relative flex min-h-0 flex-1">
      <div
        ref="carouselRef"
        class="flex flex-1 snap-x snap-mandatory items-center gap-4 overflow-x-auto scroll-smooth px-6 pb-2 pt-4 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
        @scroll.passive="syncIndexFromScroll"
      >
        <article
          v-for="(item, idx) in items"
          :key="item.item_id"
          :data-idx="idx"
          class="flex h-[65vh] min-w-[85vw] shrink-0 snap-center flex-col justify-between rounded-2xl border-2 border-ink bg-surface p-4 shadow-[4px_4px_0_var(--color-ink)]"
        >
          <!-- S11 provenance is already returned on the review item; render the backend's per-character source/version/fallback lines. -->
          <div v-if="glyphProvenance(item).length" class="relative">
            <button
              type="button"
              class="flex min-h-[44px] w-full items-center justify-between gap-2 rounded-xl border-2 border-primary bg-sky/30 px-3 py-2 text-left"
              :aria-expanded="expandedProvenanceIds.has(item.item_id)"
              @click="toggleGlyphProvenance(item.item_id)"
            >
              <span class="text-[11px] font-black text-ink">字形实际来源 · {{ glyphProvenance(item).length }} 条服务端记录</span>
              <span class="text-[10px] font-mono">{{ expandedProvenanceIds.has(item.item_id) ? '收起' : '查看' }}</span>
            </button>
            <div v-if="expandedProvenanceIds.has(item.item_id)" class="mt-2 space-y-1 rounded-xl border border-primary/40 bg-surface p-3">
              <p v-for="warning in glyphProvenance(item)" :key="warning" class="break-all font-mono text-[9px] font-bold text-ink-muted">{{ warning }}</p>
            </div>
          </div>
          <!-- 回退字体告警 -->
          <div v-if="item.fallback_used" class="relative">
            <button
              type="button"
              class="flex min-h-[44px] w-full items-center justify-between gap-2 rounded-xl border-2 border-amber bg-amber/15 px-3 py-2 text-left"
              :aria-expanded="expandedIds.has(item.item_id)"
              @click="toggleFallback(item.item_id)"
            >
              <span class="flex items-center gap-2 text-[11px] font-black text-ink">
                <svg aria-hidden="true" class="h-4 w-4 shrink-0 text-amber" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
                  <path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"></path>
                  <line x1="12" y1="9" x2="12" y2="13"></line>
                  <line x1="12" y1="17" x2="12.01" y2="17"></line>
                </svg>
                部分字形发生来源回退，请查看各字符的实际来源与版本
              </span>
              <svg aria-hidden="true" class="h-4 w-4 shrink-0 transition-transform duration-150" :class="expandedIds.has(item.item_id) ? 'rotate-180' : ''" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round">
                <path d="m6 9 6 6 6-6"></path>
              </svg>
            </button>
            <div
              v-if="expandedIds.has(item.item_id)"
              class="motion-fade mt-2 rounded-xl border border-amber bg-surface p-3"
            >
              <p class="font-mono text-[9px] font-black uppercase tracking-widest text-ink-muted">替换字典 fallback_map</p>
              <div v-if="Object.keys(item.fallback_map ?? {}).length" class="mt-1.5 space-y-1">
                <div
                  v-for="[ch, font] in Object.entries(item.fallback_map)"
                  :key="ch"
                  class="flex items-center justify-between rounded-lg bg-canvas px-2.5 py-1.5"
                >
                  <span class="text-sm font-black">「{{ ch }}」</span>
                  <span class="font-mono text-[11px] font-black text-primary">{{ font }}</span>
                </div>
              </div>
              <p v-else class="mt-1.5 text-xs font-bold text-ink-muted">服务端未提供逐字回退映射</p>
            </div>
          </div>

          <!-- 成品画芯完整适配，保留渲染图原始比例 -->
          <div class="relative my-3 flex flex-1 items-center justify-center overflow-hidden rounded-xl border-2 border-ink" :class="idx % 2 === 0 ? 'bg-sky' : 'bg-blush'">
            <!-- 真渲染图（后端图源端点）；无产出时退回文字占位 -->
            <img
              v-if="item.output_relative_path"
              :src="`/api/v1/render-jobs/${jobId}/items/${item.item_id}/image`"
              alt=""
              class="absolute inset-0 h-full w-full object-contain"
              draggable="false"
            >
            <template v-else>
              <svg aria-hidden="true" class="absolute h-2/5 w-2/5 text-ink/15" viewBox="0 0 100 100" fill="none" stroke="currentColor" stroke-width="3">
                <circle cx="50" cy="38" r="17"></circle>
                <path d="M18 92c4-20 17-30 32-30s28 10 32 30" stroke-linecap="round"></path>
              </svg>
              <!-- To 签成品文字占位 -->
              <div class="relative z-10 flex flex-col items-center gap-2 px-4 text-center">
                <span class="rounded-md border-2 border-ink bg-surface px-2 py-0.5 text-xs font-black" :class="{ 'whitespace-break-spaces break-all': frozenSettings?.layout_profile === 'natural-layout-v1' }" data-testid="review-cn">To {{ item.cn || '—' }}</span>
                <span class="rounded-md border border-ink/40 bg-surface/90 px-2 py-0.5 text-[11px] font-bold text-ink-muted">{{ ['failed', 'error', 'cancelled'].includes(item.status) ? '未生成成品' : item.blessing || '渲染中…' }}</span>
              </div>
            </template>
            <span class="absolute left-2 top-2 rounded-md border-2 border-ink bg-surface/85 px-1.5 py-0.5 font-mono text-[9px] font-black">图片版本 {{ item.render_version }}</span>
          </div>

          <!-- 元信息 -->
          <div class="flex items-center justify-between border-t border-ink/10 pt-3">
            <div>
              <p class="text-sm font-black">{{ item.item_id }}</p>
              <p class="font-mono text-[10px] font-bold text-ink-muted">图片版本 {{ item.render_version }}</p>
            </div>
            <span class="font-mono text-[10px] font-bold text-ink-muted">
              <span v-if="item.status === 'approved'" class="text-emerald-700">已质检通过</span>
              <span v-else-if="item.status === 'rejected'" class="text-coral">已打回：{{ item.comment }}</span>
              <span v-else-if="['failed', 'error', 'cancelled'].includes(item.status)">没有成品可审核</span>
              <span v-else>等待人工质检</span>
              <span v-if="item.reviewed_by || item.reviewed_at" class="mt-1 block text-[9px] font-bold text-ink-muted">
                {{ item.reviewed_by || '审核人未知' }}<template v-if="item.reviewed_at"> · {{ item.reviewed_at }}</template>
              </span>
            </span>
          </div>
        </article>
      </div>

      <div v-if="loadError" class="absolute inset-0 z-10 flex items-center justify-center bg-surface/95 p-6">
        <div class="max-w-sm space-y-3 text-center">
          <p class="text-lg font-black">任务读取失败</p>
          <p class="text-sm font-bold text-ink-muted">无法确认当前成品与审核状态。重试读取后再继续审核。</p>
          <button type="button" class="btn-hard btn-hard--primary w-full" @click="retryLoad">重试读取</button>
        </div>
      </div>
      <div v-else-if="!items.length && hasLoaded && renderPhase === 'completed'" class="absolute inset-0 flex items-center justify-center p-6">
        <div class="max-w-sm space-y-2 text-center">
          <p class="text-lg font-black">当前任务没有成品</p>
          <p class="text-sm font-bold text-ink-muted">任务可能已过期、尚无结果，或没有生成成功的项目。</p>
          <button type="button" class="btn-hard w-full" @click="router.push('/batch')">返回批量工作台</button>
        </div>
      </div>
      <div v-else-if="!items.length" class="absolute inset-0 flex items-center justify-center p-6" role="status">
        <p class="text-sm font-bold text-ink-muted">{{ hasLoaded ? '任务仍在运行，暂时没有可审核成品…' : '正在读取任务与成品…' }}</p>
      </div>

      <!-- 左右切换（触摸滑动之外的补充交互，边缘不遮挡决策栏） -->
      <button
        type="button"
        class="absolute left-2 top-1/2 z-20 flex h-11 w-11 -translate-y-1/2 items-center justify-center rounded-full border-2 border-ink bg-surface shadow-[2px_2px_0_var(--color-ink)] transition-all hover:bg-sky active:scale-90 disabled:opacity-30 disabled:shadow-none"
        aria-label="上一张"
        :disabled="currentIndex === 0"
        @click="goPrev"
      >
        <svg aria-hidden="true" class="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round">
          <path d="m15 18-6-6 6-6"></path>
        </svg>
      </button>
      <button
        type="button"
        class="absolute right-2 top-1/2 z-20 flex h-11 w-11 -translate-y-1/2 items-center justify-center rounded-full border-2 border-ink bg-surface shadow-[2px_2px_0_var(--color-ink)] transition-all hover:bg-sky active:scale-90 disabled:opacity-30 disabled:shadow-none"
        aria-label="下一张"
        :disabled="currentIndex === items.length - 1"
        @click="goNext"
      >
        <svg aria-hidden="true" class="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round">
          <path d="m9 18 6-6-6-6"></path>
        </svg>
      </button>
    </div>

    <!-- 滑动圆点指示 -->
    <div class="flex shrink-0 justify-center gap-1.5 pb-2">
      <span
        v-for="(item, i) in items"
        :key="`dot-${item.item_id}`"
        class="h-2 rounded-full transition-all duration-150"
        :class="i === currentIndex ? 'w-6 bg-ink' : 'w-2 bg-ink/25'"
      ></span>
    </div>

    <details v-if="canReviewCurrent && !isCurrentDecided && !loadError" class="shrink-0 border-t border-ink/15 bg-surface px-4 py-2 text-xs">
      <summary class="min-h-8 cursor-pointer font-bold">可选：质检说明</summary>
      <label>说明实际审核方式与范围<textarea v-model="approvalComment" aria-label="质检说明" maxlength="500" :disabled="decisionPending" class="input-hard mt-1 min-h-16 w-full"></textarea></label>
    </details>
    <!-- 双大决策栏（56px） -->
    <footer class="safe-b z-30 shrink-0 border-t-4 border-ink bg-surface px-4 py-3">
      <div class="flex items-center justify-between gap-3">
        <button
          type="button"
          class="split-cta split-cta--danger h-14 flex-1"
          :disabled="isCurrentDecided || !canReviewCurrent || decisionPending || loadError"
          @click="openReject"
        >
          <span class="!min-h-0 flex-1 gap-1.5 text-base">
            <svg aria-hidden="true" class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.8" stroke-linecap="round">
              <path d="M18 6 6 18M6 6l12 12"></path>
            </svg>
            打回重做
          </span>
          <span class="!w-10" aria-hidden="true">↗</span>
        </button>
        <button
          type="button"
          class="split-cta split-cta--mint h-14 flex-1"
          :disabled="isCurrentDecided || !canReviewCurrent || decisionPending || loadError"
          @click="approveCurrent"
        >
          <span class="!min-h-0 flex-1 gap-1.5 text-base">
            <svg aria-hidden="true" class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.8" stroke-linecap="round" stroke-linejoin="round">
              <path d="M20 6 9 17l-5-5"></path>
            </svg>
            质检通过
          </span>
          <span class="!w-10" aria-hidden="true">↗</span>
        </button>
      </div>
    </footer>

    <!-- 轻量提示 / 加载失败 -->
    <div
      v-if="notice"
      role="status"
      class="pointer-events-none fixed inset-x-0 z-[60] flex justify-center px-4"
      style="bottom: calc(7rem + env(safe-area-inset-bottom));"
    >
      <span class="motion-fade rounded-full border-2 border-ink bg-ink px-4 py-2 font-mono text-[11px] font-black text-white shadow-hard">
        {{ notice }}
      </span>
    </div>

    <!-- 打回理由抽屉 -->
    <div
      v-if="showRejectSheet"
      class="motion-fade fixed inset-0 z-40 bg-ink/50"
      @click.self="closeReject"
    >
      <div
        class="sheet-panel safe-b fixed inset-x-0 bottom-0 z-50 space-y-4 p-5"
        role="dialog"
        aria-modal="true"
        aria-label="填写打回理由"
      >
        <div class="flex items-start justify-between">
          <div>
            <h2 class="flex items-center gap-2 text-lg font-black tracking-tight">
              填写打回理由
              <span class="tag-code !border-coral !bg-blush text-coral">required ≥ 2</span>
            </h2>
            <p class="mt-0.5 text-[11px] font-bold text-ink-muted">理由将随任务打回生产端，便于定位缺陷</p>
          </div>
          <button
            ref="closeRejectButton"
            type="button"
            aria-label="关闭"
            class="flex h-11 w-11 items-center justify-center rounded-full border-2 border-ink bg-surface transition-all hover:bg-ink hover:text-white active:scale-95"
            @click="closeReject"
          >
            <svg aria-hidden="true" class="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round">
              <path d="M18 6 6 18M6 6l12 12"></path>
            </svg>
          </button>
        </div>
        <textarea
          v-model="rejectReason"
          class="input-hard input-hard--danger min-h-[100px] text-sm"
          placeholder="请指出具体缺陷（如：签名被遮挡、文字重叠、字迹模糊等）"
          aria-label="打回理由"
        ></textarea>
        <p class="font-mono text-[10px] font-bold text-ink-muted" aria-live="polite">
          还需 {{ Math.max(0, 2 - rejectReason.trim().length) }} 个字符才能提交
        </p>
        <div class="flex items-center justify-between gap-3 pt-1">
          <button type="button" class="btn-hard w-[45%]" @click="closeReject">取消</button>
          <button
            type="button"
            class="btn-hard btn-hard--danger w-[45%]"
            :disabled="!rejectValid || decisionPending"
            @click="confirmReject"
          >
            确认打回
          </button>
        </div>
      </div>
    </div>

    <!-- 批次完成模态 -->
    <div
      v-if="showComplete"
      class="motion-fade fixed inset-0 z-[60] flex items-center justify-center bg-ink/50 p-6"
    >
      <div
        class="motion-rise relative w-full max-w-sm"
        role="dialog"
        aria-modal="true"
        aria-label="批次质检完成"
      >
        <div aria-hidden="true" class="absolute inset-0 translate-x-1.5 translate-y-1.5 rounded-2xl bg-ink"></div>
        <div class="relative space-y-4 rounded-2xl border-2 border-ink bg-surface p-6 text-center">
          <span class="mx-auto flex h-14 w-14 items-center justify-center rounded-2xl border-2 border-ink bg-mint shadow-[3px_3px_0_var(--color-ink)]">
            <svg aria-hidden="true" class="h-7 w-7" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round">
              <path d="M20 6 9 17l-5-5"></path>
            </svg>
          </span>
          <div>
            <h2 class="text-xl font-black tracking-tight">本批次处理完成</h2>
            <p class="mt-1 text-xs font-bold text-ink-muted">本批次已处理完毕，{{ approvedExportCount }} 张通过并可导出；失败和取消项目保留原状态</p>
          </div>
          <p v-if="exportError" role="alert" class="text-xs font-black text-danger">{{ exportError }}</p>
          <button type="button" aria-label="读取已有导出文件" class="min-h-11 font-bold underline" :disabled="exporting" @click="exportApprovedPngs(false, true)">读取已有导出文件</button>
          <div v-if="existingPngs.length || existingZips.length" class="max-h-40 overflow-y-auto rounded-xl border border-ink/20 p-2 text-left text-xs">
            <p class="font-bold">已有导出文件（保留当时的图片版本）</p>
            <button v-for="(artifact, index) in existingPngs" :key="artifact.artifact_id" type="button" :aria-label="`下载已有 PNG ${index + 1}`" class="mr-2 min-h-11 font-bold underline" :disabled="exporting" @click="downloadExistingExport(artifact)">下载图片 {{ index + 1 }}</button>
            <button v-for="(artifact, index) in existingZips" :key="artifact.artifact_id" type="button" :aria-label="`下载已有 ZIP ${index + 1}`" class="mr-2 min-h-11 font-bold underline" :disabled="exporting" @click="downloadExistingExport(artifact)">下载压缩包 {{ index + 1 }}</button>
          </div>
          <p v-if="exportFailures.length" class="max-h-24 overflow-y-auto text-left text-xs font-bold text-danger">
            <span v-for="failure in exportFailures" :key="failure.item_id" class="block">{{ failure.item_id }}：{{ failure.error }}</span>
          </p>
          <button
            type="button"
            class="btn-hard btn-hard--primary w-full"
            :disabled="exporting || approvedExportCount === 0"
            @click="exportApprovedPngs()"
          >
            {{ exporting ? '导出中…' : exportedOnce ? '再次下载已通过成品' : knownExports?.artifacts.length && !knownExports.linked ? '查看已有导出文件' : approvedExportCount === 1 ? '导出 PNG' : '导出 PNG 压缩包' }}
          </button>
          <button type="button" class="btn-hard w-full" @click="router.push('/')">
            返回工作台
          </button>
          <button type="button" class="btn-hard w-full" @click="showComplete = false">
            返回本批次继续处理
          </button>
        </div>
      </div>
    </div>
  </main>
</template>

<!-- 验收: CSS snap横向吸附; 双按钮各约48%宽高56px; 打回理由≥2必填; 回退字形醒目可展开; usePolling代际轮询; 波普圆润风 -->
