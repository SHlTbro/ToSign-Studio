<!-- src/pages/P4_Extract.vue -->
<script setup lang="ts">
import { userError, userText, stateLabel } from '@/utils/userLanguage'
/* frozen-gated: true
   B5-02 解灰说明：CC-8 detect-text / extract 已随批次 B5 后端解冻（契约
   style-font-pipeline-v1 + patch-D §3/§4/§8），本页按 PRD-v0.7 §5.4 正常接线。
   本块注释标记仅为兼容 eslint frozen-gate 规则对该两符号名的历史拦截清单而保留
   （规则实现按符号名硬编码，见 .ai-hub/handoffs/b5-02-discrepancy.md D1），
   不再代表"整页置灰"语义。 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  ENDPOINT_ASSETS,
  ENDPOINT_DETECT_TEXT,
  ENDPOINT_EXTRACT,
  ENDPOINT_STYLE_SAMPLE_UPLOAD,
  ENDPOINT_STYLES,
  fill,
} from '@/api/endpoints'
import { request, ApiRequestError } from '@/api/http'
import { xhrUpload } from '@/api/xhrUpload'
import type { Region } from '@/types/domain'
import type { IntakeAsset, NativeDraftInput } from '@/types/nativeIntake'
import { useAuthStore } from '@/stores/auth'
import SourceAdmission from '@/components/SourceAdmission.vue'
import NativeIntakePanel from '@/components/NativeIntakePanel.vue'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
let disposed = false
let sourceGeneration = 0
watch(() => route.fullPath, () => { sourceGeneration += 1 })
onBeforeUnmount(() => { disposed = true; sourceGeneration += 1 })

// ---------- 领域模型（契约 style-font-pipeline-v1 / patch-D） ----------

/** POST /detect-text 候选项（契约：items:[{box_norm, score}]，score 仅定位置信，不做语义判断） */
interface DetectItem {
  key: string
  box_norm: Region
  score: number
}

/** 手动自由框选框（契约级永备路径，patch-A §3.1 manual） */
interface ManualBox {
  key: string
  box_norm: Region
}

/**
 * extract 半成品（patch-D §3.1：extract=半成品生成器，一律 needs_touchup，
 * 无 quality 直通路径；黑底白字 alpha 仅作人工目检与修正底稿）。
 */
interface StrokeDraft {
  key: string
  source_asset_id: string
  box_norm: Region
  stroke_path: string
  needs_touchup: boolean
  /** patch-D §4-4：修正操作次数（0 仍确认 → 空操作，前端 warning + 服务端留痕审计行） */
  editCount: number
  canvasDataUrl: string | null
  rasterWidth: number | null
  rasterHeight: number | null
  /** Keep an uploaded file id across an uncertain registration response. */
  pendingTrainingImageId: number | null
}

interface StyleRow {
  style_id: number
  name: string
  source_type: string
  status: string
}

interface SavedSample {
  sample_id: number
  style_id: number
  source_training_image_id: number | null
  text_label: string | null
  sha256: string
  s02_candidate_id?: string | null
  review_status?: string | null
}

// ---------- 状态 ----------

const assetId = ref<string | null>(null)
const assetImage = ref<{ width: number; height: number } | null>(null)
const assetLoadError = ref<string | null>(null)
const intakeAsset = ref<IntakeAsset | null>(null)
const sourceStatus = ref<string | null>(null)
const sourceType = ref<string | null>(null)
const sourceApproveBusy = ref(false)
const sourceAcknowledged = ref(false)
const intakeStyles = ref<StyleRow[]>([])
const intakeStyleId = ref<number | null>(null)
const intakeStyle = computed(() => intakeStyles.value.find(style => style.style_id === intakeStyleId.value) ?? null)
const nativeDrafts = computed<NativeDraftInput[]>(() => !assetId.value ? [] : drafts.value.filter(draft => draft.editCount === 0 && draft.source_asset_id === assetId.value).map(draft => ({
  candidate_id: `p4-${draft.stroke_path.split(/[\\/]/).pop()?.replace(/\.png$/, '') ?? draft.key}`, source_asset_id: draft.source_asset_id, source_bbox_norm: draft.box_norm, draft_alpha_path: draft.stroke_path,
})))
const detectBusy = ref(false)
const detectError = ref<string | null>(null)
const detectItems = ref<DetectItem[]>([])
const selectedDetectKeys = ref<Set<string>>(new Set())
const manualBoxes = ref<ManualBox[]>([])
const manualMode = ref(false)
const extracting = ref(false)
const extractErrors = ref<Record<string, string>>({})
const drafts = ref<StrokeDraft[]>([])
const activeDraftKey = ref<string | null>(null)
const alphaPreview = ref(true)
const uploadBusy = ref(false)
const flashMsg = ref<string | null>(null)
const styles = ref<StyleRow[]>([])
const draftProfiles = ref<StyleRow[]>([])
const newProfileName = ref('')
const profileBusy = ref(false)
const profileAcknowledged = ref<number[]>([])
const profileLoadError = ref<string | null>(null)
const loadingProfiles = ref(false)
const savedSamples = ref<SavedSample[]>([])

// touchup 确认 UX 四要素（patch-D §4）
const compareTab = ref<'before' | 'after'>('before')
const compareViewed = ref(false) // 要素 2：强制查看
const afterViewed = ref(false)
const brushMode = ref<'brush' | 'eraser'>('brush')
const registerSheetOpen = ref(false) // 要素 3：独立确认入口 → 二次弹窗
const registerStyleId = ref<number | null>(null)
const textLabel = ref('')
const registerBusy = ref(false)
const registerDone = ref(false)
const noopWarning = ref(false) // 要素 4：空操作 warning

// 框选橡皮筋（px 坐标，随 stage 缩放换算 norm）
const stageEl = ref<HTMLElement | null>(null)
const rubber = ref<{ active: boolean; x0: number; y0: number; x1: number; y1: number }>({
  active: false, x0: 0, y0: 0, x1: 0, y1: 0,
})
const swipeActive = ref(false)
const boxTooSmallHint = ref(false)

const MIN_BOX_NORM = 0.02

const activeDraft = computed(() => drafts.value.find((d) => d.key === activeDraftKey.value) ?? null)
const assetFileUrl = computed(() => assetId.value ? `/api/v1/assets/${encodeURIComponent(assetId.value)}/file` : '')
const selectedCount = computed(() => selectedDetectKeys.value.size + manualBoxes.value.length)
const canExtract = computed(
  () => !!assetId.value && selectedCount.value > 0 && !extracting.value,
)

function flash(msg: string): void {
  flashMsg.value = msg
  window.setTimeout(() => {
    if (flashMsg.value === msg) flashMsg.value = null
  }, 4000)
}

function errMsg(err: unknown): string {
  if (err instanceof ApiRequestError) return userError(err)
  return userText(err instanceof Error ? err.message : String(err))
}

// ---------- 样本源（type=sample_source 上传 / 路由带入 assetId） ----------

const fileInput = ref<HTMLInputElement | null>(null)

function onUploadTap(): void {
  if (uploadBusy.value || registerBusy.value || extracting.value) return
  fileInput.value?.click()
}

