<script setup lang="ts">
import { userError, userText, stateLabel } from '@/utils/userLanguage'
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useAuthStore } from '@/stores/auth'
import { ApiRequestError, request } from '@/api/http'
import { ENDPOINT_NATIVE_SOURCE_ADMIT, ENDPOINT_NATIVE_REVIEW_CREATE, ENDPOINT_NATIVE_REVIEW_LIST, ENDPOINT_NATIVE_REVIEW_DETAIL, ENDPOINT_NATIVE_REVIEW_APPLY, ENDPOINT_NATIVE_REVIEW_CANCEL, fill } from '@/api/endpoints'
import { assertJob, exportNativeReviewRequest, imagePath, isBox, isHash, isJobId, sameNativeItems } from '@/api/nativeIntake'
import type { IntakeAsset, IntakeStyle, NativeDraftInput, NativeReviewJob, NativeReviewExport, NativeJobList, NativeJobSummary, SourceAdmission, SourceDeclaration } from '@/types/nativeIntake'
import NativeOccurrenceSelection from './NativeOccurrenceSelection.vue'
import NativeReviewIngress from './NativeReviewIngress.vue'

const props = defineProps<{ asset: IntakeAsset | null; profile: IntakeStyle | null; drafts: NativeDraftInput[]; sourceBusy?: boolean }>()
const emit = defineEmits<{ applied: [] }>()
const auth = useAuthStore()
const userId = computed(() => auth.me?.user_id ?? '')
const scope = computed(() => `${userId.value}:${props.profile?.style_id ?? ''}:${props.asset?.asset_id ?? ''}:${props.asset?.sha256 ?? ''}`)
const styleScope = computed(() => `${userId.value}:${props.profile?.style_id ?? ''}`)
let generation = 0
let jobReadGeneration = 0
let listReadGeneration = 0
let disposed = false
let exportController: AbortController | null = null
const busy = ref<string | null>(null)
const ingressBusy = ref(false)
const ingressUncertain = ref(false)
const ingressBlocked = ref(true)
const navigationBusy = computed(() => Boolean(busy.value) || ingressBusy.value)
const anyBusy = computed(() => Boolean(busy.value) || ingressBusy.value || ingressUncertain.value)
const error = ref<string | null>(null)
const notice = ref('')
const checked = ref(false)
const reason = ref('')
const declaration = ref<SourceDeclaration | null>(null)
const admission = ref<SourceAdmission | null>(null)
const selectedDrafts = ref<string[]>([])
const job = ref<NativeReviewJob | null>(null)
const jobFresh = ref(false)
const jobLoading = ref(false)
const jobInput = ref('')
const jobList = ref<NativeJobSummary[]>([])
const listLoading = ref(false)
const listError = ref<string | null>(null)
const nextCursor = ref<string | null>(null)
const listLoaded = ref(false)
const uncertainCreate = ref<{ style_id: number; items: NativeDraftInput[] } | null>(null)
const failedImages = ref<string[]>([])
const imageGeneration = ref(0)
const imageRecoveryRequired = ref(false)
const loadedImages = ref<string[]>([])
const occurrenceRefresh = ref(0)
const lastExport = ref<Pick<NativeReviewExport, 'filename' | 'archive_sha256' | 'package_sha256' | 'code_version_matches'> | null>(null)
const path = (template: string, id: string) => fill(template, { id: encodeURIComponent(id) })
const msg = (value: unknown) => value instanceof ApiRequestError ? userError(value) : value instanceof Error ? value.message : String(value)
const current = (g: number, key: string) => !disposed && g === generation && key === scope.value
const storageKey = (kind: string, key = scope.value) => `yy:p4:native:${kind}:${key}`
function store(kind: string, value: unknown, key = scope.value): void {
  try { if (value === null) localStorage.removeItem(storageKey(kind, key)); else localStorage.setItem(storageKey(kind, key), JSON.stringify(value)) }
  catch { if (key === scope.value) error.value = '浏览器无法保存恢复记录；请保留任务编号，刷新后从服务端任务列表恢复' }
}
function saved(kind: string): unknown { try { return JSON.parse(localStorage.getItem(storageKey(kind)) ?? 'null') } catch { return null } }
const validDrafts = computed(() => props.drafts.filter((item) => item.source_asset_id === props.asset?.asset_id && isBox(item.source_bbox_norm)
  && /^[A-Za-z0-9_-]{1,80}$/.test(item.candidate_id) && new RegExp(`^users/${userId.value}/styles/drafts/[0-9a-f]{16}\\.png$`).test(item.draft_alpha_path)))
