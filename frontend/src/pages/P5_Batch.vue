<!-- src/pages/P5_Batch.vue -->
<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { request } from '@/api/http'
import { xhrUpload } from '@/api/xhrUpload'
import { ApiRequestError } from '@/api/http'
import { aspectRatioValue } from '@/utils/aspect'
import { useDraft } from '@/composables/useDraft'
import { usePolling } from '@/composables/usePolling'
import { useAuthStore } from '@/stores/auth'
import RenderStyleControls from '@/components/RenderStyleControls.vue'
import BatchTypographyControls, { type TypographySelection } from '@/components/BatchTypographyControls.vue'
import FrozenInkSummary from '@/components/FrozenInkSummary.vue'
import { completePendingRender, listPendingRenders, pendingRenderKey, persistPendingRender, samePending, type PendingRenderOperation } from '@/api/pendingRender'
import type { FrozenRenderSettings, LayoutProfile, RenderSelection } from '@/types/renderControls'
import { inkHex, validInkRgb } from '@/api/inkColor'

const router = useRouter()
const auth = useAuthStore()
const renderUserId = computed(() => auth.me?.user_id ?? '')
const MAX_ROWS = 200

// ---------- 数据 ----------

// 真数据源：assets（approved）/ phrase-templates（enabled）/ blogger presets / approved templates
interface AssetRow { asset_id: string; status: string; width: number; height: number }
interface BlessingRow { id: string; text: string; enabled: boolean }
interface PresetRow { preset_id: string; name: string; is_default: boolean }
interface TemplateRow { template_id: string; base_asset_id: string; status: string; created_at: string | null }
const assets = ref<AssetRow[]>([])
const blessings = ref<BlessingRow[]>([])
const presets = ref<PresetRow[]>([])
const templates = ref<TemplateRow[]>([])
const selectedAssetId = ref('')
const renderSelection = ref<RenderSelection | null>(null)
const typographySelection = ref<TypographySelection | null>(null)
const renderSelectionProblem = ref('')
const typographyProblem = ref('')
const generationBlocker = computed(() => !renderSelection.value ? renderSelectionProblem.value : !typographySelection.value ? typographyProblem.value : '')
const v3Reset = ref(0)
const editingLayout = ref<LayoutProfile>('baseline-v1')
const naturalTextEditing = computed(() => editingLayout.value === 'natural-layout-v1')
const pendingRenderBody = ref<Record<string, unknown> | null>(null)
let draftGeneration = 0
let disposed = false
function renderControlsChanged(): void {
  previewOpen.value = false
  stopDraftPreview()
}
function renderSelectionReady(value: RenderSelection | null, reason = ''): void {
  if (JSON.stringify(value) !== JSON.stringify(renderSelection.value)) renderControlsChanged()
  renderSelection.value = value
  renderSelectionProblem.value = reason
}
function typographyReady(value: TypographySelection | null, reason = ''): void {
  typographySelection.value = value
  typographyProblem.value = reason
}
const approvedAssets = computed(() => assets.value.filter((a) => a.status === 'approved'))
const selectedAsset = computed(() => approvedAssets.value.find((a) => a.asset_id === selectedAssetId.value))
const enabledBlessings = computed(() => blessings.value.filter((b) => b.enabled))
const defaultBlessingId = computed(() => enabledBlessings.value[0]?.id ?? '')
const blessingTextOf = (id: string): string => blessings.value.find((b) => b.id === id)?.text ?? '未选择'

// ---------- 博主署名（预设由 P8 模版语库维护；items[].sign_blogger + blogger_name 待联调，清单 §10） ----------
const bloggerPresets = ref<{ id: string; name: string; is_default: boolean }[]>([])
const bloggerName = ref('')
const bloggerCustom = ref(false)
const filledRows = computed(() => rows.value.filter((r) => r.cn.trim()))
const signatureNameRequired = computed(() => filledRows.value.some((row) => row.sign_blogger) && !bloggerName.value.trim())
const signatureNameMessage = '请先选择或填写博主署名，或关闭有内容行的「图上署名」开关'

interface BatchRow {
  cn: string
  blessing_id: string
  sign_blogger: boolean
  asset_id?: string
  template_id?: string
}

const { data: rows, hasDraft } = useDraft<BatchRow[]>('batch-generation-rows', [
  { cn: '', blessing_id: '', sign_blogger: true },
])

const uploadingAssets = ref(false)

function templatesForAsset(assetId: string): TemplateRow[] {
  return templates.value
    .filter((template) => template.status === 'approved' && template.base_asset_id === assetId)
    .sort((a, b) => (b.created_at ?? '').localeCompare(a.created_at ?? ''))
}

function defaultTemplateForAsset(assetId: string): TemplateRow | null {
  return templatesForAsset(assetId)[0] ?? null
}

function assetIdForRow(row: BatchRow): string {
  const assetId = row.asset_id || selectedAssetId.value
  return approvedAssets.value.some((asset) => asset.asset_id === assetId) ? assetId : ''
}

function templateForRow(row: BatchRow): TemplateRow | null {
  const options = templatesForAsset(assetIdForRow(row))
  return options.find((template) => template.template_id === row.template_id) ?? options[0] ?? null
}

function templateSelectionForRow(row: BatchRow): string {
  return templatesForAsset(assetIdForRow(row)).some((template) => template.template_id === row.template_id)
    ? row.template_id ?? ''
    : ''
}

function updateRowAsset(row: BatchRow, event: Event): void {
  const assetId = (event.target as HTMLSelectElement).value
  row.asset_id = assetId || undefined
  const activeAssetId = assetIdForRow(row)
  if (row.template_id && !templatesForAsset(activeAssetId).some((template) => template.template_id === row.template_id)) {
    row.template_id = undefined
  }
}

function updateRowTemplate(row: BatchRow, event: Event): void {
  row.template_id = (event.target as HTMLSelectElement).value || undefined
}

const jobTemplate = computed(() => defaultTemplateForAsset(selectedAssetId.value))
const rowsHaveTemplates = computed(() => filledRows.value.length > 0 && filledRows.value.every((row) => Boolean(assetIdForRow(row) && templateForRow(row))))

// 底图上传：COS 图片由真实 assets 接口接收；新上传仍须完成审核后才可生成。
// 移动端语义：<input type=file accept="image/*">（不带 capture）触发系统相册/选择器，
// 相册多选行为与拍照入口的取舍需与编排者确认（见 docs/frontend-integration-checklist.md §6）。
const assetFileInput = ref<HTMLInputElement | null>(null)
function onAddAssetTap(): void {
  assetFileInput.value?.click()
}
async function onAssetFilesPicked(e: Event): Promise<void> {
  const input = e.target as HTMLInputElement
  const files = Array.from(input.files ?? [])
  input.value = ''
  if (files.length === 0 || uploadingAssets.value) return
  uploadingAssets.value = true
  try {
    const form = new FormData()
    for (const f of files) form.append('files', f, f.name)
    form.append('asset_type', 'cos')
    const result = await xhrUpload<{ data?: { items?: { asset_id: string; reused?: boolean }[] }; items?: { asset_id: string; reused?: boolean }[] }>('/api/v1/assets', form, (p) => {
      if (p < 100) flash(`上传中 ${p}%`)
    }).start()
    const uploaded = result.data?.items ?? result.items ?? []
    await loadAssets()
    if (uploaded.length !== files.length) {
      flash('上传成功，但响应缺少底图记录；请刷新后检查审核状态')
    } else if (uploaded.some((asset) => !asset.reused)) {
      await router.push({ path: '/assets', query: { filter: 'pending_review', from: 'batch-upload' } })
    } else {
      flash(`已添加 ${files.length} 张已存在底图`)
    }
  } catch (err) {
    flash(err instanceof Error && err.message ? `上传失败：${err.message}` : '上传失败，请重试')
  } finally {
    uploadingAssets.value = false
  }
}

async function loadAssets(): Promise<void> {
  try {
    const all: AssetRow[] = []
    let page = 1
    while (true) {
      const data = await request<{ items: AssetRow[] }>(`/api/v1/assets?type=cos&status=approved&page=${page}&page_size=100`)
      all.push(...data.items)
      if (data.items.length < 100) break
      page += 1
    }
    assets.value = all
    if (!selectedAssetId.value && assets.value.length > 0) selectedAssetId.value = assets.value[0].asset_id
  } catch {
    flash('底图列表加载失败，请刷新重试')
  }
}