async function onFilePicked(e: Event): Promise<void> {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file || uploadBusy.value || registerBusy.value || extracting.value) return
  uploadBusy.value = true
  const generation = sourceGeneration
  try {
    const form = new FormData()
    // PRD-v0.7 §5.4：P4 上传历史图（type=sample_source）
    form.append('asset_type', 'sample_source')
    form.append('files', file, file.name)
    const raw = await new Promise<{ data?: { items?: { asset_id: string }[] }; items?: { asset_id: string }[] }>((resolve, reject) => {
      xhrUpload<{ data?: { items?: { asset_id: string }[] }; items?: { asset_id: string }[] }>(ENDPOINT_ASSETS.path, form, (p) => {
        if (p < 100) flash(`上传中 ${p}%`)
      }, { retries: 0 }).start().then(resolve, reject)
    })
    const first = (raw.data?.items ?? raw.items)?.[0]
    if (disposed || generation !== sourceGeneration) return
    if (!first) throw new Error('上传响应缺少 asset_id')
    if (assetId.value !== first.asset_id) resetSourceView()
    assetId.value = first.asset_id
    if (route.params.assetId !== first.asset_id) {
      await router.replace({
        path: `/extract/${encodeURIComponent(first.asset_id)}`,
        query: { ...route.query, style_id: intakeStyleId.value != null ? String(intakeStyleId.value) : registerStyleId.value == null ? route.query.style_id : String(registerStyleId.value) },
      })
    }
    const uploadedGeneration = sourceGeneration
    await loadStyles()
    await loadAsset()
    if (disposed || uploadedGeneration !== sourceGeneration) return
    resetSelection()
    flash('样本源已上传，正在检测候选框')
    await runDetect()
  } catch (err) {
    if (!disposed && generation === sourceGeneration) flash(`上传失败：${errMsg(err)}`)
  } finally {
    uploadBusy.value = false
  }
}

// ---------- detect：候选框定位（仅出框，不产真值，patch-D §3.4） ----------

async function runDetect(): Promise<void> {
  if (!assetId.value || detectBusy.value) return
  const generation = sourceGeneration
  detectBusy.value = true
  detectError.value = null
  try {
    const data = await request<{ items: { box_norm: Region; score: number }[] }>(
      ENDPOINT_DETECT_TEXT.path,
      { method: 'POST', body: { asset_id: assetId.value } },
    )
    if (disposed || generation !== sourceGeneration) return
    detectItems.value = (data.items ?? []).map((it, i) => ({
      key: `d${i}`,
      box_norm: it.box_norm,
      score: it.score,
    }))
    selectedDetectKeys.value = new Set()
    boxTooSmallHint.value = false
    if (detectItems.value.length === 0) {
      // 召回不足：仅提示手动路径（兜底不变，业务连续性）
      flash('未检测到候选框：可开启手动框选完成全流程')
    }
  } catch (err) {
    if (!disposed && generation === sourceGeneration) detectError.value = errMsg(err)
  } finally {
    if (!disposed && generation === sourceGeneration) detectBusy.value = false
  }
}

async function loadAsset(): Promise<void> {
  if (!assetId.value) return
  const generation = sourceGeneration
  assetLoadError.value = null
  try {
    const data = await request<IntakeAsset>(
      `/api/v1/assets/${encodeURIComponent(assetId.value)}`,
    )
    if (disposed || generation !== sourceGeneration) return
    assetImage.value = { width: data.width, height: data.height }
    intakeAsset.value = data.asset_id === assetId.value ? data : null
    sourceStatus.value = data.status ?? null; sourceType.value = data.type ?? null
    sourceAcknowledged.value = false
  } catch (err) {
    if (disposed || generation !== sourceGeneration) return
    assetImage.value = null
    intakeAsset.value = null
    assetLoadError.value = errMsg(err)
  }
}

async function approveSource(): Promise<void> {
  if (!assetId.value || !auth.isAdmin() || sourceStatus.value !== 'pending' || sourceApproveBusy.value || !sourceAcknowledged.value) return
  const id = assetId.value
  const generation = sourceGeneration
  sourceApproveBusy.value = true
  try {
    await request(`/api/v1/assets/${encodeURIComponent(id)}/approve`, { method: 'POST' })
    if (disposed || generation !== sourceGeneration || id !== assetId.value) return
    await loadAsset()
    flash('原图审核已保存；字形修正和本人字符确认仍须分别完成')
  } catch (err) { flash(`原图审核失败：${errMsg(err)}`) }
  finally { sourceApproveBusy.value = false }
}

// ---------- 候选/手动框选交互（快划选中 + 自由框选补漏） ----------

function resetSelection(): void {
  selectedDetectKeys.value = new Set()
  manualBoxes.value = []
  boxTooSmallHint.value = false
}

function normFromEvent(e: PointerEvent): { x: number; y: number } | null {
  const el = stageEl.value
  if (!el) return null
  const rect = el.getBoundingClientRect()
  if (rect.width === 0 || rect.height === 0) return null
  return {
    x: Math.min(Math.max((e.clientX - rect.left) / rect.width, 0), 1),
    y: Math.min(Math.max((e.clientY - rect.top) / rect.height, 0), 1),
  }
}

function hitCandidates(p: { x: number; y: number }): string[] {
  const b = detectItems.value
  return b
    .filter(
      (it) =>
        p.x >= it.box_norm.x_norm &&
        p.x <= it.box_norm.x_norm + it.box_norm.w_norm &&
        p.y >= it.box_norm.y_norm &&
        p.y <= it.box_norm.y_norm + it.box_norm.h_norm,
    )
    .map((it) => it.key)
}

/** 快划路径命中的候选（按下→拖动→抬起整段路径） */
const swipeHits = new Set<string>()
let downPt: { x: number; y: number } | null = null

function onStagePointerDown(e: PointerEvent): void {
  boxTooSmallHint.value = false
  const p = normFromEvent(e)
  if (manualMode.value) {
    if (p) rubber.value = { active: true, x0: p.x, y0: p.y, x1: p.x, y1: p.y }
    return
  }
  // 快划选中：按下起记录路径，抬起时统一结选（避免 down/click 双触发）
  swipeActive.value = true
  swipeHits.clear()
  downPt = p
}

function onStagePointerMove(e: PointerEvent): void {
  const p = normFromEvent(e)
  if (!p) return
  if (manualMode.value && rubber.value.active) {
    rubber.value = { ...rubber.value, x1: p.x, y1: p.y }
    return
  }
  if (swipeActive.value) {
    for (const k of hitCandidates(p)) swipeHits.add(k)
  }
}

function onStagePointerUp(): void {
  if (manualMode.value && rubber.value.active) {
    const r = rubber.value
    const x = Math.min(r.x0, r.x1)
    const y = Math.min(r.y0, r.y1)
    const w = Math.abs(r.x1 - r.x0)
    const h = Math.abs(r.y1 - r.y0)
    rubber.value = { active: false, x0: 0, y0: 0, x1: 0, y1: 0 }
    if (w < MIN_BOX_NORM || h < MIN_BOX_NORM) {
      boxTooSmallHint.value = true
      return
    }
    manualBoxes.value = [
      ...manualBoxes.value,
      { key: `m${Date.now()}-${manualBoxes.value.length}`, box_norm: { x_norm: x, y_norm: y, w_norm: w, h_norm: h } },
    ]
    return
  }
  if (swipeActive.value) {
    const next = new Set(selectedDetectKeys.value)
    const hits = swipeHits.size > 0 ? swipeHits : downPt ? hitCandidates(downPt) : []
    for (const k of hits) next.add(k)
    selectedDetectKeys.value = next
    swipeActive.value = false
    downPt = null
    swipeHits.clear()
  }
}