const draftInputs = computed(() => validDrafts.value.filter((item) => selectedDrafts.value.includes(item.candidate_id)))
const canAdmit = computed(() => Boolean(props.profile && props.asset && isHash(props.asset.sha256) && ['sample_source', 'cos'].includes(props.asset.type)
  && checked.value && reason.value.trim() && reason.value.trim().length <= 1000 && !anyBusy.value && !props.sourceBusy))
const canCreate = computed(() => Boolean(props.profile && props.asset && !anyBusy.value && !props.sourceBusy && !uncertainCreate.value && !jobLoading.value
  && draftInputs.value.length > 0 && draftInputs.value.length <= 100 && (!job.value || ['applied', 'cancelled'].includes(job.value.status))))
const imagesRecovered = computed(() => !imageRecoveryRequired.value || Boolean(job.value && loadedImages.value.length === job.value.package.items.length * 2))
const canApply = computed(() => Boolean(jobFresh.value && job.value?.status === 'pending' && job.value.code_version_matches === true && isHash(job.value.imported_bundle_sha256)
  && props.profile?.status === 'approved' && !ingressBlocked.value && imagesRecovered.value && !anyBusy.value && !jobLoading.value && !failedImages.value.length))
const canExport = computed(() => Boolean(jobFresh.value && job.value?.capabilities?.request_export === true && typeof job.value.code_version_matches === 'boolean'
  && !anyBusy.value && !jobLoading.value && !props.sourceBusy && !uncertainCreate.value))
const statusLabel = computed(() => !job.value ? '尚未创建或打开任务' : job.value.status === 'applied' ? '已应用服务端核验的 AI 结果'
  : job.value.status === 'cancelled' ? '已取消，原证据保留' : job.value.code_version_matches === false ? '任务绑定的代码版本已变化，不能应用'
    : job.value.imported_bundle_sha256 ? '受信证据已导入，等待显式应用' : '等待真实 AI 像素复核及受信导入')