// ---------- C-27：草稿预览接真渲染（T75 draft 低优先级车道） ----------
// 预览不再用本地合成假图：drawer 打开即建 draft job（PRD §5.7 双车道，用户任务
// 优先、draft 每 5 个用户派发让 1 槽；draft 24h TTL 清理，不进审核/导出）。
// 轮询 R14 代际隔离；draft 失败降级为文本摘要预览，不冒充正式渲染结果。

interface DraftItem {
  item_id: string
  status: string
  cn: string
  output_relative_path: string | null
  warnings: string[]
  fallback_used?: boolean
  last_error?: string | null
}
type DraftPhase = 'idle' | 'creating' | 'rendering' | 'ready' | 'failed' | 'cancelled'
const draftPhase = ref<DraftPhase>('idle')
const draftJobId = ref('')
const draftItems = ref<DraftItem[]>([])
const draftHasWarnings = computed(() => draftItems.value.some((item) => item.status === 'completed_with_warning' || item.warnings?.length || item.fallback_used))
const draftFrozenSettings = ref<FrozenRenderSettings | null>(null)
const draftError = ref<string | null>(null)
// R9：draft 提交是独立动作，幂等键与正式提交分开；重试按钮 = 新动作 = 新键
const draftIdempotencyKey = ref(makeKey())
const draftCancellation = ref<{ jobId: string; status: 'pending' | 'unknown' | 'cancelled'; message: string } | null>(null)
const draftCancelPending = computed(() => draftCancellation.value?.status === 'pending')
let draftCancelGeneration = 0

// Both preview and formal render consume one request builder so their row mapping
// cannot drift while they share the current API contract.
function buildRenderPayload(draft = false): Record<string, unknown> {
  const filled = filledRows.value
  // Keep signed rows intact: missing names require an explicit choice before either render lane.
  if (signatureNameRequired.value) throw new Error(signatureNameMessage)
  if (!renderSelection.value) throw new Error('请先核验可用的字形来源、来源要求与排版配置')
  if (!typographySelection.value) throw new Error('请检查字号和不规则程度，或恢复默认排版设置')
  const rowConfigs = filled.map((row, index) => {
    const assetId = assetIdForRow(row)
    const template = templateForRow(row)
    if (!assetId || !template) throw new Error(`第 ${index + 1} 行底图没有已审批布局模板`)
    return { assetId, template }
  })
  const firstTemplate = rowConfigs[0]?.template
  if (!firstTemplate) throw new Error('请先添加至少一行有效内容')
  const hasSignature = filled.some((row) => row.sign_blogger)
  return {
    template_id: firstTemplate.template_id,
    ...renderSelection.value,
    ...typographySelection.value,
    ...(draft ? { draft: true } : {}),
    items: filled.map((row, index) => {
      const blessing = blessings.value.find((item) => item.id === row.blessing_id)
      return {
        // Validation ignores whitespace-only rows; natural layout receives every authored space/newline.
        cn: renderSelection.value?.layout_profile === 'natural-layout-v1' ? row.cn : row.cn.trim(),
        blessing: blessing?.text || undefined,
        asset_id: rowConfigs[index].assetId,
        template_id: rowConfigs[index].template.template_id,
        sign_blogger: row.sign_blogger,
      }
    }),
    // Row overrides carry the actual decision; false is only the fallback for legacy consumers.
    sign_blogger: false,
    blogger_name: hasSignature ? bloggerName.value.trim() : undefined,
  }
}

function draftItemFor(index: number): DraftItem | null {
  return draftItems.value[index] ?? null
}
function draftImageUrl(index: number): string | null {
  const item = draftItemFor(index)
  if (!item || !draftJobId.value || !draftItemHasOutput(item)) return null
  return `/api/v1/render-jobs/${draftJobId.value}/items/${item.item_id}/image`
}
function draftItemHasOutput(item: DraftItem): boolean {
  return ['completed', 'completed_with_warning'].includes(item.status)
    && Boolean(item.item_id && item.output_relative_path?.trim()) && !item.last_error
}
function finishDraftPreview(status: string): void {
  // Job `partial` includes warning-only results; every authored row must still have a real output.
  if (['succeeded', 'partial'].includes(status) && filledRows.value.length > 0
    && draftItems.value.length === filledRows.value.length && draftItems.value.every(draftItemHasOutput)) {
    draftPhase.value = 'ready'
    return
  }
  draftPhase.value = 'failed'
  draftError.value = `草稿渲染未完成（${status}）；${status === 'cancelled' ? '草稿渲染已取消' : '存在失败或未出图的行，请查看逐行记录后重试'}`
}
function draftFallbackUsed(index: number): boolean {
  const item = draftItemFor(index)
  return item?.fallback_used === true || Boolean(item?.warnings?.some((warning) => /^GLYPH_STYLE_FALLBACK(?:\s|$)/.test(warning)))
}
function draftWarningLabel(index: number): string {
  if (draftFallbackUsed(index)) return '回退'
  return draftItemFor(index)?.warnings?.every((warning) => warning.startsWith('GLYPH_STYLE ')) ? '字形来源' : '提示'
}

function stopDraftPreview(): void {
  ++draftGeneration
  draftPolling.stop()
  draftPhase.value = 'idle'
  draftJobId.value = ''
  draftItems.value = []
  draftFrozenSettings.value = null
  draftError.value = null
}

async function cancelDraftPreview(): Promise<void> {
  if (draftPhase.value !== 'rendering' || !draftJobId.value || draftCancelPending.value || submitting.value) return
  const jobId = draftJobId.value; const userId = renderUserId.value; const generation = ++draftCancelGeneration
  ++draftGeneration; draftPolling.stop()
  draftCancellation.value = { jobId, status: 'pending', message: '正在取消草稿…' }
  try {
    const result = await request<{ job: { job_id: string; status: string } }>(`/api/v1/render-jobs/${jobId}/cancel`, { method: 'POST', retries: 0, timeoutMs: 60000 })
    if (disposed || generation !== draftCancelGeneration || userId !== renderUserId.value) return
    applyDraftCancellation(jobId, result.job)
  } catch (error) {
    if (disposed || generation !== draftCancelGeneration || userId !== renderUserId.value) return
    draftCancellation.value = { jobId, status: 'unknown', message: `取消结果尚未确认：${error instanceof Error ? error.message : '请求失败'}；请刷新原草稿状态核对` }
  }
}

function applyDraftCancellation(jobId: string, job?: { job_id: string; status: string }): void {
  if (job?.job_id === jobId && job.status === 'cancelled') {
    draftCancellation.value = { jobId, status: 'cancelled', message: '草稿生成已取消' }
    if (draftJobId.value === jobId) { draftPhase.value = 'cancelled'; draftError.value = null }
  } else {
    draftCancellation.value = { jobId, status: 'unknown', message: `取消结果尚未确认（${job?.status ?? '未返回状态'}）；请刷新原草稿状态核对` }
  }
}

async function refreshDraftCancellation(): Promise<void> {
  const jobId = draftCancellation.value?.jobId
  if (!jobId || draftCancelPending.value) return
  const userId = renderUserId.value; const generation = ++draftCancelGeneration
  draftCancellation.value = { jobId, status: 'pending', message: '正在核对原草稿取消状态…' }
  try {
    const result = await request<{ job: { job_id: string; status: string } }>(`/api/v1/render-jobs/${jobId}`, { retries: 0, timeoutMs: 600000 })
    if (disposed || generation !== draftCancelGeneration || userId !== renderUserId.value) return
    applyDraftCancellation(jobId, result.job)
  } catch (error) {
    if (disposed || generation !== draftCancelGeneration || userId !== renderUserId.value) return
    draftCancellation.value = { jobId, status: 'unknown', message: `取消结果尚未确认：${error instanceof Error ? error.message : '读取失败'}；请刷新原草稿状态核对` }
  }
}