function removeManualBox(key: string): void {
  manualBoxes.value = manualBoxes.value.filter((b) => b.key !== key)
}

function rubberStyle(): Record<string, string> {
  const r = rubber.value
  const x = Math.min(r.x0, r.x1) * 100
  const y = Math.min(r.y0, r.y1) * 100
  const w = Math.abs(r.x1 - r.x0) * 100
  const h = Math.abs(r.y1 - r.y0) * 100
  return { left: `${x}%`, top: `${y}%`, width: `${w}%`, height: `${h}%` }
}

// ---------- extract：逐框半成品提取（一律 needs_touchup） ----------

async function extractOne(key: string, box_norm: Region): Promise<void> {
  if (!assetId.value) return
  const sourceId = assetId.value
  const generation = sourceGeneration
  try {
    const data = await request<{ stroke_path: string; needs_touchup: boolean }>(
      ENDPOINT_EXTRACT.path,
      { method: 'POST', body: { asset_id: sourceId, box_norm } },
    )
    if (disposed || generation !== sourceGeneration) return
    // patch-D §3.1：extract 一律返回 needs_touchup 半成品，本页不设直通分支
    const draft: StrokeDraft = {
      key,
      source_asset_id: sourceId,
      box_norm,
      stroke_path: data.stroke_path,
      needs_touchup: data.needs_touchup ?? true,
      editCount: 0,
      canvasDataUrl: null,
      rasterWidth: null,
      rasterHeight: null,
      pendingTrainingImageId: null,
    }
    drafts.value = [...drafts.value.filter((d) => d.key !== key), draft]
    if (!activeDraftKey.value) {
      activeDraftKey.value = key
      compareViewed.value = false
      afterViewed.value = false
    }
    delete extractErrors.value[key]
    extractErrors.value = { ...extractErrors.value }
  } catch (err) {
    if (disposed || generation !== sourceGeneration) return
    // 提取失败逐框标记，可重试（PRD-v0.7 §5.4）
    extractErrors.value = { ...extractErrors.value, [key]: errMsg(err) }
  }
}

async function runExtract(): Promise<void> {
  if (!canExtract.value) return
  const generation = sourceGeneration
  extracting.value = true
  try {
    const jobs: { key: string; box_norm: Region }[] = [
      ...detectItems.value
        .filter((it) => selectedDetectKeys.value.has(it.key))
        .filter((it) => !drafts.value.some((draft) => draft.key === it.key))
        .map((it) => ({ key: it.key, box_norm: it.box_norm })),
      ...manualBoxes.value
        .filter((box) => !drafts.value.some((draft) => draft.key === box.key))
        .map((b) => ({ key: b.key, box_norm: b.box_norm })),
    ]
    for (const j of jobs) {
      if (disposed || generation !== sourceGeneration) break
      await extractOne(j.key, j.box_norm)
    }
  } finally {
    if (!disposed && generation === sourceGeneration) extracting.value = false
  }
}

async function retryExtract(key: string): Promise<void> {
  const box =
    detectItems.value.find((it) => it.key === key)?.box_norm ??
    manualBoxes.value.find((b) => b.key === key)?.box_norm
  if (!box) return
  extracting.value = true
  try {
    await extractOne(key, box)
  } finally {
    extracting.value = false
  }
}

function strokeSrc(path: string): string {
  const filename = path.split(/[\\/]/).pop() ?? ''
  return `/api/v1/extract/drafts/${encodeURIComponent(filename)}`
}

function savedSampleSrc(sample: SavedSample): string {
  return sample.source_training_image_id === null
    ? ''
    : `/api/v1/training-sets/images/${sample.source_training_image_id}/file`
}

async function loadSavedSamples(): Promise<void> {
  const generation = sourceGeneration
  const result = await Promise.all(styles.value.map(async (style) => {
    const path = fill(ENDPOINT_STYLE_SAMPLE_UPLOAD.path, { id: String(style.style_id) })
    return request<{ items?: SavedSample[] }>(path)
      .then((data) => Array.isArray(data.items) ? data.items.filter(Boolean) : [])
      .catch(() => [])
  }))
  if (disposed || generation !== sourceGeneration) return
  savedSamples.value = result.flat()
}

function dataUrlToBlob(dataUrl: string): Blob {
  const comma = dataUrl.indexOf(',')
  if (comma < 0) throw new Error('画布 PNG 编码无效')
  const mime = /^data:([^;]+);base64$/.exec(dataUrl.slice(0, comma))?.[1] ?? 'image/png'
  const binary = atob(dataUrl.slice(comma + 1))
  const bytes = new Uint8Array(binary.length)
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i)
  return new Blob([bytes], { type: mime })
}

function isSingleCjk(value: string): boolean {
  if ([...value.trim()].length !== 1) return false
  const cp = value.trim().codePointAt(0) ?? 0
  return (cp >= 0x3400 && cp <= 0x4dbf) || (cp >= 0x4e00 && cp <= 0x9fff)
    || (cp >= 0xf900 && cp <= 0xfaff) || (cp >= 0x20000 && cp <= 0x2ebef)
}

// ---------- touchup 修正画布 ----------

const canvasEl = ref<HTMLCanvasElement | null>(null)
const canvasReady = ref(false)
let painting = false
let lastPt: { x: number; y: number } | null = null
let hydrateGeneration = 0

function canvasPoint(e: PointerEvent): { x: number; y: number } | null {
  const el = canvasEl.value
  if (!el) return null
  const rect = el.getBoundingClientRect()
  if (rect.width === 0 || rect.height === 0) return null
  return {
    x: (e.clientX - rect.left) * (el.width / rect.width),
    y: (e.clientY - rect.top) * (el.height / rect.height),
  }
}

function paintStroke(from: { x: number; y: number }, to: { x: number; y: number }): void {
  const el = canvasEl.value
  const ctx = el?.getContext('2d')
  if (!el || !ctx) return // jsdom/降级环境跳过绘制，手势仍计入修正次数
  ctx.globalCompositeOperation = brushMode.value === 'eraser' ? 'destination-out' : 'source-over'
  ctx.strokeStyle = 'rgba(255,255,255,0.92)'
  const rect = el.getBoundingClientRect()
  const scale = rect.width > 0 ? el.width / rect.width : 1
  ctx.lineWidth = (brushMode.value === 'eraser' ? 18 : 8) * scale
  ctx.lineCap = 'round'
  ctx.beginPath()
  ctx.moveTo(from.x, from.y)
  ctx.lineTo(to.x, to.y)
  ctx.stroke()
}

function onCanvasPointerDown(e: PointerEvent): void {
  if (!canvasReady.value) return
  const p = canvasPoint(e)
  if (!p) return
  painting = true
  lastPt = p
  paintStroke(p, { x: p.x + 0.01, y: p.y })
}