async function admit(): Promise<void> {
  if (!canAdmit.value || !props.profile || !props.asset) return
  const g = generation; const key = scope.value; const asset = props.asset; const styleId = props.profile.style_id
  const body: SourceDeclaration = declaration.value ?? { style_id: styleId, source_sha256: asset.sha256, origin: 'user_supplied_attachment', history_status: 'UNKNOWN', allow_unknown_history: true, reason: reason.value.trim(), actor_kind: 'delegated_ai' }
  declaration.value = body; store('declaration', body, key); busy.value = 'admit'; error.value = null
  try {
    const result = await request<SourceAdmission>(path(ENDPOINT_NATIVE_SOURCE_ADMIT.path, asset.asset_id), { method: 'POST', body, retries: 0 })
    if (!current(g, key)) return
    if (result.schema !== 'personal-source-admission/1' || String(result.user_id) !== userId.value || result.style_id !== styleId || result.asset_id !== asset.asset_id
      || result.source_sha256 !== asset.sha256 || result.history_status !== 'UNKNOWN' || result.actor_kind !== 'delegated_ai' || result.does_not_approve_glyphs !== true || !isHash(result.receipt_sha256)) throw new Error('来源准入收据与当前账号、档案或原图不一致')
    admission.value = result; notice.value = '来源已按既有授权登记。使用历史未知；没有确认字形或训练模型。'
  } catch (e) { if (current(g, key)) error.value = msg(e) }
  finally { if (current(g, key)) busy.value = null }
}
async function listJobs(more = false): Promise<void> {
  if (!props.profile || !userId.value || listLoading.value) return
  const g = generation; const key = scope.value; const styleId = props.profile.style_id
  const cursor = more ? nextCursor.value : null
  if (more && !cursor) return
  const listRead = ++listReadGeneration
  listLoading.value = true; listError.value = null
  try {
    const result = await request<NativeJobList>(`${ENDPOINT_NATIVE_REVIEW_LIST.path}?style_id=${styleId}&limit=50${cursor ? `&after_job_id=${encodeURIComponent(cursor)}` : ''}`, { retries: 0 })
    if (!current(g, key) || listRead !== listReadGeneration) return
    if (!Array.isArray(result.items) || result.items.some((row) => !isJobId(row.job_id) || row.style_id !== styleId) || (result.next_after_job_id != null && !isJobId(result.next_after_job_id))) throw new Error('任务列表身份或分页数据无效')
    jobList.value = [...new Map([...(more ? jobList.value : []), ...result.items].map((row) => [row.job_id, row])).values()]
    nextCursor.value = result.next_after_job_id; listLoaded.value = true
  } catch (e) { if (current(g, key) && listRead === listReadGeneration) listError.value = msg(e) }
  finally { if (current(g, key) && listRead === listReadGeneration) listLoading.value = false }
}
async function loadJob(id = jobInput.value, reloadPixels = true): Promise<NativeReviewJob | null> {
  if (!props.profile || !isJobId(id) || !userId.value) { error.value = '请选择有效的当前档案审核任务'; return null }
  const g = generation; const key = scope.value; const read = ++jobReadGeneration; const styleId = props.profile.style_id
  jobLoading.value = true; jobFresh.value = false; error.value = null; lastExport.value = null
  try {
    const result = await request<NativeReviewJob>(path(ENDPOINT_NATIVE_REVIEW_DETAIL.path, id), { retries: 0 })
    if (!current(g, key) || read !== jobReadGeneration) return null
    assertJob(result, userId.value, styleId, id)
    const pixelsChanged = job.value?.job_id !== id || job.value?.package_sha256 !== result.package_sha256
    job.value = result; jobFresh.value = true; jobInput.value = id
    if (reloadPixels || pixelsChanged) { failedImages.value = []; loadedImages.value = []; imageGeneration.value += 1 }
    store('job', id, key)
    return result
  } catch (e) { if (current(g, key) && read === jobReadGeneration) error.value = msg(e); return null }
  finally { if (current(g, key) && read === jobReadGeneration) jobLoading.value = false }
}
async function exportRequest(): Promise<void> {
  if (!canExport.value || !job.value) return
  const g = generation; const key = scope.value; const prior = job.value
  busy.value = 'export'; error.value = null; lastExport.value = null
  const controller = new AbortController(); exportController = controller
  const timer = window.setTimeout(() => controller.abort(), 60000)
  try {
    const latest = await loadJob(prior.job_id, false)
    if (!latest || !current(g, key)) return
    if (latest.package_sha256 !== prior.package_sha256) throw new Error('服务端任务包已变化；请核对当前任务后重新导出')
    const read = jobReadGeneration
    const result = await exportNativeReviewRequest(latest, controller.signal)
    if (!current(g, key) || read !== jobReadGeneration || job.value?.job_id !== latest.job_id || job.value.package_sha256 !== latest.package_sha256) return
    const url = URL.createObjectURL(result.blob)
    const anchor = document.createElement('a')
    try {
      anchor.href = url; anchor.download = result.filename; document.body.appendChild(anchor); anchor.click()
    } finally { anchor.remove(); window.setTimeout(() => URL.revokeObjectURL(url), 1000) }
    lastExport.value = { filename: result.filename, archive_sha256: result.archive_sha256, package_sha256: result.package_sha256, code_version_matches: result.code_version_matches }
    notice.value = '审核请求文件校验通过，已请求浏览器下载。导出没有提交视觉审核、导入评分、确认字形或训练模型。'
  } catch (e) {
    if (current(g, key)) error.value = e instanceof DOMException && e.name === 'AbortError' ? '审核请求导出已中断或超时；没有确认下载完成，请重新读取任务后重试' : msg(e)
  } finally {
    window.clearTimeout(timer)
    if (exportController === controller) exportController = null
    if (current(g, key)) busy.value = null
  }
}
const canResolveUncertain = computed(() => Boolean(uncertainCreate.value && job.value && jobFresh.value && listLoaded.value && !nextCursor.value && !listError.value && !listLoading.value
  && !jobLoading.value && !anyBusy.value && sameNativeItems(job.value.package.items, uncertainCreate.value.items)))