async function startDraftPreview(): Promise<void> {
  const filled = rows.value.filter((r) => r.cn.trim())
  if (filled.length === 0 || submitting.value || draftCancelPending.value) return
  // 任一有效行没有已审批模板时后端必拒：不打无效请求
  if (!rowsHaveTemplates.value) {
    draftPhase.value = 'failed'
    draftError.value = '有内容的行缺少已审批底图或布局模板，草稿渲染不可用，已降级为文本预览'
    return
  }
  const generation = ++draftGeneration
  draftPhase.value = 'creating'
  draftError.value = null
  draftItems.value = []
  draftFrozenSettings.value = null
  draftIdempotencyKey.value = makeKey()
  try {
    const result = await request<{ job: FrozenRenderSettings & { job_id: string; status: string }; items: DraftItem[] }>(
      '/api/v1/render-jobs',
      {
        method: 'POST',
        retries: 0, timeoutMs: 60000,
        idempotencyKey: draftIdempotencyKey.value,
        body: buildRenderPayload(true),
      },
    )
    if (disposed || generation !== draftGeneration) return
    draftJobId.value = result.job.job_id
    draftItems.value = result.items ?? []
    draftFrozenSettings.value = result.job
    if (['queued', 'running'].includes(result.job.status)) {
      draftPhase.value = 'rendering'
      draftPolling.start()
    } else {
      finishDraftPreview(result.job.status)
    }
  } catch (err) {
    if (disposed || generation !== draftGeneration) return
    draftPhase.value = 'failed'
    draftError.value = err instanceof Error && err.message ? err.message : '草稿渲染任务创建失败'
  }
}

// R14 代际轮询：draft job 终态即停；组件卸载自动 stop
const draftPolling = usePolling(
  async () => {
    if (!draftJobId.value) return { done: true }
    const generation = draftGeneration; const jobId = draftJobId.value
    try {
      const d = await request<{ job: FrozenRenderSettings & { status: string }; items: DraftItem[] }>(
        `/api/v1/render-jobs/${draftJobId.value}`,
      )
      if (disposed || generation !== draftGeneration || jobId !== draftJobId.value) return { done: true }
      draftItems.value = d.items ?? []
      draftFrozenSettings.value = d.job
      const st = d.job.status
      if (['queued', 'running'].includes(st)) return { done: false }
      finishDraftPreview(st)
      return { done: true }
    } catch (e) {
      if (disposed || generation !== draftGeneration || jobId !== draftJobId.value) return { done: true }
      // 单次轮询失败不判定失败，交由 usePolling 退避；连续失败显式降级
      if (e instanceof ApiRequestError && e.status === 404) {
        draftPhase.value = 'failed'
        draftError.value = '草稿渲染结果已过期（24h TTL），可重新生成'
        return { done: true }
      }
      return { done: false }
    }
  },
  { base: 1200, cap: 6000, immediate: false },
)

onBeforeUnmount(() => {
  disposed = true; ++draftGeneration
  draftPolling.stop()
})

onMounted(() => {
  void loadAssets()
  void (async () => {
    try {
      const data = await request<{ items: BlessingRow[] }>('/api/v1/phrase-templates?enabled=true')
      blessings.value = data.items
    } catch {
      flash('祝福语库加载失败')
    }
    try {
      const data = await request<{ items: PresetRow[] }>('/api/v1/blogger-name-presets')
      presets.value = data.items
      bloggerPresets.value = presets.value.map((p) => ({ id: p.preset_id, name: p.name, is_default: p.is_default }))
      bloggerName.value = bloggerPresets.value.find((p) => p.is_default)?.name ?? bloggerPresets.value[0]?.name ?? ''
    } catch {
      flash('署名预设加载失败')
    }
    try {
      const data = await request<{ items: TemplateRow[] }>('/api/v1/layout-templates?status=approved&page_size=100')
      templates.value = data.items
    } catch {
      flash('布局模板加载失败')
    }
  })()
})
const pasteOpen = ref(false)
const pasteText = ref('')
const submitting = ref(false)
const previewOpen = ref(false)
// 放大预览灯箱：后端暂无 preview 端点（D-T58-6），合成占位保留
const lightboxRow = ref<number | null>(null)
const notice = ref<string | null>(null)
let noticeTimer: number | null = null
function flash(message: string): void {
  notice.value = message
  if (noticeTimer !== null) window.clearTimeout(noticeTimer)
  noticeTimer = window.setTimeout(() => {
    notice.value = null
    noticeTimer = null
  }, 1800)
}

// ---------- 缺字预检：逐字比对 coverage fixtures（'彧' 命中 high + simkai 回退） ----------

// 缺字预检已移交服务端（create_job GLYPH 预检 + fallback_map 落 item）；客户端不再维护 fixture
const highRiskChars = new Map<string, Record<string, string>>()

interface RowRisk {
  missing: string[]
  fallbackMap: Record<string, string>
  level: 'high' | 'low'
}
function rowRisk(cn: string): RowRisk {
  const missing: string[] = []
  const fallbackMap: Record<string, string> = {}
  for (const ch of cn) {
    const hit = highRiskChars.get(ch)
    if (hit) {
      missing.push(ch)
      Object.assign(fallbackMap, hit)
    }
  }
  return {
    missing: [...new Set(missing)],
    fallbackMap,
    level: missing.length > 0 ? 'high' : 'low',
  }
}
const riskOf = (row: BatchRow): RowRisk => rowRisk(row.cn.trim())

const riskSummary = computed(() => {
  let high = 0
  for (const r of rows.value) {
    if (r.cn.trim() && rowRisk(r.cn.trim()).level === 'high') high++
  }
  return { high, total: rows.value.filter((r) => r.cn.trim()).length }
})

// ---------- 行操作 ----------

function addRow(prefill?: string): void {
  if (rows.value.length >= MAX_ROWS) return
  rows.value.push({ cn: prefill ?? '', blessing_id: defaultBlessingId.value, sign_blogger: true })
}
function removeRow(index: number): void {
  rows.value.splice(index, 1)
  if (rows.value.length === 0) addRow()
}
function resetAll(): void {
  rows.value = [{ cn: '', blessing_id: defaultBlessingId.value, sign_blogger: true }]
  pasteText.value = ''
  pasteOpen.value = false
  flash('已重置为空批次')
}

// 按 '\n' 切分、trim、剔空行，硬截断 200
function parsePaste(): void {
  const names = pasteText.value
    .split('\n')
    .map((s) => s.trim())
    .filter(Boolean)
  if (names.length === 0) {
    flash('剪贴板内容没有识别到有效名称')
    return
  }
  const room = Math.max(0, MAX_ROWS - rows.value.filter((r) => r.cn.trim()).length)
  const accepted = names.slice(0, room)
  for (const name of accepted) addRow(name)
  pasteText.value = ''
  pasteOpen.value = false
  flash(
    accepted.length < names.length
      ? `已解析 ${accepted.length} 行，${names.length - accepted.length} 行超出 200 上限被截断`
      : `已解析并追加 ${accepted.length} 行`,
  )
}

function onKeydown(e: KeyboardEvent): void {
  if (e.key !== 'Escape') return
  if (lightboxRow.value !== null) {
    lightboxRow.value = null
    return
  }
  if (previewOpen.value) {
    previewOpen.value = false
    stopDraftPreview()
  }
}

// ---------- 幂等性（白皮书 R9）：同批次提交/重试复用同一 Key，新建批次才换新 Key ----------