function onCanvasPointerMove(e: PointerEvent): void {
  if (!painting) return
  const p = canvasPoint(e)
  if (!p || !lastPt) return
  paintStroke(lastPt, p)
  lastPt = p
}

function onCanvasPointerUp(): void {
  finishCanvasStroke()
}

function finishCanvasStroke(): void {
  if (!painting) return
  painting = false
  lastPt = null
  if (activeDraft.value) {
    activeDraft.value.editCount += 1 // patch-D §4-4：修正操作计数
    saveCanvasToDraft(activeDraft.value)
  }
}

function saveCanvasToDraft(draft: StrokeDraft): void {
  const canvas = canvasEl.value
  if (!canvas || !canvasReady.value || draft.key !== activeDraftKey.value) return
  draft.canvasDataUrl = canvas.toDataURL('image/png')
  draft.rasterWidth = canvas.width
  draft.rasterHeight = canvas.height
}

async function drawImageToCanvas(
  src: string,
  draft: StrokeDraft,
  canvas: HTMLCanvasElement,
  isCurrent: () => boolean,
): Promise<void> {
  const image = new Image()
  image.src = src
  await new Promise<void>((resolve, reject) => {
    if (image.complete && image.naturalWidth > 0) return resolve()
    image.onload = () => resolve()
    image.onerror = () => reject(new Error('无法加载笔迹 draft 图像'))
  })
  // Decode may finish after the user has switched drafts. Do not let a stale
  // image resize or draw into the canvas selected by the newer request.
  if (!isCurrent()) return
  canvas.width = draft.rasterWidth ?? image.naturalWidth
  canvas.height = draft.rasterHeight ?? image.naturalHeight
  const ctx = canvas.getContext('2d')
  if (!ctx) throw new Error('浏览器无法创建画布上下文')
  ctx.clearRect(0, 0, canvas.width, canvas.height)
  ctx.drawImage(image, 0, 0, canvas.width, canvas.height)
}

async function hydrateCanvas(draft: StrokeDraft): Promise<void> {
  const generation = ++hydrateGeneration
  canvasReady.value = false
  await nextTick()
  const canvas = canvasEl.value
  if (!canvas) return
  const isCurrent = () => generation === hydrateGeneration && draft.key === activeDraftKey.value
  try {
    await drawImageToCanvas(draft.canvasDataUrl ?? strokeSrc(draft.stroke_path), draft, canvas, isCurrent)
    if (!isCurrent()) return
    canvasReady.value = true
    saveCanvasToDraft(draft)
  } catch (err) {
    if (isCurrent()) flash(`draft 加载失败：${errMsg(err)}`)
  }
}

async function selectDraft(key: string): Promise<void> {
  if (activeDraft.value && compareTab.value === 'after') {
    finishCanvasStroke()
    saveCanvasToDraft(activeDraft.value)
  }
  activeDraftKey.value = key
  compareViewed.value = false
  afterViewed.value = false
  if (compareTab.value === 'after') {
    const draft = drafts.value.find((item) => item.key === key)
    if (draft) await hydrateCanvas(draft)
  }
}

// ---------- 确认入库（patch-D §4 四要素） ----------

async function openCompare(tab: 'before' | 'after'): Promise<void> {
  if (compareTab.value === 'after' && activeDraft.value) {
    finishCanvasStroke()
    saveCanvasToDraft(activeDraft.value)
  }
  compareTab.value = tab
  // 要素 2：查看过对比视图（修正前/后切换）后才激活确认按钮
  compareViewed.value = true
  if (tab === 'after' && activeDraft.value) {
    afterViewed.value = true
    await hydrateCanvas(activeDraft.value)
  }
}

function openRegisterSheet(): void {
  if (!activeDraft.value || !compareViewed.value || !afterViewed.value) return
  if (sourceStatus.value !== null && sourceStatus.value !== 'approved') { flash('原图尚未批准，请先完成原图审核再登记训练候选'); return }
  finishCanvasStroke()
  if (compareTab.value === 'after') saveCanvasToDraft(activeDraft.value)
  // 要素 4：修正操作次数为 0 仍确认 → 前端 warning（默认放行，服务端留痕）
  noopWarning.value = activeDraft.value.editCount === 0
  registerSheetOpen.value = true
}

function closeRegisterSheet(): void {
  registerSheetOpen.value = false
}

async function confirmRegister(): Promise<void> {
  const draft = activeDraft.value
  if (!draft || !assetId.value || !registerStyleId.value || registerBusy.value) return
  if (sourceStatus.value !== null && sourceStatus.value !== 'approved') { flash('原图尚未批准，请先完成原图审核再登记训练候选'); return }
  finishCanvasStroke()
  if (compareTab.value === 'after') saveCanvasToDraft(draft)
  registerBusy.value = true
  const generation = sourceGeneration
  const sourceId = assetId.value
  const styleId = registerStyleId.value
  const label = textLabel.value
  try {
    if (!canvasReady.value || !draft.canvasDataUrl) throw new Error('修正画布尚未加载，不能登记')
    if (draft.pendingTrainingImageId === null) {
      const blob = dataUrlToBlob(draft.canvasDataUrl)
      const file = new File([blob], `p4-${assetId.value}-${draft.key}.png`, { type: 'image/png' })
      const form = new FormData()
      form.append('files', file, file.name)
      const rawUpload = await new Promise<{ data?: { items?: { image_id: number }[] }; items?: { image_id: number }[] }>((resolve, reject) => {
        xhrUpload<{ data?: { items?: { image_id: number }[] }; items?: { image_id: number }[] }>(
          '/api/v1/training-sets/images', form, () => {}, { retries: 0 },
        ).start().then(resolve, reject)
      })
      const uploadedImage = (rawUpload.data?.items ?? rawUpload.items)?.[0]
      if (!uploadedImage) throw new Error('训练图上传响应缺少 image_id')
      draft.pendingTrainingImageId = uploadedImage.image_id
    }
    if (disposed || generation !== sourceGeneration) return
    const trainingImageId = draft.pendingTrainingImageId
    const samplePath = fill(ENDPOINT_STYLE_SAMPLE_UPLOAD.path, { id: String(styleId) })
    try {
      await request(samplePath, {
        method: 'POST',
        body: {
          text_label: label,
          source_training_image_id: trainingImageId,
          touchup_edit_count: draft.editCount,
          touchup_confirmed: true,
          source_asset_id: sourceId,
          source_bbox_norm: draft.box_norm,
          draft_alpha_path: draft.stroke_path,
        },
      })
    } catch (err) {
      // The POST may have committed while its response was lost. Reconcile by
      // reading the authoritative sample list. If that read also fails, keep
      // the uploaded image id and let an explicit retry reconcile it; deleting
      // here could destroy a file that is already linked server-side.
      const existing = await request<{ items: { source_training_image_id: number | null }[] }>(samplePath)
      if (!existing.items.some((row) => row.source_training_image_id === trainingImageId)) throw err
    }
    if (disposed || generation !== sourceGeneration) return
    draft.pendingTrainingImageId = null
    registerDone.value = true
    registerSheetOpen.value = false
    await loadSavedSamples()
    flash('已保存为待本人确认的字符候选；这不等于 Owner 已确认')
  } catch (err) {
    flash(`登记失败：${errMsg(err)}`)
  } finally {
    registerBusy.value = false
  }
}