function adoptMatchingJob(): void {
  if (!canResolveUncertain.value || !job.value) return
  uncertainCreate.value = null; store('uncertain-create', null)
  notice.value = '已按你的明确选择使用此匹配任务继续；其他任务和旧证据仍保留，不会重发原创建请求。'
}

async function createJob(): Promise<void> {
  if (!canCreate.value || !props.profile) return
  const g = generation; const key = scope.value; const body = JSON.parse(JSON.stringify({ style_id: props.profile.style_id, items: draftInputs.value })) as { style_id: number; items: NativeDraftInput[] }
  // No create idempotency exists. Persist before send, and never auto-replay an uncertain outcome.
  uncertainCreate.value = body; store('uncertain-create', body, key); busy.value = 'create'; error.value = null
  // Recovery must read the server after this uncertain operation, not reuse pre-create pages or a historical job read.
  ++listReadGeneration; listLoading.value = false; listLoaded.value = false; nextCursor.value = null; listError.value = null
  ++jobReadGeneration; jobFresh.value = false; jobLoading.value = false
  try {
    const result = await request<NativeReviewJob>(ENDPOINT_NATIVE_REVIEW_CREATE.path, { method: 'POST', body, retries: 0, timeoutMs: 60000 })
    assertJob(result, key.split(':')[0]!, body.style_id)
    if (!sameNativeItems(result.package.items, body.items)) throw new Error('返回的审核任务不属于所提交的原生提取稿')
    store('job', result.job_id, key); store('uncertain-create', null, key)
    if (!current(g, key)) return
    uncertainCreate.value = null; job.value = result; jobInput.value = result.job_id; jobFresh.value = false
    await loadJob(result.job_id); if (current(g, key)) await listJobs()
  } catch (e) {
    if (e instanceof ApiRequestError && e.status >= 400 && e.status < 500) { store('uncertain-create', null, key); if (current(g, key)) uncertainCreate.value = null }
    if (current(g, key)) error.value = msg(e)
  } finally { if (current(g, key)) busy.value = null }
}
async function mutateJob(action: 'apply' | 'cancel'): Promise<void> {
  if (!job.value || anyBusy.value || jobLoading.value || (action === 'apply' ? !canApply.value : !jobFresh.value || job.value.status !== 'pending')) return
  const g = generation; const key = scope.value; const prior = job.value
  busy.value = action; error.value = null
  try {
    const latest = await loadJob(prior.job_id, false)
    if (!latest || !current(g, key)) return
    if (latest.package_sha256 !== prior.package_sha256 || latest.imported_bundle_sha256 !== prior.imported_bundle_sha256 || latest.status !== 'pending') {
      error.value = '服务端任务状态或证据已变化；请检查最新状态后重新选择操作'; return
    }
    if (action === 'apply' && (ingressBlocked.value || latest.code_version_matches !== true || !isHash(latest.imported_bundle_sha256) || props.profile?.status !== 'approved' || failedImages.value.length > 0 || !imagesRecovered.value)) throw new Error('当前任务尚未具备受信证据、代码版本、档案准入或图像读取条件')
    await request(path((action === 'apply' ? ENDPOINT_NATIVE_REVIEW_APPLY : ENDPOINT_NATIVE_REVIEW_CANCEL).path, prior.job_id), {
      method: 'POST', ...(action === 'apply' ? { body: { bundle_sha256: latest.imported_bundle_sha256 } } : {}), retries: 0, timeoutMs: 60000,
    })
    if (!current(g, key)) return
    await loadJob(prior.job_id)
    if (current(g, key)) { occurrenceRefresh.value += 1; emit('applied'); await listJobs() }
  } catch (e) {
    if (!current(g, key)) return
    const failure = msg(e)
    await loadJob(prior.job_id)
    if (current(g, key)) error.value = failure + '；已尝试重新读取服务端结果，不会重复应用或假定成功'
  } finally { if (current(g, key)) busy.value = null }
}
function pixelStatus(event: Event, candidateId: string, kind: string, okay: boolean): void {
  const target = event.target as HTMLImageElement
  if (target.dataset.imageGeneration !== String(imageGeneration.value) || target.dataset.jobId !== job.value?.job_id) return
  if (okay) loadedImages.value = [...new Set([...loadedImages.value, `${candidateId}:${kind}`])]
  else { failedImages.value.push(candidateId); imageRecoveryRequired.value = true }
}