function makeKey(): string {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) return crypto.randomUUID()
  return `idem-${Date.now()}-${Math.random().toString(16).slice(2, 8)}`
}
const idempotencyKey = ref(makeKey())
const pendingRenderError = ref('')
const recoverableOperations = ref<PendingRenderOperation[]>([])
function refreshRecoverable(): boolean {
  if (!renderUserId.value) { recoverableOperations.value = []; return true }
  try { recoverableOperations.value = listPendingRenders(renderUserId.value); return true }
  catch (reason) { pendingRenderError.value = reason instanceof Error ? reason.message : '无法读取未确认提交，请先恢复浏览器存储'; return false }
}
function chooseRecovery(operation: PendingRenderOperation): void {
  if (submitting.value || !refreshRecoverable()) return
  const stored = recoverableOperations.value.find((item) => samePending(item, operation))
  if (!stored) { flash('这条未确认提交已在其他窗口处理，请重新查看'); return }
  pendingRenderBody.value = JSON.parse(JSON.stringify(stored.body)); idempotencyKey.value = stored.key
  renderControlsChanged()
}
function pendingDescription(operation: PendingRenderOperation): string {
  const body = operation.body
  const color = body.ink_color_mode === 'custom-solid-srgb-v1' && validInkRgb(body.ink_color_rgb) ? inkHex(body.ink_color_rgb) : body.ink_palette_id ?? '原有单色'
  return `${operation.key} · ${body.style_mode === 'profile' ? `档案 #${body.style_id}` : '系统字体 M0'} · ${color} · ${(body.items as unknown[]).length} 单`
}
function pendingTexts(operation: PendingRenderOperation): string[] {
  return (operation.body.items as { cn?: unknown }[]).map((item) => typeof item?.cn === 'string' ? item.cn : '内容未记录')
}
function onPendingStorage(event: StorageEvent): void {
  if (event.storageArea === localStorage && (event.key === null || event.key === pendingRenderKey(renderUserId.value) || event.key.startsWith(`${pendingRenderKey(renderUserId.value)}:operation:`))) refreshRecoverable()
}
watch(renderUserId, () => {
  ++draftCancelGeneration; draftCancellation.value = null
  pendingRenderBody.value = null; pendingRenderError.value = ''; idempotencyKey.value = makeKey(); submitting.value = false
  recoverableOperations.value = []; renderSelection.value = null; renderControlsChanged()
  if (!renderUserId.value || !refreshRecoverable()) return
  // A single prior operation keeps legacy explicit-retry behavior. Multiple operations require a choice.
  if (recoverableOperations.value.length === 1) chooseRecovery(recoverableOperations.value[0]!)
}, { immediate: true, flush: 'sync' })
onMounted(() => window.addEventListener('storage', onPendingStorage))
onBeforeUnmount(() => window.removeEventListener('storage', onPendingStorage))

async function submitJob(preview: boolean): Promise<void> {
  if (submitting.value || draftCancelPending.value) return
  if (!renderUserId.value || pendingRenderError.value) { flash(pendingRenderError.value || '请先登录'); return }
  const requestUserId = renderUserId.value
  if (!refreshRecoverable()) { flash(pendingRenderError.value); return }
  if (!pendingRenderBody.value && recoverableOperations.value.length) {
    flash('存在其他窗口或先前的未确认提交，请明确选择要恢复的记录；当前内容和墨色不会覆盖或自动重放它')
    return
  }
  const filled = rows.value.filter((r) => r.cn.trim())
  if (filled.length === 0 && !pendingRenderBody.value) {
    flash('请先录入至少 1 个粉丝 CN 昵称')
    return
  }
  if (signatureNameRequired.value && !pendingRenderBody.value) {
    bloggerCustom.value = true
    flash(signatureNameMessage)
    return
  }
  if (preview && pendingRenderBody.value) { flash('请先确认上次正式提交结果'); return }
  if (preview) {
    previewOpen.value = true
    void startDraftPreview()
    return
  }
  submitting.value = true
  if (!rowsHaveTemplates.value && !pendingRenderBody.value) {
    flash('有内容的行缺少已审批底图或布局模板，请先完成选择和审批')
    submitting.value = false
    return
  }
  let operation: PendingRenderOperation | null = null
  try {
    if (!pendingRenderBody.value) pendingRenderBody.value = JSON.parse(JSON.stringify(buildRenderPayload()))
    operation = { key: idempotencyKey.value, body: JSON.parse(JSON.stringify(pendingRenderBody.value)) }
    persistPendingRender(requestUserId, operation)
    refreshRecoverable()
    // Preserve an uncertain request body and key across retries and reloads.
    const result = await request<{ job: { job_id: string }; items: unknown[] }>('/api/v1/render-jobs', {
      method: 'POST',
      retries: 0, timeoutMs: 60000,
      idempotencyKey: operation.key,
      body: operation.body,
    })
    completePendingRender(requestUserId, operation)
    if (disposed || requestUserId !== renderUserId.value || idempotencyKey.value !== operation.key
      || !pendingRenderBody.value || !samePending(operation, { key: idempotencyKey.value, body: pendingRenderBody.value })) return
    pendingRenderBody.value = null
    refreshRecoverable()
    submitting.value = false
    if (!disposed) void router.push(`/batch/${result.job.job_id}/review`)
  } catch (err) {
    if (disposed || requestUserId !== renderUserId.value || (operation && (idempotencyKey.value !== operation.key
      || !pendingRenderBody.value || !samePending(operation, { key: idempotencyKey.value, body: pendingRenderBody.value })))) return
    submitting.value = false
    // Only definite pre-create validation rejections permit editing a new request.
    // Unknown conflicts, auth failures and rate limits retain the uncertain original.
    if (err instanceof ApiRequestError && ['INVALID_REQUEST', 'EMPTY_BATCH', 'INVALID_CN', 'INVALID_STYLE_MODE', 'INVALID_GLYPH_SOURCE_POLICY', 'INVALID_LAYOUT_PROFILE', 'INVALID_LAYOUT_SEED', 'INK_COLOR_UNAVAILABLE', 'INK_COLOR_UNSUPPORTED',
      'STYLE_DISABLED', 'STYLE_NOT_APPROVED', 'STYLE_NOT_RENDERABLE', 'NON_CJK_FONT_UNAVAILABLE', 'TEMPLATE_NOT_APPROVED', 'ASSET_NOT_FOUND', 'ASSET_NOT_APPROVED', 'SIGNATURE_SLOT_MISSING', 'GLYPH_MISSING'].includes(err.code)
      && [400, 404, 409, 422].includes(err.status)) {
      if (operation) completePendingRender(requestUserId, operation)
      pendingRenderBody.value = null; idempotencyKey.value = makeKey(); refreshRecoverable()
    }
    flash(err instanceof Error && err.message ? `渲染任务创建失败：${err.message}` : '渲染任务创建失败，请重试')
  }
}
</script>