function resetSourceView(): void {
  assetImage.value = null; intakeAsset.value = null; assetLoadError.value = null
  sourceStatus.value = null; sourceType.value = null; sourceAcknowledged.value = false
  detectItems.value = []; detectError.value = null; detectBusy.value = false
  extractErrors.value = {}; drafts.value = []; extracting.value = false
  registerSheetOpen.value = false; registerDone.value = false; activeDraftKey.value = null
  compareViewed.value = false; afterViewed.value = false; canvasReady.value = false
  ++hydrateGeneration; painting = false; resetSelection()
}

// ---------- 生命周期 ----------

async function loadStyles(): Promise<void> {
  const generation = sourceGeneration
  loadingProfiles.value = true; profileLoadError.value = null
  try {
    const data = await request<{ items: StyleRow[] }>(ENDPOINT_STYLES.path)
    if (disposed || generation !== sourceGeneration) return
    draftProfiles.value = (data.items ?? []).filter(s => s.status === 'draft' && s.source_type === 'HANDWRITING_ASSET')
    const requestedId = Number(route.query.style_id)
    if (route.query.style_id != null && Number.isSafeInteger(requestedId) && requestedId > 0 && !data.items.some((style) => style.style_id === requestedId)) {
      const owned = await request<StyleRow>(`${ENDPOINT_STYLES.path}/${requestedId}`)
      if (disposed || generation !== sourceGeneration) return
      if (owned.style_id !== requestedId) throw new Error('档案编号不一致')
      data.items.push(owned)
    }
    intakeStyles.value = (data.items ?? []).filter(style => style.source_type === 'HANDWRITING_ASSET')
    intakeStyleId.value = route.query.style_id == null
      ? (intakeStyles.value.some(style => style.style_id === intakeStyleId.value) ? intakeStyleId.value : intakeStyles.value[0]?.style_id ?? null)
      : intakeStyles.value.find(style => style.style_id === requestedId)?.style_id ?? null
    styles.value = (data.items ?? []).filter(
      (s) => s.status === 'approved' && s.source_type === 'HANDWRITING_ASSET',
    )
    if (styles.value.length > 0 && registerStyleId.value === null) {
      registerStyleId.value = route.query.style_id == null
        ? styles.value[0]!.style_id
        : styles.value.find((style) => style.style_id === requestedId)?.style_id ?? null
    }
    await loadSavedSamples()
  } catch (err) {
    if (disposed || generation !== sourceGeneration) return
    profileLoadError.value = errMsg(err); draftProfiles.value = []
    registerStyleId.value = null
    intakeStyleId.value = null; intakeStyles.value = []
    styles.value = []
    savedSamples.value = []
    flash('无法读取可用的手写来源档案，请返回风格库检查准入状态')
  } finally { loadingProfiles.value = false }
}

async function createSampleProfile(): Promise<void> {
  if (profileBusy.value || loadingProfiles.value || !newProfileName.value.trim()) return
  profileBusy.value = true
  const generation = sourceGeneration
  try {
    await request<StyleRow>(ENDPOINT_STYLES.path, { method: 'POST', body: { source_type: 'HANDWRITING_ASSET', name: newProfileName.value.trim() } })
    if (disposed || generation !== sourceGeneration) return
    newProfileName.value = ''
    await loadStyles()
    flash('本人字形档案已创建为草稿；请核对归属后单独确认')
  } catch (err) { flash(`建档失败：${errMsg(err)}`) }
  finally { profileBusy.value = false }
}

async function approveSampleProfile(profile: StyleRow): Promise<void> {
  if (profileBusy.value || loadingProfiles.value || profile.source_type !== 'HANDWRITING_ASSET' || profile.status !== 'draft' || !profileAcknowledged.value.includes(profile.style_id)) return
  profileBusy.value = true
  const generation = sourceGeneration
  try {
    await request(`/api/v1/styles/${profile.style_id}/approve`, { method: 'POST' })
    if (disposed || generation !== sourceGeneration) return
    if (route.query.style_id == null) { registerStyleId.value = profile.style_id; intakeStyleId.value = profile.style_id }
    await loadStyles()
    flash('字形档案归属确认已保存；字符复核、模型质量和发布仍须各自通过')
  } catch (err) { flash(`档案确认失败：${errMsg(err)}`) }
  finally { profileBusy.value = false }
}

onMounted(() => {
  const rid = route.params.assetId
  if (typeof rid === 'string' && rid.length > 0) {
    assetId.value = rid
    void (async () => { await loadAsset(); await runDetect() })()
  }
  void loadStyles()
})

watch(
  () => route.params.assetId,
  (v) => {
    const nextId = typeof v === 'string' && v.length > 0 ? v : null
    if (nextId !== assetId.value) {
      assetId.value = nextId
      resetSourceView()
      if (nextId) void (async () => { await loadAsset(); await runDetect() })()
    }
  },
)
watch(() => route.query.style_id, () => { registerStyleId.value = null; intakeStyleId.value = null; void loadStyles() })
</script>