function leaveReview(): void {
  if (navigationBusy.value || !job.value) return
  // Invalidate late selected-job reads; only the pointer is cleared, never per-job recovery records.
  ++jobReadGeneration; jobLoading.value = false
  job.value = null; jobFresh.value = false; jobInput.value = ''; lastExport.value = null; store('job', null)
}
function newReview(): void {
  if (!job.value || !['applied', 'cancelled'].includes(job.value.status)) return
  leaveReview()
}
watch(scope, () => {
  ++generation; ++jobReadGeneration; ++listReadGeneration
  exportController?.abort(); exportController = null; lastExport.value = null
  busy.value = null; ingressBusy.value = false; ingressUncertain.value = false; ingressBlocked.value = true; error.value = null; notice.value = ''; admission.value = null; checked.value = false; reason.value = ''; declaration.value = null
  job.value = null; jobFresh.value = false; jobLoading.value = false; jobInput.value = ''; selectedDrafts.value = []; uncertainCreate.value = null; failedImages.value = []; imageRecoveryRequired.value = false; loadedImages.value = []
  jobList.value = []; listLoading.value = false; listLoaded.value = false; listError.value = null; nextCursor.value = null
  const raw = saved('declaration') as SourceDeclaration | null
  if (raw && raw.style_id === props.profile?.style_id && raw.source_sha256 === props.asset?.sha256 && typeof raw.reason === 'string') { declaration.value = raw; reason.value = raw.reason }
  const pending = saved('uncertain-create') as { style_id: number; items: NativeDraftInput[] } | null
  if (pending && pending.style_id === props.profile?.style_id && Array.isArray(pending.items)) uncertainCreate.value = pending
  const last = saved('job')
  void listJobs()
  if (isJobId(last)) { jobInput.value = last; void loadJob(last) }
}, { immediate: true, flush: 'sync' })
watch(validDrafts, (items) => { selectedDrafts.value = selectedDrafts.value.filter((id) => items.some((item) => item.candidate_id === id)) })
onBeforeUnmount(() => { disposed = true; ++generation; ++jobReadGeneration; ++listReadGeneration; exportController?.abort() })
</script>