<template>
  <main class="bg-blueprint flex h-[100dvh] flex-col overflow-hidden text-ink select-none" @keydown="onKeydown">
    <h1 class="sr-only">批量生成工作台</h1>

    <header class="safe-t z-20 flex h-14 shrink-0 items-center justify-between gap-2 border-b border-ink/15 px-3">
      <button
        type="button"
        aria-label="返回工作台"
        class="btn-hard btn-hard--compact !rounded-full !px-3"
        @click="router.push('/')"
      >
        <svg aria-hidden="true" class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">
          <path d="M19 12H5"></path>
          <path d="m12 19-7-7 7-7"></path>
        </svg>
      </button>
      <div class="flex min-w-0 flex-col items-center">
        <p class="truncate text-sm font-black">批量生成工作台</p>
        <span class="tag-code mt-0.5">batch × {{ MAX_ROWS }}</span>
      </div>
      <button type="button" class="btn-hard btn-hard--compact !rounded-full !px-3 text-xs" @click="resetAll">
        重置清空
      </button>
    </header>

    <section class="flex-1 space-y-5 overflow-y-auto overflow-x-hidden px-4 pb-32 pt-4">
      <!-- Step 01 底图滑轨 -->
      <div class="motion-rise">
        <div class="mb-2 flex items-center justify-between">
          <p class="text-sm font-black">01 · 选择底图</p>
          <span class="tag-code">{{ approvedAssets.length }} 张已就绪</span>
        </div>
        <div class="flex gap-3 overflow-x-auto pb-1 [&::-webkit-scrollbar]:hidden">
          <button
            v-for="asset in approvedAssets"
            :key="asset.asset_id"
            type="button"
            :aria-pressed="selectedAssetId === asset.asset_id"
            :class="[
              'relative aspect-square w-20 shrink-0 overflow-hidden rounded-xl border-2 bg-sky p-0 transition-all duration-150 active:scale-95',
              selectedAssetId === asset.asset_id
                ? 'border-primary shadow-[3px_3px_0_var(--color-primary)]'
                : 'border-ink shadow-[2px_2px_0_var(--color-ink)]',
            ]"
            @click="selectedAssetId = asset.asset_id"
          >
            <!-- 方形选择槽内真实缩略图（320px 管线），加载失败退回线描 -->
            <span class="absolute inset-0 flex items-center justify-center p-1.5">
              <span
                :style="{ aspectRatio: aspectRatioValue(asset.width, asset.height) }"
                class="flex max-h-full max-w-full items-center justify-center overflow-hidden rounded-md border border-ink/20 bg-surface/70"
                :class="asset.width >= asset.height ? 'w-full' : 'h-full'"
              >
                <img
                  :src="`/api/v1/assets/${asset.asset_id}/thumbnail`"
                  alt=""
                  class="h-full w-full object-cover"
                  draggable="false"
                >
                <svg aria-hidden="true" class="hidden max-h-2/3 max-w-2/3 text-ink/25" viewBox="0 0 100 100" fill="none" stroke="currentColor" stroke-width="4">
                  <circle cx="50" cy="38" r="17"></circle>
                  <path d="M18 92c4-20 17-30 32-30s28 10 32 30" stroke-linecap="round"></path>
                </svg>
              </span>
            </span>
            <span
              v-if="selectedAssetId === asset.asset_id"
              class="absolute bottom-1 right-1 flex h-5 w-5 items-center justify-center rounded-full border-2 border-ink bg-primary text-white"
            >
              <svg aria-hidden="true" class="h-3 w-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="4" stroke-linecap="round" stroke-linejoin="round">
                <path d="M20 6 9 17l-5-5"></path>
              </svg>
            </span>
          </button>

          <!-- 添加底图入口：调起系统相册并上传至真实 assets 接口 -->
          <input
            ref="assetFileInput"
            type="file"
            accept="image/*"
            multiple
            class="hidden"
            aria-hidden="true"
            tabindex="-1"
            @change="onAssetFilesPicked"
          >
          <button
            type="button"
            class="group relative flex aspect-square w-20 shrink-0 flex-col items-center justify-center gap-1 rounded-xl border-2 border-dashed border-ink/50 bg-surface/60 p-1 transition-all duration-150 hover:border-ink hover:bg-sky/50 active:scale-95"
            aria-label="添加底图（从相册上传）"
            @click="onAddAssetTap"
          >
            <svg aria-hidden="true" class="h-5 w-5 text-ink-muted transition-transform group-hover:scale-110" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round">
              <path d="M12 5v14M5 12h14"></path>
            </svg>
            <span class="text-[9px] font-black leading-tight text-ink-muted">添加底图</span>
            <span v-if="uploadingAssets" class="absolute -right-1 -top-1 rounded-md border border-ink bg-amber px-1 py-px font-mono text-[8px] font-black">上传中…</span>
          </button>
        </div>
        <p class="mt-1.5 font-mono text-[10px] font-bold text-ink-muted">
          左右滑动选择 · 点 + 上传新底图（审核通过后可生成）
        </p>
      </div>

      <RenderStyleControls id="batch-render-settings" :key="renderUserId" :scope-key="renderUserId" :disabled="submitting || Boolean(pendingRenderBody) || Boolean(pendingRenderError)" @ready="renderSelectionReady" @changed="renderControlsChanged" @layout="editingLayout = $event" @v3reset="v3Reset++" />
      <BatchTypographyControls id="batch-typography-settings" :key="`typography-${renderUserId}`" :scope-key="String(renderUserId)" :v3-reset="v3Reset" :disabled="submitting || Boolean(pendingRenderBody) || Boolean(pendingRenderError)" @ready="typographyReady" @changed="renderControlsChanged" />
      <p v-if="pendingRenderError" class="rounded-lg bg-blush p-2 text-xs text-danger" role="alert">{{ pendingRenderError }}</p>
      <div v-if="submitting" role="status" aria-live="polite" aria-busy="true" data-testid="formal-submission-status" class="rounded-lg border border-ink/20 bg-surface p-3 text-xs">
        <p class="font-bold">正在核验并提交，请稍候。</p>
        <p class="mt-1">模型、字体和任务参数核验可能耗时。服务器尚未确认任务，此提示不表示图片已开始生成。</p>
        <p class="mt-1">原请求已保留，请保持页面，不要重复提交。收到任务编号后将自动打开该任务；等待结束但结果未确认时，仍可恢复同一原请求。</p>
      </div>
      <p v-if="pendingRenderBody && !submitting" class="rounded-lg border border-amber bg-amber/10 p-2 text-xs font-bold" role="alert">请先确认上次正式提交结果，确认前不能预览新草稿。「重试原提交」会使用原来的来源、排版、种子、墨色和内容；当前编辑不会覆盖该请求。</p>

      <section v-if="recoverableOperations.length" class="rounded-xl border-2 border-amber bg-amber/10 p-3 text-xs" data-testid="pending-render-recovery">
        <p class="font-bold">未确认的正式提交（{{ recoverableOperations.length }}）</p>
        <p>每条记录单独保存原始请求。其他窗口的提交不会覆盖它；有多条时请明确选择，重试不会采用当前编辑的墨色或内容。</p>
        <div v-for="operation in recoverableOperations" :key="operation.key" class="mt-2 border-t border-ink/15 pt-2">
          <p class="break-all">{{ pendingDescription(operation) }}</p>
          <p v-for="(text, index) in pendingTexts(operation)" :key="index" class="whitespace-break-spaces break-all">{{ text }}</p>
          <button type="button" class="mt-1 min-h-11 font-bold underline" :disabled="submitting" :aria-label="`恢复未确认提交 ${operation.key}`" @click="chooseRecovery(operation)">{{ pendingRenderBody && idempotencyKey === operation.key ? '已选中原提交' : '选择恢复此提交' }}</button>
        </div>
      </section>

      <!-- Step 03 粘贴解析 -->
      <div class="motion-rise rounded-2xl border-2 border-ink bg-surface p-4 shadow-[3px_3px_0_var(--color-ink)]" style="animation-delay:40ms">
        <button
          type="button"
          class="flex min-h-[44px] w-full items-center justify-between text-left"
          :aria-expanded="pasteOpen"
          @click="pasteOpen = !pasteOpen"
        >
          <span class="flex items-center gap-2 text-sm font-black">
            03 · 从剪贴板粘贴粉丝名单
            <span v-if="hasDraft()" class="tag-code !border-amber !bg-amber/20">草稿</span>
          </span>
          <svg aria-hidden="true" class="h-4 w-4 transition-transform duration-150" :class="pasteOpen ? 'rotate-180' : ''" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round">
            <path d="m6 9 6 6 6-6"></path>
          </svg>
        </button>
        <div v-if="pasteOpen" class="mt-3">
          <textarea
            v-model="pasteText"
            class="input-hard min-h-[110px] resize-y font-mono text-xs"
            placeholder="格式：每行一个 CN 名称（支持按换行自动分行），单次上限 200 行"
          ></textarea>
          <p v-if="naturalTextEditing" class="mt-2 text-xs text-ink-muted">名单粘贴仍按换行分成多单，并去除每行首尾空白；需要保留空格或在同一单内换行，请在下方逐行文本框编辑。</p>
          <button type="button" class="btn-hard btn-hard--ink mt-2 w-full !rounded-2xl text-xs" @click="parsePaste">
            一键智能解析分行
          </button>
        </div>
      </div>

      <!-- 覆盖预检汇总 -->
      <div class="motion-rise flex items-center justify-between gap-2" style="animation-delay:80ms">
        <p class="text-sm font-black">04 · 逐行配置</p>
        <span
          class="rounded-full border-2 px-3 py-1 text-[11px] font-black"
          :class="riskSummary.high > 0
            ? 'border-amber bg-amber/20 text-ink'
            : 'border-mint bg-mint/40 text-emerald-800'"
          role="status"
        >
          {{ riskSummary.high > 0
            ? `存在 ${riskSummary.high} 处缺字，生成时自动降级`
            : '全部字形 100% 覆盖' }}
        </span>
      </div>

      <!-- 博主署名预设（模版语库 P8 维护，可变可预设） -->
      <div class="motion-rise rounded-2xl border-2 border-ink bg-surface p-3 shadow-[2px_2px_0_var(--color-ink)]" style="animation-delay:100ms">
        <div class="flex items-center gap-2">
          <span class="shrink-0 text-xs font-black">博主署名</span>
          <div class="flex flex-1 gap-1.5 overflow-x-auto [&::-webkit-scrollbar]:hidden" role="group" aria-label="博主署名预设">
            <button
              v-for="p in bloggerPresets"
              :key="p.id"
              type="button"
              class="shrink-0 rounded-full border-2 px-3 py-1.5 text-[11px] font-black transition-all duration-150 active:scale-95"
              :class="!bloggerCustom && bloggerName === p.name
                ? 'border-ink bg-ink text-white'
                : 'border-ink/25 bg-canvas text-ink-muted hover:border-ink hover:text-ink'"
              :aria-pressed="!bloggerCustom && bloggerName === p.name"
              @click="bloggerCustom = false; bloggerName = p.name"
            >
              {{ p.name }}
            </button>
            <button
              type="button"
              class="shrink-0 rounded-full border-2 border-dashed px-3 py-1.5 text-[11px] font-black transition-all duration-150 active:scale-95"
              :class="bloggerCustom ? 'border-ink bg-sky text-ink' : 'border-ink/40 text-ink-muted hover:border-ink hover:text-ink'"
              :aria-pressed="bloggerCustom"
              @click="bloggerCustom = true"
            >
              自定义
            </button>
          </div>
          <RouterLink to="/blessings" class="shrink-0 font-mono text-[10px] font-black text-primary underline underline-offset-2">管理</RouterLink>
        </div>
        <input
          v-if="bloggerCustom"
          v-model="bloggerName"
          type="text"
          maxlength="20"
          class="input-hard mt-2 min-h-[44px] text-sm"
          placeholder="输入自定义博主名（≤20 字）"
          aria-label="自定义博主名"
          :aria-invalid="signatureNameRequired || undefined"
          :aria-describedby="signatureNameRequired ? 'signature-name-required' : undefined"
        >
        <p v-if="signatureNameRequired" id="signature-name-required" class="mt-2 text-xs font-bold text-danger" role="alert">
          {{ signatureNameMessage }}
        </p>
      </div>

      <!-- 动态行 -->
      <div class="space-y-3">
        <div
          v-for="(row, index) in rows"
          :key="index"
          class="motion-fade rounded-2xl border-2 bg-surface p-3 transition-all"
          :class="riskOf(row).level === 'high' && row.cn.trim()
            ? 'border-coral border-l-[10px] shadow-[3px_3px_0_var(--color-coral)]'
            : 'border-ink shadow-[2px_2px_0_var(--color-ink)]'"
        >
          <div class="flex items-center gap-2">
            <span class="w-9 shrink-0 font-mono text-xs font-black text-ink-muted">#{{ index + 1 }}</span>
            <textarea
              v-if="naturalTextEditing"
              v-model="row.cn"
              rows="3"
              class="input-hard min-h-[88px] min-w-0 flex-1 resize-y !py-2 text-sm"
              placeholder="输入粉丝 CN 文本，可在同一单内换行"
              :aria-label="`第 ${index + 1} 行粉丝昵称`"
              :aria-describedby="`natural-text-help-${index}`"
            ></textarea>
            <input
              v-else
              v-model="row.cn"
              type="text"
              class="input-hard min-h-[44px] flex-1 !py-2 text-sm"
              placeholder="输入粉丝 CN 昵称"
              :aria-label="`第 ${index + 1} 行粉丝昵称`"
            >
            <button
              type="button"
              class="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl border-2 border-coral bg-surface text-coral transition-all hover:bg-coral hover:text-white active:scale-95"
              :aria-label="`删除第 ${index + 1} 行`"
              @click="removeRow(index)"
            >
              <svg aria-hidden="true" class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M3 6h18"></path>
                <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
              </svg>
            </button>
          </div>

          <p v-if="naturalTextEditing" :id="`natural-text-help-${index}`" class="mt-1 pl-11 text-[10px] text-ink-muted">自然排版保留此框中的首尾空格、全角空格和换行；回车在同一单内换行，纯空白内容不会生成。</p>

          <div class="mt-2 grid grid-cols-1 gap-2 pl-11 sm:grid-cols-2">
            <label class="sr-only" :for="`asset-${index}`">第 {{ index + 1 }} 行底图</label>
            <select
              :id="`asset-${index}`"
              :value="row.asset_id ?? ''"
              class="input-hard min-h-[44px] w-full text-xs"
              :aria-label="`第 ${index + 1} 行底图`"
              @change="updateRowAsset(row, $event)"
            >
              <option value="">跟随批次底图 · {{ selectedAsset?.asset_id ?? '未选择' }}</option>
              <option v-for="asset in approvedAssets" :key="asset.asset_id" :value="asset.asset_id">
                {{ asset.asset_id }} · {{ asset.width }}×{{ asset.height }}
              </option>
            </select>
            <label class="sr-only" :for="`template-${index}`">第 {{ index + 1 }} 行布局模板</label>
            <select
              :id="`template-${index}`"
              :value="templateSelectionForRow(row)"
              class="input-hard min-h-[44px] w-full text-xs"
              :aria-label="`第 ${index + 1} 行布局模板`"
              @change="updateRowTemplate(row, $event)"
            >
              <option value="">默认布局 · {{ defaultTemplateForAsset(assetIdForRow(row))?.template_id ?? '无已审批模板' }}</option>
              <option v-for="template in templatesForAsset(assetIdForRow(row))" :key="template.template_id" :value="template.template_id">
                {{ template.template_id }}
              </option>
            </select>
          </div>
          <p v-if="row.cn.trim() && !templateForRow(row)" class="mt-1 pl-11 text-[10px] font-bold text-danger" role="alert">
            该行所选底图没有已审批布局模板
          </p>

          <div class="mt-2 flex items-center gap-2 pl-11">
            <label class="sr-only" :for="`blessing-${index}`">祝福语模板</label>
            <div class="relative w-full">
              <select
                :id="`blessing-${index}`"
                v-model="row.blessing_id"
                class="input-hard min-h-[44px] w-full appearance-none pr-9 text-xs"
              >
                <option v-for="b in enabledBlessings" :key="b.id" :value="b.id">
                  {{ b.text }}
                </option>
              </select>
              <svg aria-hidden="true" class="pointer-events-none absolute right-3 top-1/2 h-4 w-4 -translate-y-1/2 text-ink-muted" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round">
                <path d="m6 9 6 6 6-6"></path>
              </svg>
            </div>
          </div>

          <!-- 署名开关：是否在该行 cos 图上写博主名 -->
          <div class="mt-2 flex items-center justify-between gap-2 pl-11">
            <span class="text-[11px] font-bold text-ink-muted">
              图上署名 <span class="font-black text-ink">@{{ bloggerName || '未设置' }}</span>
            </span>
            <button
              type="button"
              role="switch"
              :aria-checked="row.sign_blogger"
              :aria-label="`第 ${index + 1} 行图上署名开关`"
              class="flex min-h-[44px] min-w-[44px] items-center justify-end"
              @click="row.sign_blogger = !row.sign_blogger"
            >
              <span
                class="relative inline-flex h-6 w-11 rounded-full border-2 border-ink transition-colors duration-150"
                :class="row.sign_blogger ? 'bg-mint' : 'bg-gray-200'"
              >
                <span
                  class="absolute top-0.5 h-4 w-4 rounded-full border border-ink bg-white transition-all duration-150"
                  :style="{ left: row.sign_blogger ? '1.375rem' : '2px' }"
                ></span>
              </span>
            </button>
          </div>

          <!-- 缺字风险药丸 -->
          <div v-if="riskOf(row).level === 'high' && row.cn.trim()" class="mt-2 flex items-center gap-2 rounded-xl border-2 border-coral bg-blush px-3 py-2">
            <svg aria-hidden="true" class="h-4 w-4 shrink-0 text-coral" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
              <path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"></path>
              <line x1="12" y1="9" x2="12" y2="13"></line>
              <line x1="12" y1="17" x2="12.01" y2="17"></line>
            </svg>
            <p class="text-[11px] font-black text-danger">
              检测到生僻字「{{ riskOf(row).missing.join('、') }}」，将启用系统回退字体（{{ Object.values(riskOf(row).fallbackMap).join('、') }}）
            </p>
          </div>
        </div>

        <!-- 新增行 -->
        <button
          type="button"
          class="flex min-h-[48px] w-full items-center justify-center gap-2 rounded-2xl border-2 border-dashed border-ink/40 text-xs font-black text-ink-muted transition-all hover:border-ink hover:bg-sky/50 hover:text-ink active:scale-[0.99]"
          :disabled="rows.length >= MAX_ROWS"
          @click="addRow()"
        >
          <svg aria-hidden="true" class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round">
            <path d="M12 5v14M5 12h14"></path>
          </svg>
          {{ rows.length >= MAX_ROWS ? '已达单次 200 行上限' : '新增一行' }}
        </button>
      </div>
    </section>

    <div v-if="draftCancellation && !previewOpen" class="mx-4 mb-24 rounded-xl border border-amber bg-amber/10 p-3 text-xs" role="status">
      <p class="break-all font-mono">原草稿任务：{{ draftCancellation.jobId }}</p>
      <p class="mt-1 font-bold">{{ draftCancellation.message }}</p>
      <button v-if="draftCancellation.status === 'unknown'" type="button" class="btn-hard mt-2" @click="refreshDraftCancellation">刷新取消状态</button>
    </div>

    <!-- 底部控制台 -->
    <footer class="console-bar !relative shrink-0 flex-wrap">
      <div v-if="generationBlocker && !pendingRenderBody && !submitting" role="status" class="w-full text-xs leading-relaxed" data-testid="batch-generation-blocker">
        <p class="font-bold text-danger">{{ generationBlocker }}</p>
        <a :href="!renderSelection ? '#batch-render-settings' : '#batch-typography-settings'" class="mt-1 inline-block font-bold underline">查看并修复生成设置</a>
      </div>
      <span class="shrink-0 rounded-full bg-canvas px-2.5 py-1 font-mono text-[11px] font-black">
        {{ rows.filter((r) => r.cn.trim()).length }} / {{ MAX_ROWS }}
      </span>
      <div class="flex flex-1 items-center justify-end gap-2.5">
        <button type="button" class="btn-hard !min-h-[48px] flex-1 text-xs" :disabled="submitting || draftCancelPending || Boolean(pendingRenderBody) || !renderSelection || !typographySelection" @click="submitJob(true)">
          预览草稿
        </button>
        <button
          type="button"
          class="btn-hard btn-hard--primary !min-h-[48px] flex-[1.4] text-xs"
          :disabled="submitting || draftCancelPending || (!pendingRenderBody && (!renderSelection || !typographySelection))"
          @click="submitJob(false)"
        >
          <svg
            v-if="submitting"
            aria-hidden="true"
            class="h-4 w-4 motion-safe:animate-spin"
            viewBox="0 0 24 24"
            fill="none"
          >
            <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
            <path class="opacity-90" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z"></path>
          </svg>
          {{ submitting ? '创建任务中…' : pendingRenderBody ? '重试原提交' : '正式生成' }}
        </button>
      </div>
    </footer>


    <!-- 草稿预览抽屉（C-27：draft 车道真渲染预览，非正式结果） -->
    <div
      v-if="previewOpen"
      class="motion-fade fixed inset-0 z-40 bg-ink/50"
      @click.self="previewOpen = false; stopDraftPreview()"
    >
      <div
        class="sheet-panel safe-b fixed inset-x-0 bottom-0 z-50 max-h-[85vh] overflow-y-auto p-5"
        role="dialog"
        aria-modal="true"
        aria-label="批量生成草稿预览"
      >
        <div class="flex items-start justify-between">
          <div>
            <h2 class="flex items-center gap-2 text-lg font-black tracking-tight">
              草稿渲染预览
              <span class="tag-code">{{ filledRows.length }} 单</span>
            </h2>
            <p class="mt-0.5 font-mono text-[10px] font-bold text-ink-muted">
              draft 车道 · 非正式结果 · 24h 自动清理（Idempotency-Key {{ draftIdempotencyKey.slice(0, 13) }}… 与正式提交无关）
            </p>
          </div>
          <button
            type="button"
            aria-label="关闭预览"
            class="flex h-11 w-11 items-center justify-center rounded-full border-2 border-ink bg-surface transition-all hover:bg-ink hover:text-white active:scale-95"
            @click="previewOpen = false; stopDraftPreview()"
          >
            <svg aria-hidden="true" class="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"></path></svg>
          </button>
        </div>

        <!-- 草稿渲染状态条 -->
        <div class="mt-3">
          <div
            v-if="draftPhase === 'creating'"
            class="flex items-center gap-2 rounded-xl border-2 border-ink bg-sky/40 px-3 py-2 text-xs font-black"
            role="status"
          >
            <svg aria-hidden="true" class="h-4 w-4 motion-safe:animate-spin" viewBox="0 0 24 24" fill="none">
              <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
              <path class="opacity-90" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z"></path>
            </svg>
            正在提交草稿渲染任务…
          </div>
          <div
            v-else-if="draftPhase === 'rendering'"
            class="flex items-center justify-between gap-2 rounded-xl border-2 border-ink bg-sky/40 px-3 py-2 text-xs font-black"
            role="status"
          >
            <span class="flex items-center gap-2">
              <span aria-hidden="true" class="h-2 w-2 animate-pulse rounded-full bg-primary motion-reduce:animate-none"></span>
              草稿渲染中（低优先级车道，正式任务优先）…
            </span>
            <span class="font-mono text-[10px] font-bold text-ink-muted">{{ draftItems.filter((i) => draftImageUrl(draftItems.indexOf(i)) !== null).length }}/{{ draftItems.length }} 张就绪</span>
          </div>
          <div
            v-else-if="draftPhase === 'ready'"
            class="flex items-center gap-2 rounded-xl border-2 border-mint bg-mint/40 px-3 py-2 text-xs font-black text-emerald-800"
            role="status"
          >
            <svg aria-hidden="true" class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.8" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"></path></svg>
            草稿渲染完成 · 以下为真实渲染样张（非正式结果）{{ draftHasWarnings ? ' · 含渲染提示，请查看逐行记录' : '' }}
          </div>
          <div
            v-else-if="draftPhase === 'failed'"
            class="flex items-center justify-between gap-2 rounded-xl border-2 border-coral bg-blush px-3 py-2"
            role="alert"
          >
            <p class="text-xs font-black text-danger">草稿渲染失败：{{ draftError }}</p>
            <button
              v-if="jobTemplate || rowsHaveTemplates"
              type="button"
              class="btn-hard btn-hard--compact shrink-0"
              @click="startDraftPreview"
            >
              重试草稿渲染
            </button>
          </div>
          <div v-else-if="draftPhase === 'cancelled'" class="rounded-xl border-2 border-ink bg-canvas px-3 py-2 text-xs font-bold" role="status">草稿生成已取消 · 服务端已确认，未重新创建任务</div>
        </div>

        <button v-if="draftPhase === 'creating' || draftPhase === 'rendering'" type="button" class="btn-hard mt-3 w-full" :disabled="draftPhase !== 'rendering' || !draftJobId || draftCancelPending || submitting" @click="cancelDraftPreview">{{ draftCancelPending ? '正在取消草稿…' : '取消草稿生成' }}</button>
        <div v-if="draftCancellation" class="mt-3 rounded-xl border border-amber bg-amber/10 p-3 text-xs" role="status">
          <p class="break-all font-mono">原草稿任务：{{ draftCancellation.jobId }}</p>
          <p class="mt-1 font-bold">{{ draftCancellation.message }}</p>
          <button v-if="draftCancellation.status === 'unknown'" type="button" class="btn-hard mt-2" @click="refreshDraftCancellation">刷新取消状态</button>
        </div>

        <FrozenInkSummary v-if="draftFrozenSettings" :settings="draftFrozenSettings" class="rounded-xl border border-ink/20 bg-surface p-3" />

        <!-- 底图与覆盖汇总 -->
        <div class="mt-4 flex items-center gap-3 rounded-xl border-2 border-ink bg-sky/40 p-3">
          <span class="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl border-2 border-ink bg-surface">
            <svg aria-hidden="true" class="h-5 w-5" viewBox="0 0 100 100" fill="none" stroke="currentColor" stroke-width="4">
              <circle cx="50" cy="38" r="17"></circle>
              <path d="M18 92c4-20 17-30 32-30s28 10 32 30" stroke-linecap="round"></path>
            </svg>
          </span>
          <div class="min-w-0">
            <p class="truncate text-xs font-black">底图 {{ selectedAsset?.asset_id ?? '未选择' }}</p>
            <p class="font-mono text-[10px] font-bold text-ink-muted">{{ selectedAsset?.width }}×{{ selectedAsset?.height }}</p>
          </div>
          <span
            class="ml-auto shrink-0 rounded-full border-2 px-2.5 py-1 text-[10px] font-black"
            :class="riskSummary.high > 0 ? 'border-amber bg-amber/20' : 'border-mint bg-mint/40 text-emerald-800'"
          >{{ riskSummary.high > 0 ? `缺字 ${riskSummary.high} 行` : '字形全覆盖' }}</span>
        </div>

        <!-- 行清单（缩略图 = 草稿真渲染图，就绪前显示 CN 文本占位） -->
        <div class="mt-3 max-h-56 space-y-2 overflow-y-auto rounded-xl border border-ink/15 bg-canvas p-2.5">
          <div
            v-for="(row, i) in filledRows"
            :key="`pv-${i}`"
            class="flex flex-wrap items-center gap-2 rounded-lg bg-surface px-2.5 py-2"
          >
            <button
              type="button"
              class="relative flex h-11 w-11 shrink-0 items-center justify-center overflow-hidden rounded-lg border-2 border-ink bg-sky/60 transition-all duration-150 hover:scale-105 hover:bg-sky active:scale-95"
              :aria-label="`放大预览第 ${i + 1} 行 To 签效果`"
              @click="lightboxRow = i"
            >
              <img
                v-if="draftImageUrl(i)"
                :src="draftImageUrl(i) ?? ''"
                :alt="`${row.cn} 草稿渲染图`"
                class="absolute inset-0 h-full w-full object-cover"
                draggable="false"
              >
              <span v-else class="px-0.5 text-center text-[8px] font-black leading-tight" :class="{ 'whitespace-break-spaces': naturalTextEditing }">{{ row.cn }}</span>
              <span aria-hidden="true" class="absolute bottom-0 right-0 rounded-tl-md bg-ink px-0.5 text-[7px] font-black text-white">放大</span>
            </button>
            <span class="w-8 shrink-0 font-mono text-[10px] font-black text-ink-muted">#{{ i + 1 }}</span>
            <span class="min-w-0 flex-1 text-xs font-black" :class="naturalTextEditing ? 'whitespace-break-spaces break-all' : 'truncate'" data-testid="draft-cn">{{ row.cn }}</span>
            <span
              v-if="!draftImageUrl(i)"
              class="shrink-0 rounded-md bg-sky px-1.5 py-0.5 text-[9px] font-black text-ink"
            >{{ draftPhase === 'cancelled' ? '已取消' : draftItemFor(i)?.status === 'failed' || draftItemFor(i)?.last_error ? '渲染失败' : draftPhase === 'failed' ? '未出图' : '渲染中' }}</span>
            <span v-if="draftFallbackUsed(i) || draftItemFor(i)?.warnings?.length" class="shrink-0 rounded-md border border-amber bg-amber/20 px-1.5 py-0.5 text-[9px] font-black text-ink">{{ draftWarningLabel(i) }}</span>
            <span class="shrink-0 rounded-md bg-canvas px-1.5 py-0.5 text-[10px] font-bold text-ink-muted">{{ blessingTextOf(row.blessing_id) }}</span>
            <span
              v-if="rowRisk(row.cn.trim()).level === 'high'"
              class="shrink-0 rounded-md border border-coral bg-blush px-1.5 py-0.5 text-[9px] font-black text-coral"
            >缺字回退</span>
            <p v-if="draftItemFor(i)?.last_error" class="w-full break-all text-xs font-bold text-danger">{{ draftItemFor(i)!.last_error }}</p>
            <details v-if="draftItemFor(i)?.warnings?.length" class="w-full break-all text-[10px] text-ink-muted">
              <summary class="cursor-pointer font-bold">查看渲染记录</summary>
              <ul class="mt-1 space-y-1"><li v-for="(warning, warningIndex) in draftItemFor(i)!.warnings" :key="warningIndex">{{ warning }}</li></ul>
            </details>
          </div>
          <p v-if="filledRows.length === 0" class="py-4 text-center text-xs font-bold text-ink-muted">暂无有效行，请先录入粉丝昵称</p>
        </div>

        <!-- 决策 -->
        <div class="mt-4 flex items-center justify-between gap-3">
          <button type="button" class="btn-hard w-[45%]" @click="previewOpen = false; stopDraftPreview()">继续编辑</button>
          <button
            type="button"
            class="btn-hard btn-hard--primary w-[45%]"
            :disabled="filledRows.length === 0 || submitting || draftCancelPending"
            @click="previewOpen = false; stopDraftPreview(); submitJob(false)"
          >
            确认正式生成
          </button>
        </div>
      </div>
    </div>

    <!-- 放大预览灯箱：点击缩略图打开；ESC / 点遮罩 / 关闭键退出；正式渲染待 GET /render-jobs/preview（清单 §9） -->
    <div
      v-if="lightboxRow !== null && filledRows[lightboxRow]"
      class="motion-fade fixed inset-0 z-[70] flex items-center justify-center bg-ink/60 p-6"
      role="dialog"
      aria-modal="true"
      aria-label="To 签成品放大预览"
      @click.self="lightboxRow = null"
    >
      <div class="motion-rise w-full max-w-xs">
        <div aria-hidden="true" class="absolute inset-0 translate-x-1.5 translate-y-1.5 rounded-2xl bg-ink-soft"></div>
        <div class="relative overflow-hidden rounded-2xl border-2 border-ink bg-surface">
          <!-- 草稿真渲染图就绪即整幅展示；未就绪/失败降级为文本合成占位（不冒充渲染结果） -->
          <img
            v-if="draftImageUrl(lightboxRow)"
            :src="draftImageUrl(lightboxRow) ?? ''"
            :alt="`${filledRows[lightboxRow].cn} 草稿渲染预览`"
            class="w-full"
            draggable="false"
          >
          <div
            v-else
            class="bg-blueprint relative flex items-center justify-center"
            :style="{ aspectRatio: aspectRatioValue(selectedAsset?.width ?? 3, selectedAsset?.height ?? 4) }"
          >
            <span class="absolute left-2 top-2 rounded border border-ink bg-surface px-1.5 py-0.5 font-mono text-[9px] font-black">
              {{ draftPhase === 'cancelled' ? '草稿生成已取消' : draftPhase === 'failed' ? '草稿渲染失败' : '草稿渲染中…' }}
            </span>
            <span
              v-if="rowRisk(filledRows[lightboxRow].cn.trim()).level === 'high'"
              class="absolute right-2 top-2 rounded border border-coral bg-blush px-1.5 py-0.5 text-[9px] font-black text-coral"
            >缺字回退</span>
            <div class="px-5 text-center">
              <p class="font-mono text-[9px] font-black uppercase tracking-[0.3em] text-ink-muted">To.</p>
              <p class="mt-1 break-all text-3xl font-black leading-tight tracking-tight" :class="{ 'whitespace-break-spaces': naturalTextEditing }">{{ filledRows[lightboxRow].cn }}</p>
              <p class="mt-2 text-sm font-bold text-ink-muted">{{ blessingTextOf(filledRows[lightboxRow].blessing_id) }}</p>
              <p
                v-if="filledRows[lightboxRow].sign_blogger && bloggerName"
                class="mt-4 inline-block rounded-full border-2 border-ink bg-surface px-3 py-1 text-xs font-black"
              >
                @{{ bloggerName }}
              </p>
            </div>
          </div>
          <!-- 底部条：元信息 + 关闭 -->
          <div class="flex items-center justify-between border-t-2 border-ink p-3">
            <p class="font-mono text-[10px] font-bold text-ink-muted">
              #{{ lightboxRow + 1 }} · draft 样张 · 非正式结果
            </p>
            <button
              type="button"
              class="btn-hard btn-hard--compact"
              @click="lightboxRow = null"
            >
              关闭
            </button>
          </div>
        </div>
      </div>
    </div>

    <!-- Toast -->
    <div
      v-if="notice"
      role="status"
      class="pointer-events-none fixed inset-x-0 z-[60] flex justify-center px-4"
      style="bottom: calc(5.5rem + env(safe-area-inset-bottom));"
    >
      <span class="motion-fade max-w-full truncate rounded-full border-2 border-ink bg-ink px-4 py-2.5 font-mono text-[11px] font-black text-white shadow-hard">
        {{ notice }}
      </span>
    </div>
  </main>
</template>

<!-- 验收: 增删行与一键解析; 200行硬截断; risk=high珊瑚粗边警示; 底部栏safe-area; 热区≥44px; useDraft草稿; R9幂等键注释 -->