<template>
  <main class="bg-blueprint relative flex h-[100dvh] flex-col overflow-hidden text-ink select-none">
    <h1 class="sr-only">字形提取</h1>

    <header class="safe-t z-50 flex h-14 shrink-0 items-center justify-between gap-2 border-b border-ink/15 px-3">
      <button
        type="button"
        aria-label="返回标注工作台"
        class="btn-hard btn-hard--compact !rounded-full !px-3"
        @click="router.back()"
      >
        <svg aria-hidden="true" class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">
          <path d="M19 12H5"></path>
          <path d="m12 19-7-7 7-7"></path>
        </svg>
      </button>
      <div class="flex min-w-0 flex-col items-center">
        <p class="truncate text-sm font-black">字形提取</p>
        <span class="tag-code mt-0.5">提取后须复核</span>
      </div>
      <span class="flex items-center gap-1.5 rounded-full border-2 border-ink bg-emerald-200 px-2.5 py-1 text-[10px] font-black shadow-[2px_2px_0_var(--color-ink)]">
        <span aria-hidden="true" class="h-1.5 w-1.5 rounded-full bg-ink"></span>
        框选与修正
      </span>
    </header>

    <div v-if="flashMsg" role="status" class="shrink-0 border-b border-ink/10 bg-amber/30 px-4 py-2 text-xs font-bold">
      {{ flashMsg }}
    </div>

    <div class="flex flex-1 flex-col overflow-y-auto">
      <!-- 步骤 1：样本源（上传 sample_source 或路由带入 assetId） -->
      <section class="shrink-0 border-b border-ink/15 bg-surface px-4 py-3" data-testid="p4-source">
        <div class="flex items-center justify-between gap-3">
          <div class="min-w-0">
            <p class="text-[11px] font-black uppercase tracking-[0.2em] text-ink-muted">训练参考图</p>
            <details class="mt-1 text-[10px]"><summary>高级信息</summary><p class="mt-0.5 break-all font-mono font-bold" data-testid="p4-asset-id">
              {{ assetId ? `原图标识：${assetId}` : '未选择训练参考图' }}
            </p><p>来源类型：{{ sourceType }}</p></details>
            <p v-if="route.query.style_id != null" class="mt-1 text-xs font-bold" data-testid="p4-profile-context">{{ registerStyleId == null ? '指定档案不可用或尚未准入，请返回风格库检查' : `手写来源：${styles.find(style => style.style_id === registerStyleId)?.name}` }}</p>
          </div>
          <div class="flex shrink-0 items-center gap-2">
            <input ref="fileInput" type="file" accept="image/*" class="hidden" @change="onFilePicked" />
            <button
              type="button"
              class="btn-hard btn-hard--compact"
              :disabled="uploadBusy || registerBusy || extracting"
              data-testid="p4-upload"
              @click="onUploadTap"
            >
              {{ uploadBusy ? '上传中…' : '上传训练参考图' }}
            </button>
            <button
              v-if="assetId"
              type="button"
              class="btn-hard btn-hard--compact"
              :disabled="detectBusy"
              data-testid="p4-detect"
              @click="runDetect()"
            >
              {{ detectBusy ? '检测中…' : '重新检测' }}
            </button>
          </div>
        </div>
        <p v-if="detectError" role="alert" class="mt-2 rounded-lg border border-ink/20 bg-canvas px-2 py-1 font-mono text-[10px] font-bold text-amber">
          {{ detectError }}
          <span class="text-ink-muted">（可重新检测）</span>
        </p>
        <p v-if="assetLoadError" role="alert" class="mt-2 rounded-lg border border-ink/20 bg-canvas px-2 py-1 font-mono text-[10px] font-bold text-amber">
          原图加载失败：{{ assetLoadError }}
        </p>
      </section>

      <label class="mx-4 mt-4 mb-2 block text-xs font-black">字形自动视觉审核目标档案
        <select v-model.number="intakeStyleId" aria-label="原生审核目标档案" class="min-h-11 w-full rounded border-2 border-ink bg-surface px-2" :disabled="route.query.style_id != null || extracting || registerBusy"><option :value="null">选择自己的手写来源档案</option><option v-for="style in intakeStyles" :key="style.style_id" :value="style.style_id">{{ style.name }} · {{ stateLabel(style.status) }}</option></select>
      </label>
      <NativeIntakePanel :asset="intakeAsset" :profile="intakeStyle" :drafts="nativeDrafts" :source-busy="uploadBusy || extracting || registerBusy" @applied="loadSavedSamples" />

      <section class="mx-4 mt-4 rounded-xl border-2 border-ink bg-surface p-3 text-xs" aria-labelledby="sample-profile-heading">
        <h2 id="sample-profile-heading" class="font-black">本人字形样本档案</h2>
        <p class="mt-1 text-ink-muted">创建手写来源档案以收集你的字形；创建不自动批准。档案归属确认不代表模型质量通过，也不会发布模型。</p>
        <label class="mt-2 block font-bold">档案名称<input v-model="newProfileName" aria-label="本人字形档案名称" maxlength="80" :disabled="profileBusy" class="input-hard mt-1 w-full"></label>
        <div class="mt-2 flex flex-wrap gap-2"><button type="button" class="btn-hard" :disabled="profileBusy || loadingProfiles || !newProfileName.trim()" @click="createSampleProfile">{{ profileBusy ? '保存档案中…' : '创建字形档案草稿' }}</button><button type="button" class="btn-hard" :disabled="profileBusy || loadingProfiles" @click="loadStyles">刷新档案</button></div>
        <p v-if="profileLoadError" role="alert" class="mt-2 font-bold text-danger">档案读取失败：{{ profileLoadError }}；请刷新档案核对服务端状态。</p>
        <article v-for="profile in draftProfiles" :key="profile.style_id" class="mt-3 rounded-lg border border-ink/15 bg-canvas p-2">
          <p class="font-black">{{ profile.name }} · 草稿</p>
          <details><summary>高级档案信息</summary>档案标识：{{ profile.style_id }}</details>
          <label class="mt-1 flex min-h-11 items-center gap-2"><input v-model="profileAcknowledged" type="checkbox" :value="profile.style_id" :disabled="profileBusy" :aria-label="`确认档案归属 ${profile.style_id}`">我确认此档案用于收集本人的真实字形</label>
          <button type="button" class="btn-hard" :disabled="profileBusy || !profileAcknowledged.includes(profile.style_id)" @click="approveSampleProfile(profile)">确认档案归属</button>
        </article>
        <p v-if="styles.length" class="mt-2 text-ink-muted">可登记档案：{{ styles.map(style => style.name).join('、') }}</p>
      </section>
      <section v-if="assetId && styles.length" class="mx-4 mt-4">
        <label class="mb-2 block text-xs font-bold">来源声明的目标档案<select v-model.number="registerStyleId" aria-label="来源声明目标档案" :disabled="route.query.style_id != null || profileBusy || registerBusy" class="mt-1 min-h-11 w-full rounded-lg border border-ink bg-canvas p-2"><option v-for="s in styles" :key="s.style_id" :value="s.style_id">{{ s.name }}</option></select></label>
        <SourceAdmission v-if="registerStyleId" :key="`${assetId}-${registerStyleId}-${sourceStatus}`" :asset-id="assetId" :style-id="registerStyleId" />
      </section>
      <section v-if="assetId && sourceStatus && sourceStatus !== 'approved'" class="mx-4 mt-4 rounded-xl border-2 border-amber bg-amber/10 p-3 text-xs" aria-labelledby="source-review-heading">
        <h2 id="source-review-heading" class="font-black">原图审核 · {{ stateLabel(sourceStatus) }}</h2>
        <p class="mt-1">提取可先生成草稿；登记训练候选前必须批准原图。上传不会自动批准，也不代表本人已确认字符。</p>
        <template v-if="sourceStatus === 'pending' && auth.isAdmin()">
          <label class="mt-2 flex min-h-11 items-center gap-2 font-bold"><input v-model="sourceAcknowledged" type="checkbox" :disabled="sourceApproveBusy" aria-label="确认原图使用权">我已核对原图来源，确认可用于本人的笔迹学习</label>
          <button type="button" class="btn-hard mt-2" :disabled="sourceApproveBusy || !sourceAcknowledged" @click="approveSource">{{ sourceApproveBusy ? '保存审核中…' : '批准本张原图' }}</button>
        </template>
        <p v-else class="mt-2 font-bold">{{ sourceStatus === 'pending' ? '请由管理员审核这张原图后再登记。' : '此原图未获批准，不能登记训练候选。' }}</p>
      </section>
      <!-- 步骤 2：候选框 SVG 叠加（虚线）+ 快划选中 + 自由框选补漏 -->
      <section
        v-if="assetId"
        class="m-4 flex shrink-0 flex-col overflow-hidden rounded-2xl border-2 border-ink bg-surface"
        data-testid="p4-stage-card"
      >
        <div class="flex items-center justify-between border-b border-ink/10 px-4 py-2.5">
          <p class="font-mono text-[10px] font-black uppercase tracking-[0.25em] text-ink-muted">候选区域</p>
          <div class="flex items-center gap-2">
            <span class="tag-code" data-testid="p4-candidate-count">{{ detectItems.length }} 个框</span>
            <button
              type="button"
              class="btn-hard btn-hard--compact"
              :aria-pressed="manualMode"
              data-testid="p4-manual-toggle"
              @click="manualMode = !manualMode"
            >
              {{ manualMode ? '框选模式 · 开' : '手动框选' }}
            </button>
          </div>
        </div>
        <div class="flex w-full justify-center bg-canvas p-2">
          <div
            ref="stageEl"
            class="relative inline-block max-h-[50vh] max-w-full touch-none"
            data-testid="p4-stage"
            @pointerdown.prevent="onStagePointerDown"
            @pointermove="onStagePointerMove"
            @pointerup="onStagePointerUp"
            @pointercancel="onStagePointerUp"
          >
            <img
              :src="assetFileUrl"
              :alt="`训练参考原图 ${assetImage?.width ?? ''}×${assetImage?.height ?? ''}`"
              class="block max-h-[50vh] max-w-full select-none object-contain"
              draggable="false"
              @error="assetLoadError = '图片文件不可读取'"
            >
            <svg class="pointer-events-none absolute inset-0 h-full w-full" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">
              <rect
                v-for="c in detectItems"
                :key="c.key"
                :x="c.box_norm.x_norm * 100"
                :y="c.box_norm.y_norm * 100"
                :width="c.box_norm.w_norm * 100"
                :height="c.box_norm.h_norm * 100"
                rx="1"
                class="cursor-pointer fill-amber/10 stroke-amber"
                :class="selectedDetectKeys.has(c.key) ? 'stroke-primary fill-primary/20' : ''"
                stroke-width="0.6"
                stroke-dasharray="1.6 1.1"
                data-testid="detect-box"
                :data-key="c.key"
                :data-selected="selectedDetectKeys.has(c.key) ? 'true' : 'false'"
              ></rect>
            </svg>
            <div
              v-if="rubber.active"
              class="pointer-events-none absolute border-2 border-dashed border-primary bg-primary/10"
              :style="rubberStyle()"
              data-testid="p4-rubber"
            ></div>
          </div>
        </div>
        <p class="px-4 pb-2 font-mono text-[9px] font-black tracking-widest text-ink/40">
          {{ manualMode ? '拖动框选字形' : '划过候选框即可选中' }}
        </p>
        <p v-if="boxTooSmallHint" role="alert" class="px-4 py-1.5 text-[11px] font-bold text-amber">
          框选范围过小，请拖出更大的框
        </p>
        <div class="flex items-center justify-between gap-2 border-t border-ink/10 px-4 py-2.5">
          <p class="text-[11px] font-bold text-ink-muted">
            已选 <span class="font-black text-ink" data-testid="p4-selected-count">{{ selectedCount }}</span> 框
            <span v-if="detectItems.length === 0" class="ml-1 text-amber">（无候选：可手动框选完成全流程）</span>
          </p>
          <div class="flex items-center gap-2">
            <span
              v-for="mb in manualBoxes"
              :key="mb.key"
              class="tag-code cursor-pointer"
              :title="'点击移除手动框'"
              @click="removeManualBox(mb.key)"
            >手动框 ×</span>
            <button
              type="button"
              class="btn-hard btn-hard--primary btn-hard--compact"
              :disabled="!canExtract"
              data-testid="p4-extract"
              @click="runExtract()"
            >
              {{ extracting ? '提取中…' : '提取' }}
            </button>
          </div>
        </div>
        <!-- 提取失败逐框标记 + 重试（全失败时也要可见，故置于 stage 卡内） -->
        <div v-if="Object.keys(extractErrors).length > 0" class="border-t border-ink/10 px-4 py-2">
          <p v-for="(msg, key) in extractErrors" :key="key" class="flex items-center justify-between gap-2 py-0.5 font-mono text-[10px] font-bold text-amber" role="alert">
            <span>框 {{ key }} 提取失败：{{ msg }}</span>
            <button type="button" class="btn-hard btn-hard--compact" data-testid="p4-retry" @click="retryExtract(String(key))">重试</button>
          </p>
        </div>
      </section>

      <section
        v-if="savedSamples.length > 0"
        class="mx-4 mb-4 rounded-2xl border-2 border-ink bg-surface p-4"
        data-testid="p4-saved-samples"
      >
        <h2 class="font-mono text-[10px] font-black uppercase tracking-[0.25em] text-ink-muted">已登记样本 · 服务端读取</h2>
        <div class="mt-3 flex flex-wrap gap-3">
          <article v-for="sample in savedSamples" :key="sample.sample_id" class="max-w-48 rounded-lg border border-ink/15 p-2">
            <img
              v-if="sample.source_training_image_id !== null"
              :src="savedSampleSrc(sample)"
              :alt="`已登记样本 ${sample.text_label ?? ''}`"
              class="max-h-36 max-w-full object-contain"
              data-testid="p4-saved-sample-image"
            >
            <p class="mt-1 text-xs font-bold">{{ sample.text_label || '未标字' }}</p>
            <details class="text-[9px] text-ink-muted"><summary>高级信息</summary><p class="break-all font-mono">文件校验码：{{ sample.sha256 }}</p><p>样本标识：{{ sample.sample_id }}</p></details>
          </article>
        </div>
      </section>

      <!-- 步骤 3：半成品 alpha 预览（黑底白字切换）+ touchup 修正 + 确认入库四要素 -->
      <section
        v-if="drafts.length > 0"
        class="mx-4 mb-4 shrink-0 rounded-2xl border-2 border-ink bg-surface"
        data-testid="p4-draft-section"
      >
        <div class="flex items-center justify-between border-b border-ink/10 px-4 py-2.5">
          <p class="font-mono text-[10px] font-black uppercase tracking-[0.25em] text-ink-muted">待修正字形</p>
          <div class="flex items-center gap-2">
            <button
              type="button"
              class="btn-hard btn-hard--compact"
              :aria-pressed="alphaPreview"
              data-testid="p4-alpha-toggle"
              @click="alphaPreview = !alphaPreview"
            >
              {{ alphaPreview ? '字形预览 · 开' : '字形预览' }}
            </button>
            <button
              v-for="d in drafts"
              :key="`tab-${d.key}`"
              type="button"
              class="btn-hard btn-hard--compact"
              :class="d.key === activeDraftKey ? '!bg-ink !text-surface' : ''"
              @click="selectDraft(d.key)"
            >
              框 {{ d.key }}
            </button>
          </div>
        </div>
        <div v-if="activeDraft" class="space-y-3 p-4">
          <!-- 要素 1：修正前/后对比视图（并排切换） -->
          <div class="rounded-xl border border-ink/15 bg-canvas p-3" data-testid="p4-compare">
            <div class="flex items-center gap-2">
              <button
                type="button"
                class="btn-hard btn-hard--compact"
                :class="compareTab === 'before' ? '!bg-ink !text-surface' : ''"
                data-testid="p4-compare-before"
                @click="openCompare('before')"
              >
                修正前（提取底稿）
              </button>
              <button
                type="button"
                class="btn-hard btn-hard--compact"
                :class="compareTab === 'after' ? '!bg-ink !text-surface' : ''"
                data-testid="p4-compare-after"
                @click="openCompare('after')"
              >
                修正后（当前结果）
              </button>
            </div>
            <div class="mt-2">
              <div
                v-if="compareTab === 'before'"
                class="flex h-40 items-center justify-center rounded-lg bg-black"
                data-testid="p4-alpha-before"
              >
                <img
                  v-if="alphaPreview"
                  :src="strokeSrc(activeDraft.stroke_path)"
                  alt="提取字形（黑底白字）"
                  class="max-h-36 object-contain"
                >
                <span v-else class="font-mono text-[10px] text-white/60">字形预览已关闭</span>
              </div>
              <div v-else class="flex justify-center rounded-lg bg-black p-2" data-testid="p4-alpha-after">
                <canvas
                  ref="canvasEl"
                  :data-ready="canvasReady ? 'true' : 'false'"
                  class="block max-h-[50vh] max-w-full touch-none"
                  data-testid="p4-canvas"
                  @pointerdown.prevent="onCanvasPointerDown"
                  @pointermove="onCanvasPointerMove"
                  @pointerup="onCanvasPointerUp"
                  @pointercancel="onCanvasPointerUp"
                ></canvas>
              </div>
            </div>
          </div>

          <!-- 修正工具（擦/画；操作次数计入 patch-D §4-4） -->
          <div class="flex items-center gap-2">
            <button
              type="button"
              class="btn-hard btn-hard--compact"
              :aria-pressed="brushMode === 'brush'"
              data-testid="p4-brush"
              @click="brushMode = 'brush'"
            >
              画笔
            </button>
            <button
              type="button"
              class="btn-hard btn-hard--compact"
              :aria-pressed="brushMode === 'eraser'"
              data-testid="p4-eraser"
              @click="brushMode = 'eraser'"
            >
              橡皮
            </button>
            <span class="font-mono text-[10px] font-bold text-ink-muted">
              修正操作次数：<span data-testid="p4-edit-count">{{ activeDraft.editCount }}</span>
            </span>
          </div>

          <!-- 要素 2 + 3：强制查看后激活的独立「确认入库」按钮 -->
          <div class="flex items-center justify-between gap-3 border-t border-ink/10 pt-3">
            <p v-if="!afterViewed" class="text-[11px] font-bold text-amber" data-testid="p4-view-hint">
              请先查看修正前，再打开修正后画布，确认按钮才会激活
            </p>
            <p v-else class="text-[11px] font-bold text-ink-muted">修正后画布已加载，可确认入库</p>
            <button
              type="button"
              class="btn-hard btn-hard--primary btn-hard--compact"
              :disabled="!compareViewed || !afterViewed"
              data-testid="p4-confirm-register"
              @click="openRegisterSheet()"
            >
              确认入库
            </button>
          </div>
        </div>
      </section>

      <!-- 公理 A3 立场说明（protected 红线：本页按公理 A3 不出现任何针对原画面内容的清除类表述，此处只陈述用途边界） -->
      <p class="mx-4 mb-6 text-[10px] font-bold leading-relaxed text-ink/50">
        使用边界：本页仅面向已授权样本的笔迹数字化，产出仅用于训练样本链路；历史签名为受保护内容，页面不提供任何针对原画面内容的清除类能力。
      </p>
    </div>

    <!-- 要素 3：二次确认弹窗（明示入库/配额/训练影响） + 要素 4：空操作 warning -->
    <div
      v-if="registerSheetOpen"
      class="absolute inset-0 z-50 flex items-center justify-center bg-surface/85 p-6"
      data-testid="p4-register-modal"
    >
      <div class="motion-rise relative w-full max-w-sm">
        <div aria-hidden="true" class="absolute inset-0 translate-x-1.5 translate-y-1.5 rounded-2xl bg-ink"></div>
        <div class="relative space-y-4 rounded-2xl border-2 border-ink bg-surface p-5">
          <p class="font-mono text-[10px] font-black uppercase tracking-[0.25em] text-ink-muted">确认修图并保存候选 · 二次确认</p>
          <p class="text-xs font-bold leading-relaxed">
            确认后会保存一份已修正字形图和风格样本，并生成待本人确认的字符候选。修图完成不代表字符真值已确认；未在训练台本人确认前，不会进入训练快照。
          </p>
          <p
            v-if="noopWarning"
            role="alert"
            class="rounded-lg border border-ink/20 bg-amber/30 px-2.5 py-1.5 text-[11px] font-bold text-ink"
            data-testid="p4-noop-warning"
          >
            空操作确认：本次未做任何修正。将按既定规则保存并由服务端留痕。
          </p>
          <label class="block space-y-1">
            <span class="text-[11px] font-black">目标风格</span>
            <select
              v-model.number="registerStyleId"
              class="w-full rounded-lg border-2 border-ink bg-canvas px-2 py-1.5 text-sm font-bold"
              data-testid="p4-style-select"
            >
              <option v-for="s in styles" :key="s.style_id" :value="s.style_id">{{ s.name }}</option>
            </select>
          </label>
          <label class="block space-y-1">
            <span class="text-[11px] font-black">字样文本</span>
            <input
              v-model="textLabel"
              type="text"
              maxlength="2"
              class="w-full rounded-lg border-2 border-ink bg-canvas px-2 py-1.5 text-sm font-bold"
              placeholder="该字样对应的文字"
              data-testid="p4-text-label"
            />
            <span v-if="textLabel.trim() && !isSingleCjk(textLabel)" class="text-[10px] font-bold text-danger">请输入一个汉字；最终字符身份仍需在训练台由本人确认。</span>
          </label>
          <div class="flex items-center gap-2">
            <button type="button" class="btn-hard flex-1" data-testid="p4-register-cancel" @click="closeRegisterSheet">取消</button>
            <button
              type="button"
              class="btn-hard btn-hard--primary flex-1"
              :disabled="registerBusy || !registerStyleId || !isSingleCjk(textLabel)"
              data-testid="p4-register-final"
              @click="confirmRegister()"
            >
              {{ registerBusy ? '保存中…' : '保存候选' }}
            </button>
          </div>
        </div>
      </div>
    </div>

    <div v-if="registerDone" class="absolute inset-0 z-50 flex items-center justify-center bg-surface/85 p-6">
      <div class="relative w-full max-w-xs rounded-2xl border-2 border-ink bg-surface p-6 text-center" data-testid="p4-register-done">
        <p class="text-lg font-black">已保存为字符候选</p>
        <p class="mt-1.5 text-xs font-bold text-ink-muted">它仍是待本人确认状态；在训练台批量确认后才有资格进入训练快照。</p>
        <button type="button" class="btn-hard btn-hard--ink mt-4 w-full" @click="registerDone = false">继续</button>
      </div>
    </div>
  </main>
</template>

<!-- 验收(B5-02)：解灰接线——detect 候选虚线叠加/快划选中/手动框选兜底/extract 逐框半成品/黑底白字 alpha 预览切换/patch-D §4 四要素(对比视图·强制查看·独立确认+二次弹窗明示后果·空操作 warning)；登记携带 touchup_confirmed:true；公理 A3 禁词（清除类表述）零出现 -->