<template>
  <section class="mx-4 mb-4 space-y-4 rounded-2xl border-2 border-ink bg-surface p-4" data-testid="native-intake-panel">
    <h2 class="text-sm font-black">原生提取稿 · 自动视觉 审核与来源准入</h2>
    <p class="text-xs">档案：{{ profile ? `${profile.name} · ${stateLabel(profile.status)}` : '请先选择自己的手写来源档案' }}。提取只生成待审核稿，不会自动确认字符。</p>
    <details class="break-all text-xs"><summary>高级来源信息</summary><p>档案标识：{{ profile?.style_id }} · 原图标识：{{ asset?.asset_id }}</p><p>来源类型：{{ asset?.type }} · 文件校验码：{{ asset?.sha256 }}</p><p v-if="admission">声明者：{{ admission.actor_kind }} · 收据校验码：{{ admission.receipt_sha256 }}</p></details>
    <p v-if="error" role="alert" class="break-all text-xs font-bold text-danger">{{ error }}</p>
    <p v-if="notice" role="status" class="text-xs">{{ notice }}</p>
    <fieldset class="space-y-2 rounded-lg border border-ink/20 p-3" :disabled="anyBusy || sourceBusy || !profile || !asset">
      <legend class="text-xs font-black">1. 显式登记当前来源</legend>
      <p class="break-all text-xs">训练参考图：{{ asset ? '已选择' : '未选择' }}</p>
      <p class="text-xs">保留使用历史未知和训练参考图来源。仅按当前既有 自动视觉 授权登记此图到此档案，不能覆盖禁用来源、冒充从未使用或代替字形审核。</p>
      <label class="block text-xs">本次来源用途和范围<input v-model="reason" aria-label="来源准入理由" maxlength="1000" :readonly="Boolean(declaration)" class="mt-1 min-h-11 w-full rounded border border-ink px-2" placeholder="说明此上传原图在当前手写档案中的用途"></label>
      <p v-if="declaration" class="text-xs text-amber">已保存原声明；重试会提交同一声明，不能改写既有准入记录。</p>
      <label class="flex items-start gap-2 text-xs"><input v-model="checked" type="checkbox" aria-label="确认来源范围与未知历史">我确认当前上传原图及该档案范围，并明确保留使用历史未知；这不是人工字形确认</label>
      <button type="button" class="btn-hard min-h-11" :disabled="!canAdmit" @click="admit">{{ busy === 'admit' ? '登记中…' : admission ? '重新核验原来源声明' : '登记来源准入' }}</button>
      <p v-if="admission" class="break-all text-xs text-emerald-800" data-testid="native-admission">服务端已登记 · {{ admission.history_status === 'UNKNOWN' ? '使用历史未知' : stateLabel(admission.history_status) }}；未批准字形</p>
    </fieldset>

    <fieldset class="space-y-2 rounded-lg border border-ink/20 p-3" :disabled="anyBusy || sourceBusy || jobLoading">
      <legend class="text-xs font-black">2. 为当前未编辑提取稿创建审核任务</legend>
      <p class="text-xs">先在下方框选并提取；这里使用原图、实际框和提取底稿，不提交标签或分数。已绘画修改的稿件须走独立修图流程，或重新框选提取再审。</p>
      <p v-if="!validDrafts.length" class="text-xs text-amber">暂无身份完整的当前原生提取稿</p>
      <label v-for="(draft, index) in validDrafts" :key="draft.candidate_id" class="flex items-start gap-2 break-all text-xs"><input v-model="selectedDrafts" type="checkbox" :value="draft.candidate_id" :aria-label="`选择原生提取稿 ${index + 1}`">提取稿 {{ index + 1 }}<details><summary>高级信息</summary>{{ draft.candidate_id }} · 原图 {{ draft.source_asset_id }} · 框 {{ draft.source_bbox_norm }}</details></label>
      <p v-if="uncertainCreate" role="alert" class="text-xs font-bold text-amber" data-testid="native-uncertain-create">上次创建结果未确认，已停止再次创建。请刷新服务端任务列表，打开原任务核对；未找到时保留此状态并核查服务端，不能把不完整页面当作没有任务。</p>
      <button type="button" class="btn-hard min-h-11" :disabled="!canCreate" @click="createJob">{{ busy === 'create' ? '创建中…' : '创建当前像素审核任务' }}</button>
    </fieldset>

    <div class="space-y-2 rounded-lg border border-ink/20 p-3">
      <h3 class="text-xs font-black">3. 恢复任务与受信导入状态</h3>
      <p class="text-xs">任务创建不会自动调用未配置的视觉服务。必须由真实自动视觉 检查字形像素 和原图上下文，再经受信导入。网页不提供评分文件 上传，不会把任意文件当作可信审核。</p>
      <div class="flex flex-wrap gap-2"><button type="button" class="btn-hard min-h-11" :disabled="listLoading || navigationBusy || !profile" @click="listJobs()">刷新服务端任务列表</button><button v-if="nextCursor" type="button" class="btn-hard min-h-11" :disabled="listLoading || navigationBusy" @click="listJobs(true)">加载更多任务</button></div>
      <p v-if="listError" role="alert" class="text-xs text-danger">任务列表读取失败：{{ listError }}</p>
      <p v-else-if="listLoaded" class="text-xs">已读取 {{ jobList.length }} 条{{ nextCursor ? '，还有后续页' : '，当前列表已读完' }}；按任务标识分页，不表示最新任务排序。</p>
      <select v-model="jobInput" aria-label="服务端审核任务" class="min-h-11 w-full rounded border border-ink" :disabled="navigationBusy || jobLoading"><option value="">选择服务端任务</option><option v-for="(row, index) in jobList" :key="row.job_id" :value="row.job_id">任务 {{ index + 1 }} · {{ stateLabel(row.status) }} · {{ row.code_version_matches ? '代码匹配' : '需检查代码/记录' }} {{ row.reason ? userText(row.reason) : '' }}</option><option v-if="jobInput && !jobList.some(row => row.job_id === jobInput)" :value="jobInput">已保存任务</option></select>
      <button type="button" class="btn-hard min-h-11" :disabled="navigationBusy || jobLoading || !isJobId(jobInput)" @click="loadJob()">打开/刷新所选任务</button>
      <div v-if="uncertainCreate && job" class="rounded border border-amber p-2 text-xs">
        <p>未确认的创建不会自动重发。请读完服务端所有任务页，核对当前任务的原图/框/提取稿；相同稿件可能有多个历史任务，选择继续不代表已证明原请求唯一对应此任务。</p>
        <button type="button" class="btn-hard mt-2 min-h-11" :disabled="!canResolveUncertain" @click="adoptMatchingJob">确认用此匹配任务继续</button>
      </div>
      <article v-if="job" class="space-y-2 border-t border-ink/20 pt-3" data-testid="native-current-job">
        <details class="break-all text-xs"><summary>高级信息</summary><p>任务标识：{{ job.job_id }}</p><p>任务包校验码：{{ job.package_sha256 }}</p><p>已导入证据校验码：{{ job.imported_bundle_sha256 }}</p><p v-if="lastExport">导出文件校验码：{{ lastExport.archive_sha256 }} · {{ lastExport.package_sha256 }}</p></details>
        <p class="break-all text-xs font-black">当前任务 · {{ statusLabel }}{{ !jobFresh ? ' · 状态尚未重新核验' : '' }}</p>
        <p class="text-xs">创建时间：{{ job.package.created_at }}</p>
        <p class="break-all text-xs" data-testid="native-import-state">服务端已导入证据：{{ job.imported_bundle_sha256 ? '已导入，仍需单独应用' : '未导入；真实自动视觉复核/受信导入尚未完成' }}</p>
        <div class="space-y-2 rounded border border-ink/20 p-3" data-testid="native-review-channel">
          <h4 class="text-xs font-black">审核请求与可用渠道</h4>
          <template v-if="job.capabilities && jobFresh">
            <p class="text-xs" data-testid="native-provider-status">在线视觉服务尚未配置，当前网页不能发起自动视觉复核；可在下方显式登记审核请求。</p>
            <p class="text-xs" data-testid="native-trusted-channel">受信导入仅支持服务端操作员渠道：须先完成真实自动视觉 像素复核，再核验原始证据、审核者身份和文件校验码 并暂存。网页仅能选择已暂存的受信记录，不接受原始评分文件。</p>
            <p class="text-xs">导出包含本任务的权威请求、提取图、上下文及所属原图。文件含手写原图，请仅交给本次授权的审核方；导出不代表 自动视觉或人工验收。</p>
          </template>
          <p v-else class="text-xs text-amber" data-testid="native-channel-unknown">{{ jobFresh ? '服务端尚未返回审核请求导出与渠道能力' : '当前任务状态尚未重新核验' }}，当前无法确认是否支持导出；请刷新任务状态。</p>
          <p v-if="job.code_version_matches === false" class="text-xs text-amber" data-testid="native-export-historical">任务代码版本已变化：可以导出原始历史证据供检查，但不能将此任务用于当前受信导入或应用。</p>
          <p v-if="uncertainCreate" class="text-xs text-amber">请先核对并明确采用匹配任务，再导出审核请求。</p>
          <div class="flex flex-wrap gap-2">
            <button type="button" class="btn-hard min-h-11" :disabled="!canExport" @click="exportRequest">{{ busy === 'export' ? '正在校验并导出…' : '导出审核请求与像素压缩包' }}</button>
            <button type="button" class="btn-hard min-h-11" :disabled="navigationBusy || jobLoading" @click="loadJob(job.job_id, false)">刷新受信导入状态</button>
          </div>
          <p v-if="lastExport" class="break-all text-xs" data-testid="native-export-result">已请求下载 {{ lastExport.filename }}{{ lastExport.code_version_matches ? '' : ' · 仅供历史证据检查' }}</p>
        </div>
        <button type="button" class="btn-hard min-h-11" :disabled="navigationBusy" @click="leaveReview">离开当前审核任务（保留证据与恢复记录）</button>
        <NativeReviewIngress :key="`${scope}:${job.job_id}:${job.package_sha256}`" :job="job" :fresh="jobFresh" :disabled="Boolean(busy) || jobLoading || Boolean(sourceBusy) || Boolean(uncertainCreate)" @busy="ingressBusy = $event" @uncertain="ingressUncertain = $event" @blocked="ingressBlocked = $event" @changed="loadJob(job.job_id, false)" />
        <p v-if="profile?.status !== 'approved'" class="text-xs text-amber">该档案尚未准入：可以检查任务，但不能应用为训练候选。请在风格库显式完成来源档案准入。</p>
        <div class="grid gap-3 sm:grid-cols-2">
          <div v-for="(candidate, index) in job.package.items" :key="candidate.candidate_id" class="rounded border border-ink/20 p-2">
            <p class="text-xs font-bold">字形 {{ index + 1 }}</p>
            <div :key="`${job.job_id}:${imageGeneration}:${candidate.candidate_id}`" class="grid grid-cols-2 gap-2"><img :src="imagePath({ job_id: job.job_id, candidate_id: candidate.candidate_id }, 'alpha')" :alt="`字形 ${index + 1} 当前审核底稿`" class="h-28 w-full bg-black object-contain" :data-image-generation="imageGeneration" :data-job-id="job.job_id" @load="pixelStatus($event, candidate.candidate_id, 'alpha', true)" @error="pixelStatus($event, candidate.candidate_id, 'alpha', false)"><img :src="imagePath({ job_id: job.job_id, candidate_id: candidate.candidate_id }, 'source-context')" :alt="`字形 ${index + 1} 原图上下文`" class="h-28 w-full object-contain" :data-image-generation="imageGeneration" :data-job-id="job.job_id" @load="pixelStatus($event, candidate.candidate_id, 'source-context', true)" @error="pixelStatus($event, candidate.candidate_id, 'source-context', false)"></div>
            <details class="break-all text-[10px]"><summary>高级字形信息</summary><p>候选标识：{{ candidate.candidate_id }} · 原图标识：{{ candidate.source_asset_id }}</p><p>版本校验码：{{ candidate.candidate_version_sha256 }} · 原框：{{ candidate.source_bbox_norm }}</p></details>
            <template v-if="job.result"><div v-for="result in job.result.items.filter(row => row.candidate_id === candidate.candidate_id)" :key="result.candidate_id" class="mt-2 text-xs" data-testid="native-decision"><p class="font-bold">{{ result.canonical_sample_id ? '自动视觉授权确认' : '隔离，不进入训练样本' }} · {{ result.reviewed_label ?? '未确定字符' }} · {{ result.recommendation === 'AI_ACCEPT' ? '接受' : result.recommendation === 'AI_NEEDS_HUMAN' ? '需人工复核' : stateLabel(result.recommendation) }}</p><p>{{ userText(result.reason) }}<template v-if="result.isolation_reasons.length"> · {{ result.isolation_reasons.map(userText).join('；') }}</template></p><details><summary>高级审核证据</summary><p>审核来源：{{ result.reviewer?.provider ?? '未记录' }} / {{ result.reviewer?.model_id ?? '未记录' }} / {{ result.reviewer?.reviewer_kind ?? '未记录' }}；不是人工确认</p><p class="break-all">原始复核证据 {{ result.source_review_sha256 ?? '未记录' }}</p></details></div></template>
          </div>
        </div>
        <p v-if="failedImages.length || !imagesRecovered" role="alert" class="text-xs text-danger">审核图像无法读取；已阻止应用，请刷新核查当前证据</p>
        <p v-if="job.result" class="text-xs font-bold">服务端结果：接受 {{ job.result.accepted }}，隔离 {{ job.result.isolated }}；未训练、未发布模型</p>
        <div class="flex flex-wrap gap-2"><button type="button" class="btn-hard min-h-11" :disabled="!canApply" @click="mutateJob('apply')">应用服务端已受信导入的证据</button><button v-if="job.status === 'pending'" type="button" class="btn-hard min-h-11" :disabled="!jobFresh || anyBusy || jobLoading" @click="mutateJob('cancel')">取消此审核任务并保留证据</button><button v-if="['applied', 'cancelled'].includes(job.status)" type="button" class="btn-hard min-h-11" :disabled="navigationBusy || jobLoading" @click="newReview">准备新的审核任务</button></div>
      </article>
    </div>
    <NativeOccurrenceSelection v-if="profile" :key="styleScope" :style-id="profile.style_id" :refresh-token="occurrenceRefresh" :known-job-ids="jobList.map(row => row.job_id)" />
  </section>
</template>
