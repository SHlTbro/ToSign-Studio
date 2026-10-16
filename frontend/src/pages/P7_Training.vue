<!-- /training · Server-verified M2 training and review workflow -->
<script setup lang="ts">
import { userError, userText, stateLabel } from '@/utils/userLanguage'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ApiRequestError, request, newIdempotencyKey, type ApiEnvelope } from '@/api/http'
import { useAuthStore } from '@/stores/auth'
import M2RecipeSummary from '@/components/M2RecipeSummary.vue'
import HumanDatasetReview from '@/components/HumanDatasetReview.vue'
import SnapshotFamilyPlan from '@/components/SnapshotFamilyPlan.vue'
import UserScrPreparation from '@/components/UserScrPreparation.vue'
import OwnerDirectVisualAcceptance from '@/components/OwnerDirectVisualAcceptance.vue'
import OwnerOutputEvidence from '@/components/OwnerOutputEvidence.vue'
import PersonalRuntimeInstallation from '@/components/PersonalRuntimeInstallation.vue'
import RunRecovery from '@/components/RunRecovery.vue'
import UnpublishedModels from '@/components/UnpublishedModels.vue'
import TrainingReferenceImages from '@/components/TrainingReferenceImages.vue'
import { USER_SCR_RECIPE, WINDOWS_SCR_RECIPE, isUserScrRecipe, validBudget, type ScrPreparation } from '@/api/userScr'
import { isRegistrationId, matchesRunRuntime, recipeProblem, registrationForConfig, runtimeBinding, runtimePath, selectionProblem } from '@/api/m2Runtime'
import {
  ENDPOINT_STYLES,
  ENDPOINT_STYLE_SAMPLES,
  ENDPOINT_STYLE_RUNS,
  ENDPOINT_STYLE_RUN_DETAIL,
  ENDPOINT_STYLE_RUN_SAMPLES,
  ENDPOINT_STYLE_RUN_START,
  ENDPOINT_STYLE_RUN_CANCEL,
  ENDPOINT_STYLE_RUN_PROGRESS,
  ENDPOINT_STYLE_RUN_METRICS,
  ENDPOINT_STYLE_RUN_EVALUATE,
  ENDPOINT_STYLE_RUN_BLIND_REVIEW,
  ENDPOINT_STYLE_RUN_PUBLISH,
  ENDPOINT_STYLE_RUN_READINESS,
  ENDPOINT_STYLE_RUN_EVALUATION_START,
  ENDPOINT_STYLE_RUN_AI_REVIEW,
  ENDPOINT_TRAINING_SAMPLES_AI_CONFIRM,
  ENDPOINT_TRAINING_SETS,
  ENDPOINT_TRAINING_IMAGE_DELETE,
  ENDPOINT_TRAINING_IMAGE_FILE,
  ENDPOINT_TRAINING_SAMPLES,
  ENDPOINT_TRAINING_SAMPLES_BATCH_REVIEW,
  fill,
} from '@/api/endpoints'
import type {
  InferenceDeploymentState,
  BlindReviewSubmission,
  CandidateAiConfirmation,
  M2Runtime,
  M2RuntimeOption,
  M2WorkflowJob,
  PublishedStyleProfile,
  RunReadiness,
  SnapshotPreparation,
  EvaluateResponse,
  RunMetricsResponse,
  RunProgress,
  StyleProfile,
  StyleRun,
  StyleRunStatus,
  StyleSample,
} from '@/types/domain'

interface TrainingImageRow {
  image_id: number
  file_name: string
  width: number
  height: number
  size_bytes: number
  status: string
  uploaded_at: string | null
}
interface PageData<T> { items: T[]; page: number; page_size: number; total: number }
interface PersonalStyleSample {
  sample_id: string
  style_id: number
  text_label: string
  char_unicode: string
  source_asset_id: string
  source_asset_sha256: string | null
  source_family_id: string | null
  source_training_image_id: number | null
  source_training_image_sha256: string | null
  draft_alpha_sha256: string | null
  final_alpha_sha256: string | null
  text_region_bbox_norm: { x_norm: number; y_norm: number; w_norm: number; h_norm: number } | null
  touchup_edit_count: number | null
  style_sample_id: number | null
  review_status: 'candidate' | 'owner_confirmed' | 'ai_confirmed_authorized' | 'rejected'
  owner_confirmed_by: string | null
  owner_confirmed_at: string | null
  provenance_complete: boolean
  reviewed_by?: string | null
  reviewed_at?: string | null
  confirmation_actor_type?: 'ai' | 'human' | null
  review_provenance?: Record<string, unknown> | null
  candidate_visual_review?: { review_state: string; recommendation: string; reason?: string; isolation_reasons?: string[]; reviewer?: { provider?: string; model_id?: string; reviewer_kind?: string } }
}
type BlindScore = 0 | 1 | 2 | 3 | null

const router = useRouter()
const auth = useAuthStore()
const route = useRoute()
let contextGeneration = 0
const contextRestoring = ref(true)
const recoveryError = ref<string | null>(null)
let mounted = false
const styleContextNotice = ref('')
const foreignPendingCreate = ref(false)
const errorText = ref<string | null>(null)
const busyAction = ref<string | null>(null)
const notice = ref<string | null>(null)
let noticeTimer: number | null = null
let pollTimer: number | null = null
let disposed = false
let sampleGeneration = 0
let runtimeGeneration = 0
let runGeneration = 0
const paneErrors = ref<Record<string, string>>({})
function errorMessage(error: unknown, fallback: string): string {
  return error instanceof ApiRequestError ? userError(error)
    : error instanceof Error && error.message ? error.message : fallback
}
const lastErrorCode = ref('')
watch(errorText, value => { if (value === null) lastErrorCode.value = '' })
const errorGuidance = computed(() => {
  const text = errorText.value ? lastErrorCode.value : ''
  if (text.includes('SNAPSHOT_MISMATCH')) return '训练快照不一致，已阻止继续。请重新检查运行环境；不要修改历史收据或手填校验值。'
  if (text.includes('ARTIFACT_INVALID')) return '模型文件未通过校验，不能发布。保留此运行用于检查，修复后创建新运行。'
  if (text.includes('EVAL_NOT_PRODUCED')) return '尚未产出真实评估。训练完成后点击“运行评估”，结果可用前不会放行发布。'
  if (text.includes('AI_PROVIDER_UNAVAILABLE')) return 'AI 视觉服务当前不可用。已有复核结果仍以服务端证据为准，不能用默认分数替代。'
  return ''
})
function flash(message: string): void {
  if (disposed) return
  notice.value = message
  if (noticeTimer !== null) window.clearTimeout(noticeTimer)
  noticeTimer = window.setTimeout(() => { notice.value = null; noticeTimer = null }, 2800)
}
function showError(error: unknown, fallback: string): void {
  if (!disposed) { errorText.value = errorMessage(error, fallback); lastErrorCode.value = error instanceof ApiRequestError ? String(error.code) : '' }
}

// 训练图集：缩略图只使用服务端文件读面；列表按后端分页元数据翻页。
const images = ref<TrainingImageRow[]>([])
const imageById = ref(new Map<number, TrainingImageRow>())
const imagePage = ref(1)
const imageTotal = ref(0)
const imagePageSize = 24
const loadingImages = ref(false)
const imageIndexLoaded = ref(false)
const failedPreviewIds = ref(new Set<number>())
const deleteBusyId = ref<number | null>(null)
async function loadImages(page = imagePage.value): Promise<void> {
  loadingImages.value = true
  try {
    const d = await request<PageData<TrainingImageRow>>(`${ENDPOINT_TRAINING_SETS.path}?page=${page}&page_size=${imagePageSize}`)
    images.value = d.items
    imagePage.value = d.page
    imageTotal.value = d.total
    const all = new Map(imageById.value)
    for (const item of d.items) all.set(item.image_id, item)
    imageById.value = all
    if (!imageIndexLoaded.value) await loadImageStatusIndex(d.total)
  } catch (error) { showError(error, '训练图集加载失败') }
  finally { loadingImages.value = false }
}
async function loadImageStatusIndex(total: number): Promise<void> {
  const indexPageSize = 100
  const pageCount = Math.ceil(total / indexPageSize)
  if (total <= imagePageSize) { imageIndexLoaded.value = true; return }
  const firstPage = await request<PageData<TrainingImageRow>>(`${ENDPOINT_TRAINING_SETS.path}?page=1&page_size=${indexPageSize}`)
  const pages = await Promise.all(Array.from({ length: Math.max(0, pageCount - 1) }, (_, index) => index + 2).map((page) =>
    request<PageData<TrainingImageRow>>(`${ENDPOINT_TRAINING_SETS.path}?page=${page}&page_size=${indexPageSize}`),
  ))
  const all = new Map(imageById.value)
  for (const item of firstPage.items) all.set(item.image_id, item)
  for (const page of pages) for (const item of page.items) all.set(item.image_id, item)
  imageById.value = all
  imageIndexLoaded.value = true
}
const imagePageCount = computed(() => Math.max(1, Math.ceil(imageTotal.value / imagePageSize)))
function imageUrl(id: number): string { return fill(ENDPOINT_TRAINING_IMAGE_FILE.path, { id: String(id) }) }
function markPreviewFailed(id: number): void { failedPreviewIds.value = new Set(failedPreviewIds.value).add(id) }
async function deleteImage(image: TrainingImageRow): Promise<void> {
  if (!window.confirm(`确认删除训练图“${image.file_name}”？如果它被运行锁定，服务端会拒绝删除。`)) return
  deleteBusyId.value = image.image_id
  errorText.value = null
  try {
    await request(fill(ENDPOINT_TRAINING_IMAGE_DELETE.path, { id: String(image.image_id) }), { method: 'DELETE' })
    imageById.value.delete(image.image_id)
    await loadImages(imagePage.value)
    await loadStyleSamples()
    flash('训练图已删除')
  } catch (error) { showError(error, '训练图删除失败') }
  finally { deleteBusyId.value = null }
}

// M2 uses the verified server manifest; M1 remains a separate frozen inference mode.
const styles = ref<StyleProfile[]>([])
const selectedStyleId = ref<number | null>(null)
const loadingStyles = ref(false)
const styleSamples = ref<StyleSample[]>([])
const loadingSamples = ref(false)
const samplesError = ref<string | null>(null)
const humanInputBusy = ref(false)
const humanInputRevision = ref(0)
const selectedChar = ref('')
const engineChoice = ref<'m1' | 'm2'>('m2')
const runtime = ref<M2Runtime | null>(null)
const selectedRecipe = ref('m2-control-corrected-v1')
const snapshotSha = ref('')
const maxSeconds = ref(3600)
const genericRecipeSelected = computed(() => isUserScrRecipe(selectedRecipe.value))
const selectedRuntimeRegistration = ref('')
const runtimeOptions = ref<M2RuntimeOption[]>([])
const registeredRuntimeOptions = computed(() => runtimeOptions.value.filter((option) => option.registration_id && isRegistrationId(option.registration_id)))
const missingSelectedRuntime = computed(() => Boolean(selectedRuntimeRegistration.value
  && !registeredRuntimeOptions.value.some((option) => option.registration_id === selectedRuntimeRegistration.value)))
const runtimeSelectionProblem = computed(() => selectionProblem(runtime.value, selectedRuntimeRegistration.value, selectedStyleId.value)
  ?? (runtime.value?.config && runtime.value.config.recipe_id !== selectedRecipe.value ? '服务端返回了不同配方；已阻止创建，不会替换为旧配方' : null)
  ?? (genericRecipeSelected.value && !isRegistrationId(selectedRuntimeRegistration.value) ? '个人快照 SCR 必须选择合格的注册环境' : null)
  ?? (runtime.value?.ready ? recipeProblem(runtime.value.config) : null))
const runtimeReady = computed(() => Boolean(!runtimeLoading.value && !runtimeError.value && !runtimeSelectionProblem.value
  && runtime.value?.ready && runtime.value.capabilities.create_m2_run && runtime.value.config))
const runtimeLoading = ref(false)
const runtimeError = ref<string | null>(null)
const readiness = ref<RunReadiness | null>(null)
const humanReviewOpen = ref(false)
const candidateHumanMode = ref(false)
const selectedSampleIds = ref<number[]>([])
const stylesError = ref<string | null>(null)
const selectedStyle = computed(() => styles.value.find((style) => style.style_id === selectedStyleId.value) ?? null)
function styleDisplayName(style: StyleProfile): string {
  return style.source_type === 'M2_ADAPTER' && style.model_version === 'adapter@017881442b7f8b90'
    && style.receipt_id === 'imported_inference/16ec01496e0b472b9f561ce383a7ed33' ? '经典手写 V3' : style.name
}
const sampleChars = computed(() => [...new Set(styleSamples.value.map((sample) => sample.text_label).filter((label): label is string => Boolean(label && label.length === 1)))].sort())
const charSamples = computed(() => styleSamples.value.filter((sample) => sample.text_label === selectedChar.value))
const eligibleSamples = computed(() => charSamples.value.filter((sample) => {
  const imageId = sample.source_training_image_id
  const image = imageId == null ? null : imageById.value.get(imageId)
  // StyleSample creation already required touchup_confirmed. The backend only
  // exposes uploaded/cleaning for raw images, so a registered CJK glyph with
  // its owned source image is the true ready unit, not a raw-image status label.
  return Boolean(sample.text_label && sample.text_label.length === 1 && image)
}))
const selectedReadySamples = computed(() => eligibleSamples.value.filter((sample) => selectedSampleIds.value.includes(sample.sample_id)))
const samplePageReadyCount = computed(() => styleSamples.value.filter((sample) => {
  const imageId = sample.source_training_image_id
  return Boolean(sample.text_label && sample.text_label.length === 1 && imageId != null && imageById.value.has(imageId))
}).length)
const readyCharacterCount = computed(() => new Set(styleSamples.value.filter((sample) => {
  return Boolean(sample.text_label && sample.text_label.length === 1 && sample.source_training_image_id != null && imageById.value.has(sample.source_training_image_id))
}).map((sample) => sample.text_label)).size)
const candidateSamples = ref<PersonalStyleSample[]>([])
const ownerConfirmedSamples = ref<PersonalStyleSample[]>([])
const aiConfirmedSamples = ref<PersonalStyleSample[]>([])
const rejectedSampleCount = ref(0)
const selectedCandidateIds = ref<string[]>([])
const reviewBusy = ref(false)
const reviewError = ref<string | null>(null)
const reviewPlan = ref<{ styleId: number; styleName: string; decision: 'confirm' | 'reject'; items: { sample_id: string; text_label: string; source_training_image_id: number | null; final_alpha_sha256: string | null }[] } | null>(null)
const reviewReason = ref('')
const reviewAcknowledged = ref(false)
const reviewDialog = ref<HTMLDialogElement | null>(null)
const reviewOpener = ref<HTMLElement | null>(null)

const candidateSelection = computed(() => candidateSamples.value.filter((sample) => selectedCandidateIds.value.includes(sample.sample_id)))
const ownerConfirmedCharacters = computed(() => new Set(ownerConfirmedSamples.value
  .map((sample) => sample.text_label)
  .filter((label) => isSingleCjk(label))))
const aiConfirmedCharacters = computed(() => new Set(aiConfirmedSamples.value.map((sample) => sample.text_label).filter(isSingleCjk)))
const m2SampleIds = computed(() => genericRecipeSelected.value ? [] : runtime.value?.sample_ids ?? [])
const canCreateRun = computed(() => !humanInputBusy.value && !reviewBusy.value && !reviewPlan.value && !samplesError.value && !loadingSamples.value && !contextRestoring.value && !recoveryError.value && !foreignPendingCreate.value && (engineChoice.value === 'm2'
  ? runtimeReady.value && (!genericRecipeSelected.value || validBudget(maxSeconds.value))
  : Boolean(selectedStyle.value && selectedChar.value && selectedReadySamples.value.length)))
const canAiConfirm = computed(() => Boolean(runtime.value?.capabilities.ai_candidate_confirmation
  && runtime.value.acceptance_policy?.candidate_confirmation === 'AI_AUTHORIZED'))
async function loadRuntime(): Promise<void> {
  const generation = ++runtimeGeneration
  const styleId = selectedStyleId.value
  runtime.value = null
  runtimeError.value = null
  if (styleId == null) { runtimeLoading.value = false; return }
  runtimeLoading.value = true
  try {
    const result = await request<M2Runtime>(runtimePath(styleId, selectedRuntimeRegistration.value, genericRecipeSelected.value ? selectedRecipe.value : undefined), { timeoutMs: selectedRecipe.value === WINDOWS_SCR_RECIPE ? 600000 : 60000, retries: 0 })
    if (generation === runtimeGeneration && !disposed) {
      runtime.value = result; runtimeOptions.value = result.runtime_options ?? []
      const sha = result.config?.snapshot_sha256 ?? result.snapshot?.snapshot_sha256
      if (typeof sha === 'string' && /^[0-9a-f]{64}$/.test(sha)) snapshotSha.value = sha
    }
  } catch (error) {
    if (generation === runtimeGeneration && !disposed) runtimeError.value = errorMessage(error, 'M2 运行环境读取失败')
  } finally {
    if (generation === runtimeGeneration && !disposed) runtimeLoading.value = false
  }
}
function runtimeOptionLabel(option: M2RuntimeOption): string {
  const device = option.device === 'cuda' ? 'GPU / CUDA' : option.device === 'cpu' ? 'CPU' : '设备未知'
  const state = option.ready && option.environment_ready && option.full_model_ready ? '完整模型已验证' : '不可训练'
  return `${device} · ${option.variant_id ?? '注册环境'} · ${state} · ${option.registration_id?.slice(0, 8) ?? '注册错误'}`
}
function chooseRuntime(event: Event): void {
  if (runtimeSelectionLocked.value) return
  const requested = (event.target as HTMLSelectElement).value
  selectedRuntimeRegistration.value = requested
  // A manipulated option must fail closed, including while an earlier request is pending.
  if (requested && !registeredRuntimeOptions.value.some((option) => option.registration_id === requested)) {
    ++runtimeGeneration; runtime.value = null; runtimeLoading.value = false
    runtimeError.value = '所选运行环境未在服务端目录登记；已阻止创建，不会切换到 CPU'
    return
  }
  void loadRuntime()
}

function m2RunConfig(): Record<string, unknown> {
  if (!runtimeReady.value || !runtime.value?.config) {
    throw new Error('M2 运行环境未就绪，请先重新检查')
  }
  if (genericRecipeSelected.value) {
    if (!validBudget(maxSeconds.value)) throw new Error('时间预算必须为大于 0 且不超过 86400 的有限秒数')
    const config = runtime.value.config
    return { engine: 'm2', recipe_id: selectedRecipe.value, style_id: selectedStyleId.value,
      snapshot_sha256: config.snapshot_sha256, preparation_id: config.preparation_id,
      runtime_registration_id: selectedRuntimeRegistration.value, train: { max_seconds: maxSeconds.value } }
  }
  return { ...runtime.value.config }
}
function chooseRecipe(event: Event): void {
  if (runtimeSelectionLocked.value) return
  const value = (event.target as HTMLSelectElement).value
  if (value !== 'm2-control-corrected-v1' && !isUserScrRecipe(value)) return
  selectedRecipe.value = value; saveScrSelection(); void loadRuntime()
}
function saveScrSelection(): void {
  const key = `yy:p7:scr-selection:${auth.me?.user_id ?? ''}:${selectedStyleId.value}`
  try {
    if (genericRecipeSelected.value) localStorage.setItem(key, JSON.stringify({ recipe_id: selectedRecipe.value, runtime_registration_id: selectedRuntimeRegistration.value, max_seconds: maxSeconds.value }))
    else localStorage.removeItem(key)
  } catch { /* A storage failure must not grant permission to create or start. */ }
}
watch([selectedRuntimeRegistration, maxSeconds], saveScrSelection)
async function scrPrepared(_result: ScrPreparation): Promise<void> {
  // Preparation is not launch authorization: rediscover all server gates.
  await loadRuntime()
}

function isSingleCjk(value: string): boolean {
  if ([...value].length !== 1) return false
  const cp = value.codePointAt(0) ?? 0
  return (cp >= 0x3400 && cp <= 0x4dbf) || (cp >= 0x4e00 && cp <= 0x9fff)
    || (cp >= 0xf900 && cp <= 0xfaff) || (cp >= 0x20000 && cp <= 0x2ebef)
}
async function loadStyles(): Promise<void> {
  const generation = contextGeneration
  const requested = route.query.style_id
  loadingStyles.value = true
  stylesError.value = null
  try {
    const d = await request<{ items: StyleProfile[] }>(`${ENDPOINT_STYLES.path}?page=1&page_size=100`)
    if (disposed || generation !== contextGeneration) return
    styles.value = d.items
    if (requested != null) {
      const id = Number(requested)
      if (typeof requested !== 'string' || !Number.isSafeInteger(id) || id < 1) throw new Error('指定的风格档案编号无效')
      if (!d.items.some((style) => style.style_id === id)) {
        const owned = await request<StyleProfile>(`${ENDPOINT_STYLES.path}/${id}`)
        if (disposed || generation !== contextGeneration) return
        styles.value = [...d.items, owned]
      }
      selectedStyleId.value = id
    } else if (selectedStyleId.value == null && d.items.length) selectedStyleId.value = (d.items.find((style) => style.source_type === 'HANDWRITING_ASSET' && style.status === 'approved') ?? d.items[0]!).style_id
  } catch (error) {
    if (!disposed && generation === contextGeneration) { selectedStyleId.value = null; stylesError.value = error instanceof Error ? error.message : '风格列表加载失败' }
  } finally { if (!disposed && generation === contextGeneration) loadingStyles.value = false }
}
function syncStyleRoute(): void {
  if (route.query.style_id != null && selectedStyleId.value != null) {
    void router.replace({ path: '/training', query: { ...route.query, style_id: String(selectedStyleId.value) } })
  }
}
async function loadStyleSamples(): Promise<void> {
  samplesError.value = null
  const generation = ++sampleGeneration
  const styleId = selectedStyleId.value
  styleSamples.value = []
  selectedSampleIds.value = []
  candidateSamples.value = []
  ownerConfirmedSamples.value = []
  aiConfirmedSamples.value = []
  rejectedSampleCount.value = 0
  selectedCandidateIds.value = []
  if (styleId == null) { loadingSamples.value = false; return }
  loadingSamples.value = true
  try {
    const path = fill(ENDPOINT_STYLE_SAMPLES.path, { id: String(styleId) })
    const d = await request<{ items: StyleSample[] }>(path)
    if (generation !== sampleGeneration || disposed) return
    styleSamples.value = d.items
    if (selectedStyle.value?.source_type === 'HANDWRITING_ASSET') {
      const [candidates, confirmed, aiConfirmed, rejected] = await Promise.all([
        loadAllPersonalSamples(styleId, 'candidate'),
        loadAllPersonalSamples(styleId, 'owner_confirmed'),
        loadAllPersonalSamples(styleId, 'ai_confirmed_authorized'),
        loadAllPersonalSamples(styleId, 'rejected'),
      ])
      if (generation !== sampleGeneration || disposed) return
      candidateSamples.value = candidates
      ownerConfirmedSamples.value = confirmed
      aiConfirmedSamples.value = aiConfirmed
      rejectedSampleCount.value = rejected.length
    }
    const chars = [...new Set(d.items.map((sample) => sample.text_label).filter((label): label is string => Boolean(label && label.length === 1)))].sort()
    if (!chars.includes(selectedChar.value)) selectedChar.value = chars[0] ?? ''
    selectedSampleIds.value = d.items.filter((sample) => sample.text_label === selectedChar.value && sample.source_training_image_id != null && imageById.value.has(sample.source_training_image_id)).map((sample) => sample.sample_id)
  } catch (error) { if (generation === sampleGeneration && !disposed) samplesError.value = errorMessage(error, '风格样本读取失败') }
  finally { if (generation === sampleGeneration) loadingSamples.value = false }
}
async function loadAllPersonalSamples(styleId: number, status: PersonalStyleSample['review_status']): Promise<PersonalStyleSample[]> {
  const pageSize = 100
  const first = await request<{ items: PersonalStyleSample[]; total: number }>(
    `${ENDPOINT_TRAINING_SAMPLES.path}?style_id=${styleId}&page=1&page_size=${pageSize}&review_status=${status}`,
  )
  const pageCount = Math.ceil(first.total / pageSize)
  if (pageCount <= 1) return first.items
  const later = await Promise.all(Array.from({ length: pageCount - 1 }, (_, index) => index + 2).map((page) =>
    request<{ items: PersonalStyleSample[] }>(
      `${ENDPOINT_TRAINING_SAMPLES.path}?style_id=${styleId}&page=${page}&page_size=${pageSize}&review_status=${status}`,
    ),
  ))
  return [...first.items, ...later.flatMap((response) => response.items)]
}
function openCandidateReview(decision: 'confirm' | 'reject', event: Event): void {
  if (!candidateHumanMode.value || disposed || contextRestoring.value || !selectedStyle.value || loadingSamples.value || samplesError.value || reviewBusy.value || !candidateSelection.value.length || candidateSelection.value.length > 100) return
  if (decision === 'confirm' && (selectedStyle.value.status !== 'approved' || candidateSelection.value.some((sample) => !sample.provenance_complete || !isSingleCjk(sample.text_label)))) {
    showError(new Error('所选候选缺少完整来源链或单字标签，请修正后再确认'), '候选不可确认')
    return
  }
  reviewOpener.value = event.currentTarget as HTMLElement
  reviewPlan.value = { styleId: selectedStyle.value.style_id, styleName: selectedStyle.value.name, decision, items: candidateSelection.value.map(sample => ({ sample_id: sample.sample_id, text_label: sample.text_label, source_training_image_id: sample.source_training_image_id, final_alpha_sha256: sample.final_alpha_sha256 })) }
  reviewReason.value = ''
  reviewAcknowledged.value = false
  reviewError.value = null
  void nextTick(() => reviewDialog.value?.showModal())
}
function closeCandidateReview(): void {
  if (reviewBusy.value || !reviewPlan.value) return
  reviewDialog.value?.close?.()
  reviewPlan.value = null
  reviewOpener.value?.focus()
}
function selectCandidateBatch(): void {
  if (loadingSamples.value || reviewBusy.value || reviewPlan.value) return
  selectedCandidateIds.value = candidateSamples.value.slice(0, 100).map(sample => sample.sample_id)
}
async function submitCandidateReview(): Promise<void> {
  const plan = reviewPlan.value
  if (!plan || plan.styleId !== selectedStyleId.value || reviewBusy.value || !reviewAcknowledged.value) return
  if (plan.decision === 'reject' && !reviewReason.value.trim()) return
  reviewBusy.value = true
  errorText.value = null
  try {
    const result = await request<{ confirmed: number; rejected: number }>(ENDPOINT_TRAINING_SAMPLES_BATCH_REVIEW.path, {
      method: 'POST',
      body: {
        style_id: plan.styleId,
        items: plan.items.map((sample) => ({
          sample_id: sample.sample_id,
          decision: plan.decision,
          ...(plan.decision === 'confirm' ? { text_label: sample.text_label } : {}),
          ...(plan.decision === 'reject' ? { reason: reviewReason.value.trim() } : {}),
        })),
      },
    })
    reviewDialog.value?.close?.()
    reviewPlan.value = null
    selectedCandidateIds.value = []
    await Promise.all([loadStyleSamples(), loadRuntime()])
    flash(`本人复核完成：确认 ${result.confirmed}，排除 ${result.rejected}；每条样本均已单独留审计记录`)
  } catch (error) {
    reviewError.value = error instanceof ApiRequestError ? userError(error) : '提交结果未确认，请重新读取候选核对服务端状态。'
  }
  finally { reviewBusy.value = false }
}
async function submitAiCandidateConfirmation(): Promise<void> {
  if (!selectedStyleId.value || !canAiConfirm.value || reviewBusy.value || !candidateSelection.value.length || candidateSelection.value.length > 100) return
  reviewBusy.value = true
  errorText.value = null
  try {
    const result = await request<CandidateAiConfirmation>(ENDPOINT_TRAINING_SAMPLES_AI_CONFIRM.path, {
      method: 'POST', body: { style_id: selectedStyleId.value, sample_ids: [...selectedCandidateIds.value] }, timeoutMs: 60000,
    })
    await Promise.all([loadStyleSamples(), loadRuntime()])
    flash(`AI 授权确认 ${result.confirmed} 条；${result.isolated} 条保留待复核。未记为本人确认。`)
  } catch (error) { showError(error, 'AI 候选确认失败') }
  finally { reviewBusy.value = false }
}
function snapshotBusy(busy: boolean): void {
  if (busy) busyAction.value = 'snapshot'
  else if (busyAction.value === 'snapshot') busyAction.value = null
}
async function snapshotPrepared(result: SnapshotPreparation): Promise<void> {
  const styleId = selectedStyleId.value; const generation = contextGeneration
  if (result.status === 'READY' && result.target_style_id === styleId && result.snapshot_ready && /^[0-9a-f]{64}$/.test(result.dataset_snapshot_sha256 ?? '')) snapshotSha.value = result.dataset_snapshot_sha256!
  await loadRuntime()
  if (disposed || generation !== contextGeneration || styleId !== selectedStyleId.value) return
  if (result.status === 'READY' && result.snapshot_ready) flash('S03 已验证；当前运行环境状态已刷新')
}
const snapshotRefreshToken = computed(() => JSON.stringify([...ownerConfirmedSamples.value, ...aiConfirmedSamples.value]
  .map((sample) => [sample.sample_id, sample.final_alpha_sha256, sample.review_status]).sort()) + `:${humanInputRevision.value}`)
watch(selectedStyleId, (styleId) => {
  if (!reviewBusy.value) closeCandidateReview()
  runtimeOptions.value = []
  snapshotSha.value = ''
  if (run.value && Number(run.value.config.style_id) !== styleId) clearRunView()
  const boundConfig = pendingCreate.value && Number(pendingCreate.value.body.config.style_id) === styleId
    ? pendingCreate.value.body.config : run.value?.config
  selectedRuntimeRegistration.value = boundConfig?.engine === 'm2' ? registrationForConfig(boundConfig) : ''
  if (boundConfig?.engine === 'm2' && typeof boundConfig.recipe_id === 'string') selectedRecipe.value = boundConfig.recipe_id
  else {
    selectedRecipe.value = 'm2-control-corrected-v1'
    try {
      const saved = JSON.parse(localStorage.getItem(`yy:p7:scr-selection:${auth.me?.user_id ?? ''}:${styleId}`) ?? 'null')
      if (isUserScrRecipe(saved?.recipe_id)) {
        selectedRecipe.value = saved.recipe_id
        selectedRuntimeRegistration.value = typeof saved.runtime_registration_id === 'string' ? saved.runtime_registration_id : 'invalid-runtime-binding'
        maxSeconds.value = validBudget(saved.max_seconds) ? saved.max_seconds : 3600
      }
    } catch { /* Historical malformed settings cannot enable any run. */ }
  }
  void loadStyleSamples(); void loadRuntime()
}, { flush: 'sync' })
watch(candidateHumanMode, (manual) => { if (!manual) void loadStyleSamples() })
watch(selectedChar, () => {
  selectedSampleIds.value = eligibleSamples.value.map((sample) => sample.sample_id)
})
watch(imageById, () => { selectedSampleIds.value = selectedSampleIds.value.filter(id => eligibleSamples.value.some(sample => sample.sample_id === id)) })

// Reload restores only the run pointer and one exact idempotent pending create request; progress always comes from the server.
const run = ref<StyleRun | null>(null)
const draftMaxSeconds = ref<number>(3600)
const genericDraft = computed(() => run.value?.status === 'created' && isUserScrRecipe(run.value.config.recipe_id))
const draftBudgetUnchanged = computed(() => !genericDraft.value || draftMaxSeconds.value === (run.value?.config.train as Record<string, unknown> | undefined)?.max_seconds)
const progress = ref<RunProgress | null>(null)
const metrics = ref<RunMetricsResponse | null>(null)
const evaluation = ref<EvaluateResponse | null>(null)
const published = ref<PublishedStyleProfile | null>(null)
const runSamples = ref<StyleSample[]>([])
const runLoading = ref(false)
const preservedDraftId = ref<number | null>(null)
const seed = ref(20260928)
const lastRunKey = 'yy:p7:last-style-run-id'
const pendingCreateKey = 'yy:p7:pending-style-run-create'
interface PendingCreate { key: string; body: { sample_ids: number[]; config: Record<string, unknown> } }
const pendingCreate = ref<PendingCreate | null>(null)
const existingRunNumber = ref('')
const existingRunError = ref('')
function positiveRunId(value: unknown): number | null {
  if (typeof value !== 'string' || !/^[1-9][0-9]*$/.test(value.trim())) return null
  const id = Number(value.trim())
  return Number.isSafeInteger(id) && id > 0 ? id : null
}
async function recoverExistingRun(value: unknown = existingRunNumber.value): Promise<void> {
  if (pendingCreate.value || foreignPendingCreate.value || busyAction.value || runLoading.value || (run.value && !isTerminal.value)) return
  const id = positiveRunId(value)
  clearRunView(); recoveryError.value = null; existingRunError.value = ''; localStorage.removeItem(lastRunKey)
  if (id === null) { existingRunError.value = '运行编号必须是正整数'; return }
  existingRunNumber.value = String(id)
  await loadRun(id, { recoverStyle: true, explicitRecovery: true })
}
const terminalStatuses: StyleRunStatus[] = ['succeeded', 'failed', 'cancelled']
const isTerminal = computed(() => Boolean(run.value && terminalStatuses.includes(run.value.status)))
const runLabel: Record<StyleRunStatus, string> = { created: '已创建', queued: '排队中', running: '运行中', succeeded: '已完成', failed: '失败', cancelled: '已取消' }
const runIsM2 = computed(() => run.value?.config.engine === 'm2')
const readinessFresh = computed(() => !paneErrors.value['发布门'] && !paneErrors.value['运行'] && !paneErrors.value['推理部署'])
const runtimeSelectionLocked = computed(() => Boolean(contextRestoring.value || recoveryError.value || (run.value && !isTerminal.value) || pendingCreate.value || busyAction.value || runLoading.value || reviewBusy.value))
const canStartRun = computed(() => !contextRestoring.value && draftBudgetUnchanged.value && run.value?.status === 'created' && (!runIsM2.value || Boolean(runtimeReady.value && !recipeProblem(run.value.config) && matchesRunRuntime(run.value.config, runtime.value, selectedRuntimeRegistration.value))))
const runRuntimeBinding = computed(() => runtimeBinding(run.value?.config))
const inferenceDeployment = ref<InferenceDeploymentState | null>(null)
const inferenceRegistration = ref('')
const deploymentPending = ref(false)
const deploymentUnconfirmed = ref(false)
const deploymentSupported = computed(() => runIsM2.value && run.value?.status === 'succeeded' && inferenceDeployment.value?.run_id === run.value.run_id && inferenceDeployment.value?.supports_preserved_training === true)
const deploymentRequired = computed(() => deploymentSupported.value && inferenceDeployment.value?.state !== 'registered')
const deploymentPendingKey = computed(() => `yy:inference-deployment:${auth.me?.user_id ?? 'anonymous'}:${run.value?.run_id ?? ''}`)
function acceptDeployment(value: InferenceDeploymentState): void {
  inferenceDeployment.value = value
  deploymentUnconfirmed.value = Boolean(localStorage.getItem(deploymentPendingKey.value))
  if (value.state === 'registered') {
    localStorage.removeItem(deploymentPendingKey.value); deploymentUnconfirmed.value = false
  }
  if (!value.available?.some(option => option.registration_id === inferenceRegistration.value && option.ready)) inferenceRegistration.value = ''
}
async function readInferenceDeployment(): Promise<void> {
  if (!run.value || !runIsM2.value || deploymentPending.value) return
  await readRunPane<InferenceDeploymentState>('推理部署', `/api/v1/style-runs/${run.value.run_id}/inference-deployment`, acceptDeployment, runGeneration)
}
async function registerInferenceDeployment(): Promise<void> {
  if (!run.value || deploymentPending.value || busyAction.value || deploymentUnconfirmed.value || !deploymentRequired.value
    || !inferenceDeployment.value?.available.some(option => option.ready && option.registration_id === inferenceRegistration.value)) return
  const runId = run.value.run_id; const context = contextGeneration
  try { localStorage.setItem(deploymentPendingKey.value, inferenceRegistration.value) } catch { paneErrors.value['推理部署'] = '无法保留部署请求身份，尚未提交'; return }
  deploymentPending.value = true; busyAction.value = 'inference-deployment'
  try {
    await request(`/api/v1/style-runs/${runId}/inference-deployment`, { method: 'POST', body: { runtime_registration_id: inferenceRegistration.value }, retries: 0,
      timeoutMs: run.value.config.recipe_id === WINDOWS_SCR_RECIPE ? 600000 : 15000 })
  } catch {
    if (ownsRunContext(runId,context)) deploymentUnconfirmed.value = true
  } finally {
    deploymentPending.value = false; busyAction.value = null
    if (ownsRunContext(runId,context)) await readInferenceDeployment()
  }
}
const canPublish = computed(() => Boolean(readinessFresh.value && run.value?.status === 'succeeded'
  && (runIsM2.value ? readiness.value?.allowed_actions?.publish : humanReviewRecorded.value)))
const humanReviewRecorded = ref(false)
function stopPolling(): void { if (pollTimer !== null) window.clearTimeout(pollTimer); pollTimer = null }
function shouldPoll(): boolean {
  return Boolean(run.value && (!terminalStatuses.includes(run.value.status)
    || readiness.value?.evaluation_state === 'running' || readiness.value?.ai_review_state === 'running' || workflowPending.value))
}
function startPolling(): void {
  stopPolling()
  if (!disposed && shouldPoll()) pollTimer = window.setTimeout(() => { void refreshRun() }, 2200)
}
async function readRunPane<T>(key: string, path: string, assign: (value: T) => void, generation: number): Promise<void> {
  try {
    const result = await request<T>(path, {
      retries: 0,
      timeoutMs: ['发布门', '推理部署'].includes(key) && run.value?.config.recipe_id === WINDOWS_SCR_RECIPE ? 600000 : 15000,
    })
    if (disposed || generation !== runGeneration) return
    assign(result)
    delete paneErrors.value[key]
  } catch (error) {
    if (!disposed && generation === runGeneration) paneErrors.value[key] = errorMessage(error, `${key} 读取失败`)
  }
}
async function readRunSamples(id: number, generation: number): Promise<void> {
  const path = fill(ENDPOINT_STYLE_RUN_SAMPLES.path, { id: String(id) })
  try {
    const first = await request<ApiEnvelope<{ items: StyleSample[] }>>(`${path}?page=1&page_size=100`, { preserveEnvelope: true, retries: 0 })
    const total = Number(first.meta?.total ?? first.data.items.length)
    const rest = await Promise.all(Array.from({ length: Math.max(0, Math.ceil(total / 100) - 1) }, (_, index) =>
      request<{ items: StyleSample[] }>(`${path}?page=${index + 2}&page_size=100`, { retries: 0 })))
    if (generation !== runGeneration || disposed) return
    runSamples.value = [...first.data.items, ...rest.flatMap((page) => page.items)]
    delete paneErrors.value['样本']
  } catch (error) {
    if (generation === runGeneration && !disposed) paneErrors.value['样本'] = errorMessage(error, '样本读取失败')
  }
}
function clearRunView(): void {
  ++runGeneration; stopPolling(); runLoading.value = false
  run.value = null; progress.value = null; metrics.value = null; evaluation.value = null; readiness.value = null; published.value = null; inferenceDeployment.value = null; inferenceRegistration.value = ''; deploymentUnconfirmed.value = false
  runSamples.value = []; blindScores.value = {}; blindVerdict.value = ''; humanReviewRecorded.value = false; paneErrors.value = {}; workflowPending.value = false
}

async function loadRun(id: number, options: { recoverStyle?: boolean; explicitRecovery?: boolean } = {}): Promise<void> {
  if (runLoading.value) return
  const generation = ++runGeneration
  runLoading.value = true
  stopPolling()
  try {
    const current = await request<StyleRun>(fill(ENDPOINT_STYLE_RUN_DETAIL.path, { id: String(id) }), { retries: 0 })
    if (generation !== runGeneration || disposed) return
    if (current.run_id !== id || !current.config || !['created','queued','running','succeeded','failed','cancelled'].includes(current.status) || !Number.isSafeInteger(Number(current.config.style_id)) || Number(current.config.style_id) < 1) throw new Error('服务器运行身份无效，未恢复任何训练')
    if (route.query.style_id != null && Number(current.config.style_id) !== selectedStyleId.value) {
      if (options.explicitRecovery) { existingRunError.value = '运行与指定档案不匹配，未恢复任何训练'; return }
      styleContextNotice.value ||= '已保留其他档案的上次运行，本页仅显示指定档案。可打开训练控制台恢复上次运行。'
      return
    }
    if (options.recoverStyle) recoveryError.value = null
    const changed = run.value?.run_id !== current.run_id
    if (changed) {
      inferenceDeployment.value = null; inferenceRegistration.value = ''; deploymentUnconfirmed.value = false
      progress.value = null; metrics.value = null; evaluation.value = null; readiness.value = null; published.value = null
      runSamples.value = []; blindScores.value = {}; blindVerdict.value = ''; humanReviewRecorded.value = false; paneErrors.value = {}
    }
    run.value = current
    existingRunError.value = ''; existingRunNumber.value = String(id)
    const seconds = (current.config.train as Record<string, unknown> | undefined)?.max_seconds
    if (typeof seconds === 'number') draftMaxSeconds.value = seconds
    localStorage.setItem(lastRunKey, String(id))
    const configStyleId = Number(current.config.style_id)
    if (options.recoverStyle && Number.isInteger(configStyleId) && configStyleId > 0) selectedStyleId.value = configStyleId
    if (options.recoverStyle) engineChoice.value = current.config.engine === 'm2' ? 'm2' : 'm1'
    const runtimeReads: Promise<void>[] = []
    if ((changed || options.recoverStyle) && current.config.engine === 'm2') {
      const config = pendingCreate.value?.body.config ?? current.config
      if (typeof config.recipe_id === 'string') selectedRecipe.value = config.recipe_id
      selectedRuntimeRegistration.value = registrationForConfig(config)
      // Completed runs use owned readiness/deployment gates, not a new-training preflight.
      if (terminalStatuses.includes(current.status)) void loadRuntime()
      else runtimeReads.push(loadRuntime())
    }
    const params = { id: String(id) }
    const reads: Promise<void>[] = [
      ...runtimeReads,
      readRunPane<RunProgress>('进度', fill(ENDPOINT_STYLE_RUN_PROGRESS.path, params), (value) => { progress.value = value }, generation),
      readRunPane<RunMetricsResponse>('指标', fill(ENDPOINT_STYLE_RUN_METRICS.path, params), (value) => { metrics.value = value }, generation),
    ]
    if (current.config.engine === 'm2' && current.status === 'succeeded') reads.push(readRunPane<InferenceDeploymentState>('推理部署', `/api/v1/style-runs/${id}/inference-deployment`, acceptDeployment, generation))
    if (changed || !runSamples.value.length) reads.push(readRunSamples(id, generation))
    if (current.config.engine === 'm2') reads.push(readRunPane<RunReadiness>('发布门', fill(ENDPOINT_STYLE_RUN_READINESS.path, params), (value) => {
      readiness.value = value
      published.value = value.publication
      if (value.evaluation_state !== 'running' && value.ai_review_state !== 'running') workflowPending.value = false
    }, generation))
    else readiness.value = null
    if (terminalStatuses.includes(current.status)) reads.push(readRunPane<EvaluateResponse>('评估', fill(ENDPOINT_STYLE_RUN_EVALUATE.path, params), (value) => { evaluation.value = value }, generation))
    await Promise.all(reads)
    delete paneErrors.value['运行']
  } catch (error) {
    if (generation !== runGeneration || disposed) return
    if (options.explicitRecovery) { run.value = null; progress.value = null; readiness.value = null; published.value = null; inferenceDeployment.value = null; existingRunError.value = errorMessage(error, '已有训练读取失败'); localStorage.removeItem(lastRunKey) }
    if (error instanceof ApiRequestError && error.code === 'RUN_NOT_FOUND') {
      localStorage.removeItem(lastRunKey); run.value = null; readiness.value = null; published.value = null
    }
    if (options.recoverStyle && !(error instanceof ApiRequestError && error.code === 'RUN_NOT_FOUND')) recoveryError.value = errorMessage(error, '上次运行恢复失败')
    if (!disposed) paneErrors.value['运行'] = errorMessage(error, '训练运行读取失败')
    showError(error, '训练运行读取失败')
  } finally {
    if (generation === runGeneration && !disposed) { runLoading.value = false; startPolling() }
  }
}
async function refreshRun(): Promise<void> {
  if (!run.value || runLoading.value) { startPolling(); return }
  await loadRun(run.value.run_id)
}
async function leaveDraft(): Promise<void> {
  if (run.value?.status !== 'created' || busyAction.value || runLoading.value || contextRestoring.value || pendingCreate.value) return
  const id = run.value.run_id
  const context = contextGeneration
  const styleId = selectedStyleId.value
  preservedDraftId.value = id
  if (localStorage.getItem(lastRunKey) === String(id)) localStorage.removeItem(lastRunKey)
  clearRunView()
  // Keep the chosen registration, including a missing GPU registration. The user
  // must choose any replacement explicitly; this action never rewrites the old run.
  await loadRuntime()
  if (!ownsStyleContext(styleId, context)) return
  flash(`草稿 #${id} 已保留且未修改；请选择新运行环境并创建新运行`)
}

function ownsRunContext(id: number, generation: number): boolean {
  return !disposed && generation === contextGeneration && run.value?.run_id === id
    && Number(run.value.config.style_id) === selectedStyleId.value
}
function ownsStyleContext(id: number | null, generation: number): boolean {
  return !disposed && generation === contextGeneration && selectedStyleId.value === id
}

function clearPendingRequest(key: string): void {
  let cleared = false
  try {
    const saved = JSON.parse(localStorage.getItem(pendingCreateKey) ?? 'null')
    if (saved?.key === key) { localStorage.removeItem(pendingCreateKey); cleared = true }
  } catch { /* Preserve an unknown request; only the matching operation may clear it. */ }
  if (pendingCreate.value?.key === key) pendingCreate.value = null
  if (cleared && foreignPendingCreate.value) {
    foreignPendingCreate.value = false
    styleContextNotice.value = '其他档案的创建请求已返回；本页仍显示指定档案。可打开训练控制台查看已保存的运行。'
  }
}

async function createRun(): Promise<void> {
  if (contextRestoring.value || busyAction.value || runLoading.value) return
  if (!pendingCreate.value && (!canCreateRun.value || !selectedStyle.value || (run.value && !isTerminal.value))) return
  const context = contextGeneration
  const styleId = selectedStyleId.value
  let submitted: PendingCreate | null = null
  busyAction.value = 'create'
  errorText.value = null
  try {
    if (!pendingCreate.value) {
      pendingCreate.value = { key: newIdempotencyKey(), body: {
        sample_ids: engineChoice.value === 'm2' ? [...m2SampleIds.value] : selectedReadySamples.value.map((sample) => sample.sample_id),
        config: engineChoice.value === 'm2' ? m2RunConfig() : { engine: 'm1', target_char: selectedChar.value, seed: seed.value, style_id: selectedStyle.value!.style_id },
      } }
      localStorage.setItem(pendingCreateKey, JSON.stringify(pendingCreate.value))
    }
    submitted = pendingCreate.value
    const created = await request<StyleRun>(ENDPOINT_STYLE_RUNS.path, {
      method: 'POST', body: submitted.body, idempotencyKey: submitted.key,
      timeoutMs: submitted.body.config.recipe_id === WINDOWS_SCR_RECIPE ? 600000 : 15000, retries: 0,
    })
    localStorage.setItem(lastRunKey, String(created.run_id))
    clearPendingRequest(submitted.key)
    if (!ownsStyleContext(styleId, context)) return
    await loadRun(created.run_id, { recoverStyle: true })
    if (!ownsStyleContext(styleId, context)) return
    flash(`运行 #${created.run_id} 已创建，请启动`)
  } catch (error) {
    // A transport/server error may hide a committed run. Preserve this exact
    // request across reload and retry under the same server idempotency key.
    if (error instanceof ApiRequestError && error.status >= 400 && error.status < 500) {
      if (submitted) clearPendingRequest(submitted.key)
    }
    if (ownsStyleContext(styleId, context)) showError(error, '训练运行创建失败')
  } finally { busyAction.value = null }
}
async function mutateRun(action: 'start' | 'cancel'): Promise<void> {
  if (!run.value || busyAction.value || runLoading.value) return
  if (action === 'start' && !canStartRun.value) return
  if (action === 'cancel' && !['queued', 'running'].includes(run.value.status)) return
  const context = contextGeneration
  const runId = run.value.run_id
  busyAction.value = action
  errorText.value = null
  try {
    const path = fill((action === 'start' ? ENDPOINT_STYLE_RUN_START : ENDPOINT_STYLE_RUN_CANCEL).path, { id: String(runId) })
    const updated = await request<StyleRun>(path, { method: 'POST', timeoutMs: run.value.config.recipe_id === WINDOWS_SCR_RECIPE ? 600000 : 15000, retries: 0 })
    if (!ownsRunContext(runId, context)) return
    run.value = updated
    await loadRun(runId)
  } catch (error) { if (ownsRunContext(runId, context)) { showError(error, action === 'start' ? '启动训练失败' : '取消训练失败'); await refreshRun() } }
  finally { busyAction.value = null }
}
async function saveRunBudget(): Promise<void> {
  if (!genericDraft.value || !run.value || !validBudget(draftMaxSeconds.value) || busyAction.value || runLoading.value || contextRestoring.value) return
  const id = run.value.run_id; const context = contextGeneration; const config = run.value.config; const requestedSeconds = draftMaxSeconds.value
  busyAction.value = 'budget'; errorText.value = null
  try {
    await request<StyleRun>(fill(ENDPOINT_STYLE_RUN_DETAIL.path, { id: String(id) }), { method: 'PUT', retries: 0,
      body: { config: { engine: 'm2', recipe_id: config.recipe_id, style_id: config.style_id, snapshot_sha256: config.snapshot_sha256,
        preparation_id: config.preparation_id, runtime_registration_id: config.runtime_registration_id, train: { max_seconds: requestedSeconds } } } })
    if (!ownsRunContext(id, context)) return
    await loadRun(id)
    if (!ownsRunContext(id, context)) return
    const current = run.value?.config
    if ((current?.train as Record<string, unknown> | undefined)?.max_seconds === requestedSeconds
      && ['recipe_id', 'style_id', 'snapshot_sha256', 'preparation_id', 'runtime_registration_id'].every(key => current?.[key] === config[key])) flash('已重新读取服务端草稿预算；启动仍需单独操作')
    else errorText.value ??= '服务端尚未确认所请求的草稿预算，请刷新运行核对；不会自动启动'
  } catch (error) { if (ownsRunContext(id, context)) { showError(error, '草稿预算保存结果未确认，请刷新运行核对'); await refreshRun() } }
  finally { busyAction.value = null }
}
async function loadEvaluation(): Promise<void> {
  if (!run.value || runLoading.value) return
  await loadRun(run.value.run_id)
}
const workflowPending = ref(false)
async function runWorkflow(action: 'evaluate' | 'ai-review'): Promise<void> {
  if (!run.value || busyAction.value || runLoading.value || !readinessFresh.value) return
  if (action === 'evaluate' && deploymentRequired.value) return
  if (action === 'evaluate' ? !readiness.value?.allowed_actions?.evaluate : !readiness.value?.allowed_actions?.ai_review) return
  const context = contextGeneration
  const runId = run.value.run_id
  busyAction.value = action
  errorText.value = null
  try {
    const endpoint = action === 'evaluate' ? ENDPOINT_STYLE_RUN_EVALUATION_START : ENDPOINT_STYLE_RUN_AI_REVIEW
    const result = await request<M2WorkflowJob>(fill(endpoint.path, { id: String(runId) }), { method: 'POST', body: {}, timeoutMs: run.value?.config.recipe_id === WINDOWS_SCR_RECIPE ? 600000 : 60000, retries: 0 })
    if (!ownsRunContext(runId, context)) return
    workflowPending.value = ['running', 'queued', 'pending'].includes(result.state)
    flash(`${action === 'evaluate' ? '真实评估' : 'AI 视觉盲评'}：${result.state}`)
    await loadRun(run.value.run_id)
  } catch (error) { if (ownsRunContext(runId, context)) { showError(error, action === 'evaluate' ? '评估启动失败' : 'AI 视觉复核失败'); await refreshRun() } }
  finally { busyAction.value = null; startPolling() }
}
const blindVerdict = ref<'' | 'pass' | 'fail'>('')
const blindScores = ref<Record<number, BlindScore>>({})
const canSubmitBlindReview = computed(() => runSamples.value.length > 0 && blindVerdict.value !== '' && runSamples.value.every((sample) => blindScores.value[sample.sample_id] != null))
async function submitBlindReview(): Promise<void> {
  if (!run.value || !canSubmitBlindReview.value || busyAction.value) return
  const context = contextGeneration
  const runId = run.value.run_id
  busyAction.value = 'blind-review'
  errorText.value = null
  try {
    const submission: BlindReviewSubmission = {
      verdict: blindVerdict.value as 'pass' | 'fail',
      items: runSamples.value.map((sample) => ({ sample_key: String(sample.sample_id), score: blindScores.value[sample.sample_id] as 0 | 1 | 2 | 3 })),
    }
    const result = await request<{ review_id: string }>(fill(ENDPOINT_STYLE_RUN_BLIND_REVIEW.path, { id: String(run.value.run_id) }), { method: 'POST', body: submission })
    if (!ownsRunContext(runId, context)) return
    humanReviewRecorded.value = blindVerdict.value === 'pass'
    await refreshRun()
    flash(`真人盲评已记录：${result.review_id}`)
  } catch (error) { if (ownsRunContext(runId, context)) showError(error, '真人盲评提交失败') }
  finally { busyAction.value = null }
}
async function publishRun(): Promise<void> {
  if (!run.value || !canPublish.value || busyAction.value || runLoading.value) return
  const context = contextGeneration
  const runId = run.value.run_id
  busyAction.value = 'publish'
  errorText.value = null
  try {
    const result = await request<PublishedStyleProfile>(fill(ENDPOINT_STYLE_RUN_PUBLISH.path, { id: String(runId) }), { method: 'POST' })
    if (!ownsRunContext(runId, context)) return
    published.value = result
    await Promise.all([loadStyles(), refreshRun()])
    if (!ownsRunContext(runId, context)) return
    flash(`发布申请已创建；风格状态：${stateLabel(published.value.status)}，质量门：${stateLabel(published.value.quality_status ?? '待读取')}`)
  } catch (error) {
    if (!ownsRunContext(runId, context)) return
    showError(error, '发布申请失败')
    if (error instanceof ApiRequestError && error.code === 'RUN_BLIND_REVIEW_REQUIRED') errorText.value = '服务端验收政策尚未满足，请刷新发布门。'
    await refreshRun()
  } finally { busyAction.value = null }
}
function formatProgress(value: number | null | undefined, digits = 3): string { return value == null || !Number.isFinite(value) ? '暂无真实数据' : Number(value).toFixed(digits) }
function etaLabel(seconds: number | null): string { return seconds == null || !Number.isFinite(seconds) ? '暂无可靠预计时间' : `${Math.max(0, Math.round(seconds))} 秒` }
const runStatusClass: Record<StyleRunStatus, string> = { created: 'bg-canvas text-ink-muted', queued: 'bg-amber/30 text-amber', running: 'bg-sky/60 text-primary', succeeded: 'bg-mint text-emerald-800', failed: 'bg-blush text-danger', cancelled: 'bg-canvas text-ink-muted' }

async function restorePageContext(): Promise<void> {
  const generation = ++contextGeneration
  contextRestoring.value = true
  recoveryError.value = null
  try {
  ++runtimeGeneration
  runtime.value = null; runtimeOptions.value = []; runtimeError.value = null; runtimeLoading.value = false; selectedRuntimeRegistration.value = ''
  ++runGeneration
  stopPolling()
  runLoading.value = false
  run.value = null; progress.value = null; metrics.value = null; evaluation.value = null
  readiness.value = null; published.value = null; runSamples.value = []; paneErrors.value = {}
  selectedStyleId.value = null; preservedDraftId.value = null; pendingCreate.value = null; foreignPendingCreate.value = false
  styleContextNotice.value = ''; errorText.value = null; existingRunError.value = ''; existingRunNumber.value = ''
  const requested = route.query.style_id
  try {
    const saved: unknown = JSON.parse(localStorage.getItem(pendingCreateKey) ?? 'null')
    if (saved && typeof saved === 'object' && 'key' in saved && typeof saved.key === 'string' && 'body' in saved) {
      const body = saved.body
      if (body && typeof body === 'object' && 'sample_ids' in body && Array.isArray(body.sample_ids) && 'config' in body && body.config && typeof body.config === 'object') {
        if (requested == null || Number((body.config as Record<string, unknown>).style_id) === Number(requested)) pendingCreate.value = saved as PendingCreate
        else { foreignPendingCreate.value = true; styleContextNotice.value = '另一个档案的创建结果尚未确认，请先打开训练控制台恢复，避免覆盖未确认的请求。' }
      }
    }
  } catch { localStorage.removeItem(pendingCreateKey) }
  await Promise.all([loadImages(1), loadStyles()])
  if (disposed || generation !== contextGeneration || (requested != null && selectedStyleId.value == null)) return
  if (pendingCreate.value) {
    const styleId = Number(pendingCreate.value.body.config.style_id)
    if (Number.isSafeInteger(styleId) && styleId > 0) {
      if (!styles.value.some((style) => style.style_id === styleId)) {
        try {
          const owned = await request<StyleProfile>(`${ENDPOINT_STYLES.path}/${styleId}`, { retries: 0 })
          if (disposed || generation !== contextGeneration) return
          styles.value = [...styles.value, owned]
        } catch (error) { if (generation === contextGeneration) showError(error, '未确认请求的风格档案不可用'); return }
      }
      selectedStyleId.value = styleId
      engineChoice.value = pendingCreate.value.body.config.engine === 'm2' ? 'm2' : 'm1'
      selectedRuntimeRegistration.value = engineChoice.value === 'm2' ? registrationForConfig(pendingCreate.value.body.config) : ''
      if (engineChoice.value === 'm2' && typeof pendingCreate.value.body.config.recipe_id === 'string') selectedRecipe.value = pendingCreate.value.body.config.recipe_id
      await loadRuntime()
      if (disposed || generation !== contextGeneration) return
    }
  }
  const savedRunId = Number(localStorage.getItem(lastRunKey))
  if (!pendingCreate.value && !foreignPendingCreate.value && route.query.run_id != null) await recoverExistingRun(route.query.run_id)
  else if (!pendingCreate.value && !foreignPendingCreate.value && Number.isSafeInteger(savedRunId) && savedRunId > 0) await loadRun(savedRunId, { recoverStyle: true })
  } finally { if (!disposed && generation === contextGeneration) contextRestoring.value = false }
}
// Step navigation changes presentation only: all request/recovery gates remain mounted.
const activeStep = ref(1)
const componentProblems = ref<Record<string, string>>({})
watch(selectedStyleId, () => { componentProblems.value = {} })
const stepLabels = ['提取字形', '确认字样', '开始训练', '查看效果并使用']
watch(() => [run.value?.run_id, run.value?.status], () => {
  if (run.value) activeStep.value = run.value.status === 'succeeded' ? 4 : 3
})
const trainingBlockers = computed(() => {
  const inputRelevant = run.value?.status !== 'succeeded'
  const inputProblems = new Set(['字样输入审核', '训练材料准备', '个人训练准备', '训练恢复'])
  const reasons = [recoveryError.value, ...(inputRelevant ? [samplesError.value, reviewError.value] : []), ...Object.values(paneErrors.value),
    ...Object.entries(componentProblems.value).filter(([key]) => inputRelevant || !inputProblems.has(key)).map(([,value]) => value)]
  const failure = progress.value?.failure
  if (failure) reasons.push([failure.code, failure.message].filter(Boolean).join('：'))
  if (inputRelevant && engineChoice.value === 'm2' && selectedStyleId.value) {
    reasons.push(runtimeError.value, runtimeSelectionProblem.value)
    for (const blocker of runtime.value?.blockers ?? []) reasons.push(`${blocker.code}：${blocker.detail}`)
    if (runtime.value && !runtimeReady.value && !runtimeLoading.value && !reasons.some(Boolean)) reasons.push('训练环境尚未通过检查，请重新检查环境；准备好之前不能开始训练。')
  }
  return [...new Set(reasons.filter((reason): reason is string => Boolean(reason)).map(userText))]
})
const newTrainingNotices = computed(() => {
  if (run.value?.status !== 'succeeded') return []
  const keys = ['字样输入审核', '训练材料准备', '个人训练准备']
  return [...new Set([runtimeError.value, runtimeSelectionProblem.value, ...keys.map(key => componentProblems.value[key]), ...(runtime.value?.blockers ?? []).map(row => `${row.code}：${row.detail}`)].filter(Boolean).map(userText))]
})
const nextAction = computed(() => {
  if (contextRestoring.value) return '正在恢复上次状态，请等待检查完成。'
  if (!selectedStyle.value) return '先选择个人字体档案，再准备图片。'
  if (pendingCreate.value) return '恢复未确认的创建：继续原请求，避免重复创建。'
  if (run.value?.status === 'succeeded') return '查看效果，完成评估与本人验收，再发布使用。'
  if (run.value?.status === 'created') return '草稿已保存；环境检查通过后，开始训练。'
  if (run.value && ['queued', 'running'].includes(run.value.status)) return '训练进行中，等待完成；需要时可以取消。'
  if (run.value && ['failed', 'cancelled'].includes(run.value.status)) return '训练已停止。查看原因后恢复，或保留记录创建新任务。'
  if (activeStep.value === 1) return '从 To 签参考图提取字形，再核对字符和完整笔画。'
  if (activeStep.value === 2) return '确认字样：核对字符和完整笔画，再准备训练材料。'
  return canCreateRun.value ? '材料与环境已检查，可以创建新运行，再开始训练。' : '暂时不能训练。请完成字样确认，并解决下方阻塞原因。'
})
onMounted(() => { mounted = true; void restorePageContext() })
watch([() => route.query.style_id, () => route.query.run_id], () => { if (mounted && !disposed) void restorePageContext() })
onBeforeUnmount(() => { reviewDialog.value?.close?.(); disposed = true; ++sampleGeneration; ++runtimeGeneration; ++runGeneration; stopPolling(); if (noticeTimer !== null) window.clearTimeout(noticeTimer) })
</script>

<template>
  <main class="bg-blueprint flex h-[100dvh] flex-col overflow-hidden text-ink select-none">
    <h1 class="sr-only">训练我的字体</h1>
    <header class="safe-t z-20 flex h-14 shrink-0 items-center justify-between gap-2 border-b border-ink/15 px-3">
      <button type="button" aria-label="返回工作台" class="btn-hard btn-hard--compact !rounded-full !px-3" @click="router.push('/')">
        <svg aria-hidden="true" class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M19 12H5"></path><path d="m12 19-7-7 7-7"></path></svg>
      </button>
      <div class="flex min-w-0 flex-col items-center"><p class="truncate text-sm font-black">训练我的字体</p><span class="tag-code mt-0.5">跟着四步完成</span></div>
      <span class="rounded-full border-2 border-amber bg-amber/15 px-2 py-1 text-[10px] font-black">真实运行</span>
    </header>

    <section class="mx-auto w-full max-w-5xl flex-1 space-y-4 overflow-y-auto overflow-x-hidden px-4 pb-10 pt-4">
      <div v-if="errorText" class="flex items-start justify-between gap-2 rounded-xl border-2 border-coral bg-blush px-3 py-2" role="alert"><div><p class="break-all text-xs font-black text-danger">{{ errorText }}</p><p v-if="errorGuidance" class="mt-1 text-xs">{{ errorGuidance }}</p></div><button type="button" class="font-black" aria-label="关闭错误" @click="errorText = null">×</button></div>

      <section class="rounded-2xl border-2 border-ink bg-surface p-4" aria-label="当前字体与下一步">
          <details class="mb-3 text-xs" :open="Boolean(existingRunError) || !run" data-testid="existing-run-recovery"><summary class="min-h-11 cursor-pointer font-black">恢复已有训练（不重新训练）</summary>
            <p class="mt-1">输入已有运行编号，只读取本账号的训练记录。已有训练的评估与新训练材料准备分别检查。</p>
            <label class="mt-2 block">运行编号<input v-model="existingRunNumber" aria-label="已有训练运行编号" inputmode="numeric" class="mt-1 min-h-11 w-full rounded border-2 border-ink px-2" :disabled="contextRestoring || runLoading || busyAction !== null || Boolean(pendingCreate) || foreignPendingCreate || Boolean(run && !isTerminal)"></label>
            <button type="button" class="btn-hard mt-2 min-h-11" :disabled="contextRestoring || runLoading || busyAction !== null || Boolean(pendingCreate) || foreignPendingCreate || Boolean(run && !isTerminal)" @click="recoverExistingRun()">{{ runLoading ? '读取中…' : '读取已有训练' }}</button>
            <p v-if="pendingCreate || foreignPendingCreate" class="mt-2 text-amber">请先处理未确认的创建请求；已有请求不会被运行链接覆盖。</p>
            <p v-if="existingRunError" class="mt-2 text-danger" role="alert">{{ existingRunError }}</p>
          </details>
          <label class="text-xs font-black">风格档案<select aria-label="风格档案" v-model.number="selectedStyleId" class="mt-1 min-h-11 w-full rounded-xl border-2 border-ink bg-canvas px-3" :disabled="contextRestoring || Boolean(recoveryError) || runLoading || loadingStyles || Boolean(run && !isTerminal) || busyAction !== null || reviewBusy || humanInputBusy || Boolean(reviewPlan) || Boolean(pendingCreate)" @change="syncStyleRoute"><option :value="null">选择风格档案</option><option v-for="style in styles" :key="style.style_id" :value="style.style_id">{{ styleDisplayName(style) }}（编号 {{ style.style_id }}）</option></select></label>
          <details v-if="selectedStyle" class="text-xs"><summary class="min-h-11 cursor-pointer font-bold">高级：字体登记信息</summary><p class="break-all">来源 {{ stateLabel(selectedStyle.source_type) }} · 登记状态 {{ stateLabel(selectedStyle.status) }} · 编号 {{ selectedStyle.style_id }}。登记状态不代表训练模型可用，以下方训练检查为准。</p></details>
          <p v-if="contextRestoring" role="status" class="text-xs font-bold">正在恢复档案与上次运行；校验完成前不会创建或切换环境。</p>
          <div v-if="recoveryError" class="rounded-lg border border-danger p-2 text-xs text-danger" data-testid="run-recovery-error">
            <p role="alert">上次运行身份尚未恢复，已阻止创建新运行与切换环境：{{ recoveryError }}</p>
            <button type="button" class="min-h-11 font-bold underline" :disabled="contextRestoring || busyAction !== null" @click="restorePageContext">重试恢复上次运行</button>
          </div>
          <p v-if="styleContextNotice" role="status" class="text-xs font-bold text-amber">{{ styleContextNotice }} <RouterLink to="/training" class="underline">打开训练控制台恢复</RouterLink></p>
          <p v-if="stylesError" class="text-xs font-black text-danger" role="alert">风格加载失败：{{ stylesError }}</p>
          <p v-if="!loadingStyles && !stylesError && !styles.length" class="text-xs font-bold text-amber">没有风格档案。<RouterLink to="/styles/new" class="underline">创建个人手写档案</RouterLink>，确认来源后经 P4 登记字符候选。</p>
        <nav aria-label="字体训练步骤" class="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-4">
          <button v-for="(label, index) in stepLabels" :key="label" type="button" :data-testid="`choose-step-${index + 1}`" :aria-current="activeStep === index + 1 ? 'step' : undefined" class="min-h-12 rounded-xl border-2 border-ink px-3 py-2 text-left text-xs font-bold" :class="activeStep === index + 1 ? 'bg-mint' : 'bg-canvas'" @click="activeStep = index + 1">{{ index + 1 }} · {{ label }}</button>
        </nav>
        <p role="status" data-testid="training-next-action" class="mt-3 text-sm font-bold">下一步：{{ nextAction }}</p>
        <p class="mt-1 text-xs text-ink-muted">当前状态：{{ run ? runLabel[run.status] : '尚未开始训练' }}。AI 结果与本人验收分开，发布以服务端检查为准。</p>
        <p v-if="run?.status === 'succeeded'" class="mt-2 text-xs font-bold">已有训练已完成，可继续查看效果。新训练的材料准备提醒不代表已有模型未训练；过期字样审核仍须重新完成正式审核。评估与发布按此运行的服务端发布门检查。</p>
        <div v-if="trainingBlockers.length" data-testid="training-blockers" role="alert" class="mt-3 rounded-xl border border-coral bg-blush p-3 text-xs">
          <p class="font-bold">需要先解决的问题</p><ul class="mt-1 list-inside list-disc space-y-1"><li v-for="reason in trainingBlockers" :key="reason" class="break-words">{{ reason }}</li></ul>
          <button type="button" class="mt-2 min-h-11 font-bold underline" :disabled="runtimeLoading || contextRestoring || busyAction !== null" @click="loadRuntime">重新检查训练环境</button>
        </div>
        <details v-if="newTrainingNotices.length && activeStep <= 3" class="mt-3 rounded-xl border border-amber p-3 text-xs" data-testid="new-training-notices"><summary class="min-h-11 cursor-pointer font-bold">新训练准备提醒（不影响已有模型继续评估）</summary><ul class="mt-2 list-inside list-disc"><li v-for="notice in newTrainingNotices" :key="notice">{{ notice }}</li></ul></details>
      </section>
      <UnpublishedModels />

      <section v-show="activeStep === 1" data-testid="training-step-1" class="rounded-2xl border-2 border-ink bg-surface p-4 shadow-[3px_3px_0_var(--color-ink)]">
        <TrainingReferenceImages :style-id="selectedStyle?.style_id" />
        <div class="flex items-center justify-between"><h2 class="text-sm font-black">1 · 从参考原图提取字形</h2><span role="status" class="tag-code">{{ samplePageReadyCount }} 条已登记字形 · {{ readyCharacterCount }} 个已登记字符</span></div>
        <p class="mt-1 text-xs font-bold text-amber">训练资格以正式审核为准；素材登记计数不表示审核或训练准入通过。</p>
        <p class="mt-1 text-[11px] font-bold text-ink-muted">从 To 签参考图提取字形 → 确认字样 → 生成训练集。请通过上方原图进入提取页，由项目提取和登记字形，无需上传裁好的训练集。</p>
        <details class="mt-3 text-xs" data-testid="historical-training-materials"><summary class="min-h-11 cursor-pointer font-bold">高级：历史素材记录（登记不表示可训练）</summary>
        <p class="mt-2 text-ink-muted">保留全部历史素材，不根据文件名推断来源或训练资格。只有来源完整、正式审核通过的字样才能纳入训练集；已有正式训练集及训练记录保持不变。</p>
        <div v-if="loadingImages" class="mt-3 text-center text-xs text-ink-muted" role="status">正在读取历史素材…</div>
        <div class="mt-3 grid grid-cols-3 gap-2 sm:grid-cols-4">
          <article v-for="image in images" :key="image.image_id" data-testid="existing-training-glyph" style="background-color:#aaa;background-image:conic-gradient(#eee 25%,transparent 0 50%,#eee 0 75%,transparent 0);background-size:16px 16px" class="relative flex aspect-[3/4] flex-col justify-end overflow-hidden rounded-xl border-2 border-ink">
            <img v-if="!failedPreviewIds.has(image.image_id)" :src="imageUrl(image.image_id)" alt="历史字图素材预览" class="absolute inset-0 h-full w-full object-contain" loading="lazy" @error="markPreviewFailed(image.image_id)"><div v-else class="absolute inset-0 flex items-center justify-center text-[9px]">预览不可用</div>
            <div class="relative bg-surface/90 p-1.5"><p class="text-[10px] font-bold">字形记录 {{ image.image_id }}</p><span class="text-[10px] font-black">{{ stateLabel(image.status) }}</span><details class="text-[9px]"><summary class="min-h-8 cursor-pointer">高级：原文件与维护</summary><p class="break-all">{{ image.file_name }}</p><p>{{ image.width }}×{{ image.height }} · {{ Math.round(image.size_bytes / 1024) }} KB</p><button type="button" class="min-h-8 font-black text-danger underline" :disabled="deleteBusyId === image.image_id" :aria-label="`删除训练图 ${image.file_name}`" @click="deleteImage(image)">{{ deleteBusyId === image.image_id ? '删除中' : '删除' }}</button></details></div>
          </article>
        </div>
        <div class="mt-3 flex items-center justify-between text-xs"><button type="button" class="btn-hard btn-hard--compact" :disabled="loadingImages || imagePage <= 1" @click="loadImages(imagePage - 1)">上一页</button><span>{{ imagePage }} / {{ imagePageCount }} · {{ imageTotal }} 张</span><button type="button" class="btn-hard btn-hard--compact" :disabled="loadingImages || imagePage >= imagePageCount" @click="loadImages(imagePage + 1)">下一页</button></div>
        <p v-if="!loadingImages && imageTotal === 0" class="mt-3 text-center text-xs text-ink-muted" role="status">尚无已提取字形，请先打开参考原图并提取文字。</p>
        </details>
        <RouterLink :to="selectedStyleId ? `/extract?style_id=${selectedStyleId}` : '/extract'" class="btn-hard mt-3 min-h-12 w-full">进入原图提取页</RouterLink>
        <button type="button" class="mt-2 min-h-11 text-xs font-bold underline" @click="activeStep = 2">已有字样，下一步确认</button>
      </section>

<div v-show="activeStep === 2" data-testid="training-step-2">
      <section v-if="selectedStyle?.source_type === 'HANDWRITING_ASSET'" class="rounded-2xl border-2 border-ink bg-surface p-4 shadow-[3px_3px_0_var(--color-ink)]" aria-labelledby="owner-confirm-heading">
        <div class="flex flex-wrap items-center justify-between gap-2"><h2 id="owner-confirm-heading" class="text-sm font-black">2 · 确认字样 · {{ selectedStyle.name }}</h2><span class="tag-code !whitespace-normal">候选 {{ candidateSamples.length }} · 本人确认 {{ ownerConfirmedCharacters.size }} · AI 授权确认 {{ aiConfirmedCharacters.size }} · 已排除 {{ rejectedSampleCount }}</span></div>
        <p class="mt-2 rounded-lg border border-amber bg-amber/10 p-2 text-[11px] font-bold">请对照图片核对字样和完整笔画。本人确认与 AI 授权确认分开记录；确认后再准备训练材料。</p>
        <div v-if="candidateSamples.length" class="mt-3 space-y-2">
          <article v-for="sample in candidateSamples" :key="sample.sample_id" class="grid grid-cols-[auto_4rem_minmax(0,1fr)] gap-2 rounded-xl border border-ink/15 bg-canvas p-2">
            <input v-model="selectedCandidateIds" type="checkbox" :value="sample.sample_id" :aria-label="`选择候选 ${sample.sample_id}`" class="mt-2 h-4 w-4" :disabled="reviewBusy || Boolean(reviewPlan)">
            <img v-if="sample.source_training_image_id" :src="imageUrl(sample.source_training_image_id)" alt="候选字形 PNG" style="background-color:#aaa;background-image:conic-gradient(#eee 25%,transparent 0 50%,#eee 0 75%,transparent 0);background-size:16px 16px" class="h-16 w-16 rounded-lg border border-ink/20 bg-white object-contain">
            <div v-else class="flex h-16 w-16 items-center justify-center rounded-lg border border-dashed border-coral text-[9px] text-danger">无字形图</div>
            <div class="min-w-0">
              <label class="block text-[10px] font-bold">候选字符
                <input v-model="sample.text_label" :readonly="!candidateHumanMode || Boolean(reviewPlan) || reviewBusy" type="text" maxlength="2" class="mt-1 min-h-9 w-full rounded-lg border border-ink bg-white px-2 text-base" :aria-label="`修正候选字符 ${sample.sample_id}`">
              </label>
              <p class="mt-1 truncate text-[9px]" :title="sample.source_asset_id">来源图片 {{ sample.source_training_image_id ?? '缺失' }}</p>
              <details><summary class="min-h-8 cursor-pointer text-[10px]">高级：字形校验记录</summary>              <p class="truncate text-[9px] text-ink-muted" :title="sample.final_alpha_sha256 ?? ''">touchup {{ sample.touchup_edit_count ?? '未知' }} 次 · 最终 hash {{ sample.final_alpha_sha256?.slice(0, 12) ?? '缺失' }}</p></details>
              <p v-if="!sample.provenance_complete" class="mt-1 text-[9px] font-black text-danger">来源链不完整：可排除，不能确认训练</p>
              <p v-else class="mt-1 text-[9px] font-bold text-amber">候选待确认；尚未记为本人确认</p><p v-if="sample.candidate_visual_review" class="mt-1 text-[9px]">AI 复核：{{ stateLabel(sample.candidate_visual_review.review_state) }} · {{ stateLabel(sample.candidate_visual_review.recommendation) }} · {{ sample.candidate_visual_review.reason ? userText(sample.candidate_visual_review.reason) : '' }}</p><ul v-if="sample.candidate_visual_review?.isolation_reasons?.length" class="mt-1 text-[9px] text-danger"><li v-for="reason in sample.candidate_visual_review.isolation_reasons" :key="reason">保留待复核：{{ reason }}</li></ul>
            </div>
          </article>
          <button type="button" class="btn-hard min-h-12 w-full bg-primary text-white" :disabled="reviewBusy || !canAiConfirm || candidateHumanMode || !candidateSelection.length || candidateSelection.length > 100" @click="submitAiCandidateConfirmation">{{ reviewBusy ? '提交中…' : `AI 复核并确认 ${candidateSelection.length} 条` }}</button>
          <p v-if="!canAiConfirm" class="text-[11px] text-amber">AI 候选确认当前未获授权或能力不可用</p>
          <label class="flex min-h-11 items-center gap-2 text-xs"><input v-model="candidateHumanMode" type="checkbox" :disabled="reviewBusy">本人手动复核（仅实际本人操作时选择）</label>
          <button v-if="candidateHumanMode" type="button" class="btn-hard" :disabled="reviewBusy || loadingSamples || Boolean(reviewPlan)" @click="selectCandidateBatch">选择前 {{ Math.min(candidateSamples.length, 100) }} 条</button>
          <div v-if="candidateHumanMode" class="grid grid-cols-1 gap-2 sm:grid-cols-2">
            <button type="button" class="btn-hard min-h-11 bg-primary text-white" :disabled="reviewBusy || loadingSamples || Boolean(samplesError) || Boolean(reviewPlan) || !candidateSelection.length || candidateSelection.length > 100 || selectedStyle.status !== 'approved' || candidateSelection.some(sample => !sample.provenance_complete || !isSingleCjk(sample.text_label))" @click="openCandidateReview('confirm', $event)">{{ reviewBusy ? '提交中…' : `确认选中的 ${candidateSelection.length} 个字符` }}</button>
            <button type="button" class="btn-hard min-h-11 border-coral text-danger" :disabled="reviewBusy || loadingSamples || Boolean(samplesError) || Boolean(reviewPlan) || !candidateSelection.length || candidateSelection.length > 100" @click="openCandidateReview('reject', $event)">排除选中的 {{ candidateSelection.length }} 个候选</button>
            <p v-if="candidateSelection.length > 100" class="col-span-2 text-[10px] font-black text-danger">单次最多 100 条，请取消部分选择后分批提交。</p>
          </div>
          <p class="text-[9px] text-ink-muted">AI 确认由服务端重新复核字形与来源图；本页不代填分数。本人手动复核可修正字符标签。每次最多 100 条。</p>
        </div>
        <p v-if="samplesError" role="alert" class="mt-3 text-xs text-danger">候选读取失败：{{ samplesError }}<button type="button" class="btn-hard" @click="loadStyleSamples">重试读取候选</button></p>
        <p v-else-if="!candidateSamples.length && !loadingSamples" class="mt-3 text-xs text-ink-muted">暂无候选。通过 P4 从已审核原图提取并登记后，候选会自动出现在这里。</p>
        <HumanDatasetReview @problem="componentProblems['字样输入审核'] = $event" v-if="selectedStyleId" :key="`human-${selectedStyleId}`" :style-id="selectedStyleId" :review-key="snapshotRefreshToken" :locked="busyAction !== null || reviewBusy || contextRestoring" @busy="humanInputBusy = $event" @reviewed="humanInputRevision += 1; loadRuntime()" />
        <details class="mt-3"><summary class="min-h-11 cursor-pointer text-xs font-bold">准备训练材料：检查来源与分组</summary>
        <SnapshotFamilyPlan @problem="componentProblems['训练材料准备'] = $event" v-if="selectedStyleId" :key="selectedStyleId" :style-id="selectedStyleId" :disabled="busyAction !== null || reviewBusy || humanInputBusy || loadingSamples || contextRestoring || Boolean(reviewPlan)" :refresh-token="snapshotRefreshToken" @busy="snapshotBusy" @prepared="snapshotPrepared" />
        </details>
        <button type="button" class="btn-hard mt-3 min-h-12 w-full" @click="activeStep = 3">字样已核对，前往训练</button>
        <details v-if="aiConfirmedSamples.length" class="mt-3 rounded-lg border border-ink/20 p-2 text-xs">
          <summary class="min-h-11 cursor-pointer font-bold">AI 授权确认记录 · {{ aiConfirmedSamples.length }} 条</summary>
          <p v-for="sample in aiConfirmedSamples" :key="sample.sample_id" class="mt-2 break-all">{{ sample.text_label }} · {{ sample.sample_id }} · 复核者 {{ sample.reviewed_by ?? '服务端未报告' }} · {{ sample.reviewed_at ?? '时间未报告' }} · actor {{ sample.confirmation_actor_type ?? '未报告' }}</p>
        </details>
      </section>

<p v-if="selectedStyle?.source_type !== 'HANDWRITING_ASSET'" class="rounded-xl border border-amber p-3 text-xs">请先选择个人手写档案；图片提取后的字样会出现在这里。</p>
</div>
      <section v-show="activeStep === 3" data-testid="training-step-3" class="rounded-2xl border-2 border-ink bg-surface p-4 shadow-[3px_3px_0_var(--color-ink)]" aria-labelledby="run-heading">
        <div class="flex items-center justify-between gap-2"><h2 id="run-heading" class="text-sm font-black">3 · 开始训练</h2><span v-if="run" class="rounded-full border-2 px-2 py-0.5 text-[10px] font-black" :class="runStatusClass[run.status]">{{ runLabel[run.status] }} · #{{ run.run_id }}</span><span v-else class="tag-code">尚无运行</span></div>
        <p class="mt-2 rounded-xl border border-amber bg-amber/10 p-3 text-[11px] font-bold leading-relaxed">先保存训练草稿，再开始训练。刷新页面会恢复上次任务；训练完成后还需要查看效果并验收。</p>
        <div class="mt-3 grid gap-2">
          <details class="text-xs"><summary class="min-h-11 cursor-pointer font-bold">高级：训练方式</summary>          <label class="text-xs font-black">运行引擎<select aria-label="运行引擎" v-model="engineChoice" class="mt-1 min-h-11 w-full rounded-xl border-2 border-ink bg-canvas px-3" :disabled="contextRestoring || Boolean(recoveryError) || runLoading || Boolean(run && !isTerminal) || busyAction !== null || Boolean(pendingCreate)"><option value="m1">M1 冻结基座推理</option><option value="m2">M2 个人适配器训练</option></select></label></details>
          <div v-if="engineChoice === 'm2'" class="rounded-xl border-2 border-ink/20 bg-canvas p-3" data-testid="m2-runtime">
            <div class="flex items-center justify-between gap-2"><h3 class="text-xs font-black">训练前检查</h3><button type="button" class="min-h-11 text-xs font-bold underline" :disabled="runtimeLoading || selectedStyleId == null || busyAction !== null" @click="loadRuntime">{{ runtimeLoading ? '检查中…' : '重新检查' }}</button></div>
            <details class="mt-2 text-xs"><summary class="min-h-11 cursor-pointer font-bold">高级：配方、设备与环境目录</summary>            <label class="mb-2 block text-xs font-black">M2 训练配方
              <select :value="selectedRecipe" aria-label="M2 训练配方" class="mt-1 min-h-11 w-full rounded-xl border-2 border-ink bg-surface px-2" :disabled="runtimeSelectionLocked || selectedStyleId == null" @change="chooseRecipe">
                <option value="m2-control-corrected-v1">已安装 control 配方</option>
                <option :value="USER_SCR_RECIPE">个人快照 SCR · 需要独立环境资格</option>
                <option :value="WINDOWS_SCR_RECIPE">Windows CPU/CUDA 个人快照 SCR · 需要实测运行资格</option>
                <option v-if="selectedRecipe !== 'm2-control-corrected-v1' && !isUserScrRecipe(selectedRecipe)" :value="selectedRecipe">历史配方不可识别 · {{ selectedRecipe }}</option>
              </select>
            </label>
            <label class="mb-2 block text-xs font-black">训练运行环境
              <select :value="selectedRuntimeRegistration" aria-label="训练运行环境" class="mt-1 min-h-11 w-full rounded-xl border-2 border-ink bg-surface px-2" :disabled="runtimeSelectionLocked || selectedStyleId == null || loadingStyles" @change="chooseRuntime">
                <option value="">默认 CPU · 服务端自动发现</option>
                <option v-if="missingSelectedRuntime" :value="selectedRuntimeRegistration">已绑定注册不可用 · {{ selectedRuntimeRegistration }}</option>
                <option v-for="option in registeredRuntimeOptions" :key="option.registration_id" :value="option.registration_id">{{ runtimeOptionLabel(option) }}</option>
              </select>
            </label>
            <p class="mb-2 text-[11px]">环境由服务端校验。已安装依赖或检测到 GPU 不表示 M2 完整模型可训练；不会自动回退到另一环境。</p>
            <p v-if="missingSelectedRuntime && !runtimeLoading" class="mb-2 break-all text-xs font-bold text-danger" data-testid="missing-runtime">该运行绑定的注册缺失、无效或已停用：{{ selectedRuntimeRegistration }}。请恢复原注册后重新检查。</p>
            <ul v-if="runtimeOptions.length" class="mb-2 space-y-1 text-[11px]" data-testid="runtime-catalog">
              <li v-for="(option, index) in runtimeOptions" :key="option.registration_id ?? index" class="break-all">
                {{ runtimeOptionLabel(option) }} · 依赖/硬件：{{ option.environment_ready ? '已验证' : '未通过' }} · 完整模型：{{ option.full_model_ready && option.ready ? '已验证' : '未通过' }}
                <span v-if="option.code || option.reason" class="text-danger"> · {{ option.code }} {{ option.reason }}</span>
              </li>
            </ul>
</details>
            <p v-if="runtimeLoading" class="text-xs" role="status">正在校验训练快照、基座和 ML 运行环境…</p>
            <p v-else-if="runtimeError" class="text-xs font-bold text-danger" role="alert">{{ runtimeError }}</p>
            <template v-else-if="runtime">
              <p class="text-xs font-black" :class="runtimeReady ? 'text-emerald-800' : 'text-danger'">{{ runtimeReady ? (canCreateRun && (!run || isTerminal) ? '已就绪 · 可以创建 M2 运行' : '环境与配方已验证；创建仍受当前恢复/运行状态限制') : '尚未就绪 · 已阻止创建' }}</p>
              <p v-if="runtimeSelectionProblem" class="text-xs font-bold text-danger" role="alert">{{ runtimeSelectionProblem }}</p>
              <details v-if="runtime.config" class="mt-2"><summary class="min-h-11 cursor-pointer text-xs font-bold">高级：训练参数与校验记录</summary><M2RecipeSummary :config="runtime.config" /></details>
              <PersonalRuntimeInstallation v-if="runtime.installation && !runtime.ready" :installation="runtime.installation" />
              <ul v-if="runtime.blockers.length" class="mt-2 space-y-1 text-xs"><li v-for="(blocker, index) in runtime.blockers" :key="`${blocker.code}-${index}`" class="break-all"><span class="font-black">{{ userText(`${blocker.code}：${blocker.detail}`) }}</span></li></ul>
              <details v-if="runtime.snapshot_preparation_dependencies?.status === 'BLOCKED_DEPENDENCY'" class="mt-2 rounded-lg border border-amber p-2 text-[11px]" data-testid="snapshot-dependency-block">
                <summary class="font-bold text-danger">新快照准备被缺失的来源完整性元数据阻塞</summary>
                <p class="mt-1">已验证的历史快照仍可用于训练；请从原始备份恢复下列精确文件，不要重新生成收据或替换校验值。</p>
                <ul class="mt-1 space-y-1 break-all"><li v-for="entry in runtime.snapshot_preparation_dependencies.missing_metadata" :key="entry.path">{{ entry.path }} · SHA256 {{ entry.expected_sha256 }}</li></ul>
              </details>
              <p class="mt-2 text-[11px]">使用范围：{{ stateLabel(runtime.acceptance_policy?.mode ?? '服务端未报告') }} · 人工盲评：{{ runtime.acceptance_policy?.human_blind_review === 'WAIVED_BY_USER' ? '用户已明确豁免' : runtime.acceptance_policy?.human_blind_review ?? '服务端未报告' }}</p>
              <p class="mt-1 text-[11px]">候选确认：{{ runtime.acceptance_policy?.candidate_confirmation === 'AI_AUTHORIZED' ? '已授权 AI 独立确认' : runtime.acceptance_policy?.candidate_confirmation ?? '服务端未报告' }} · AI 视觉服务：{{ runtime.capabilities.ai_visual_review ? '可用' : '当前不可用' }}</p>
              <p v-if="!runtime.capabilities.ai_visual_review" class="mt-1 text-[11px] text-amber">服务不可用时不会生成评分；已有证据是否有效，以运行发布门为准。</p>
            </template>
            <p v-else class="text-xs text-ink-muted">请选择风格档案后检查环境</p>
            <template v-if="genericRecipeSelected && selectedStyleId">
              <label class="mt-2 block font-bold">训练时间预算（秒）<input v-model.number="maxSeconds" type="number" min="1" max="86400" aria-label="训练时间预算（秒）" class="mt-1 min-h-11 w-full rounded-lg border border-ink px-2" :disabled="runtimeSelectionLocked"></label>
              <p>默认 3600 秒；时间上限不是训练完成步数，超时也不表示质量通过。</p>
              <p v-if="!validBudget(maxSeconds)" role="alert" class="text-danger">预算必须为大于 0 且不超过 86400 的有限秒数。</p>
              <UserScrPreparation @problem="componentProblems['个人训练准备'] = $event" :style-id="selectedStyleId" :registration-id="selectedRuntimeRegistration" :snapshot-sha="snapshotSha" :blockers="runtime?.blockers ?? []" :runtime-error="runtimeError ?? runtimeSelectionProblem" :disabled="runtimeLoading || runtimeSelectionLocked || reviewBusy || humanInputBusy" @busy="snapshotBusy" @prepared="scrPrepared" />
            </template>
          </div>
          <div v-if="runIsM2 && run" class="rounded-xl border border-ink/20 p-3" data-testid="run-runtime-binding">
            <p class="break-all text-xs font-black">当前运行 #{{ run.run_id }} 的冻结环境：{{ runRuntimeBinding ? `${runRuntimeBinding.device} · ${runRuntimeBinding.variant_id} · ${runRuntimeBinding.registration_id}` : registrationForConfig(run.config) === '' ? '历史默认 CPU（未登记注册绑定）' : '无效或缺失的运行绑定；已阻止启动' }}</p>
            <details v-if="runRuntimeBinding"><summary class="min-h-11 cursor-pointer text-xs">高级：运行环境身份</summary><p class="break-all text-[10px]">绑定标识：{{ runRuntimeBinding.binding_sha256 }}</p></details>
            <p v-if="run.status === 'created' && !canStartRun" class="text-xs text-danger">原绑定的运行环境尚未校验通过，不能启动此运行。历史配方与绑定不会被当前选择替换。</p>
            <p v-if="recipeProblem(run.config)" class="mt-1 text-xs text-danger">{{ recipeProblem(run.config) }}</p>
            <details><summary class="min-h-11 cursor-pointer text-xs font-bold">高级：本次任务的冻结参数</summary><M2RecipeSummary :config="run.config" frozen /></details>
            <div v-if="genericDraft" class="mt-2" data-testid="scr-draft-budget">
              <label class="block text-xs font-bold">草稿时间预算（秒）<input v-model.number="draftMaxSeconds" aria-label="草稿时间预算（秒）" type="number" min="1" max="86400" class="mt-1 min-h-11 w-full rounded-lg border border-ink px-2" :disabled="contextRestoring || runLoading || busyAction !== null"></label>
              <button type="button" class="btn-hard mt-2 min-h-11" :disabled="!validBudget(draftMaxSeconds) || draftBudgetUnchanged || contextRestoring || runLoading || busyAction !== null" @click="saveRunBudget">保存草稿时间预算</button>
              <p class="mt-1 text-[11px]">须保存并重新读取服务端配置后才能启动；不会修改已有快照、注册绑定或质量门。</p>
            </div>
          </div>
          <div v-if="pendingCreate?.body.config.engine === 'm2'" data-testid="pending-runtime-binding">
            <p class="break-all text-xs font-bold text-amber">未确认请求的环境：{{ registrationForConfig(pendingCreate.body.config) || '历史默认 CPU' }}；恢复仅重试原请求，不会修改绑定或配方。</p>
            <M2RecipeSummary :config="pendingCreate.body.config" frozen />
          </div>
          <div v-if="engineChoice === 'm1'" class="grid grid-cols-1 gap-2 sm:grid-cols-2">
            <label class="text-xs font-black">目标字<select v-model="selectedChar" class="mt-1 min-h-11 w-full rounded-xl border-2 border-ink bg-canvas px-3" :disabled="!styleSamples.length || Boolean(run && !isTerminal)"><option value="">选择已登记字符</option><option v-for="char in sampleChars" :key="char" :value="char">{{ char }} · {{ styleSamples.filter(s => s.text_label === char).length }} 样本</option></select></label>
            <label class="text-xs font-black">随机种子<input v-model.number="seed" type="number" class="mt-1 min-h-11 w-full rounded-xl border-2 border-ink bg-canvas px-3" :disabled="contextRestoring || Boolean(recoveryError) || runLoading || Boolean(run && !isTerminal) || busyAction !== null || Boolean(pendingCreate)"></label>
          </div>
          <div v-if="engineChoice === 'm1'" class="flex items-center justify-between text-[11px] font-bold text-ink-muted"><span>{{ loadingSamples ? '正在读取风格样本…' : `当前字符 ${eligibleSamples.length} 个可用参考样本` }}</span><button type="button" class="underline" :disabled="loadingSamples || selectedStyleId == null" @click="loadStyleSamples">刷新样本</button></div>

          <div v-if="engineChoice === 'm1'" class="max-h-44 space-y-1 overflow-auto rounded-xl border border-ink/15 p-2">
            <p v-if="!charSamples.length" class="text-xs text-ink-muted">该字符没有登记样本。请在 P4 提取并确认字形样本，再回到这里。</p>
            <label v-for="sample in charSamples" :key="sample.sample_id" class="flex items-center gap-2 rounded-lg bg-canvas px-2 py-1.5 text-xs">
              <input v-model="selectedSampleIds" type="checkbox" :value="sample.sample_id" :disabled="!sample.source_training_image_id || !imageById.has(sample.source_training_image_id) || Boolean(run && !isTerminal)">
              <span class="min-w-0 flex-1 truncate">{{ sample.text_label ?? '未标注' }} · 样本 #{{ sample.sample_id }} · 原图 #{{ sample.source_training_image_id ?? '无' }}</span>
              <span :class="imageById.has(sample.source_training_image_id ?? -1) ? 'text-emerald-800' : 'text-amber'">{{ stateLabel(imageById.get(sample.source_training_image_id ?? -1)?.status ?? '原图不可读') }}</span>
            </label>
          </div>
          <p v-if="engineChoice === 'm1'" class="text-[10px] font-bold text-ink-muted">样本资格按单字标签、StyleSample 的人工确认记录、source_training_image_id 和服务端原图读面联合判断；原图状态字段不代表字形已准备好。</p>
          <div class="grid grid-cols-1 gap-2 sm:grid-cols-2">
            <button type="button" class="btn-hard min-h-12" :disabled="contextRestoring || busyAction !== null || runLoading || (!pendingCreate && (!canCreateRun || Boolean(run && !isTerminal)))" @click="createRun">{{ busyAction === 'create' ? '创建中…' : pendingCreate ? '恢复未确认的创建' : '创建新运行' }}</button>
            <button v-if="run?.status === 'created'" type="button" class="btn-hard min-h-12 bg-primary text-white" :disabled="busyAction !== null || runLoading || !canStartRun" @click="mutateRun('start')">{{ busyAction === 'start' ? '启动中…' : '启动真实运行' }}</button>
            <button v-else-if="run && ['queued', 'running'].includes(run.status)" type="button" class="btn-hard min-h-12 border-coral text-danger" :disabled="busyAction !== null || runLoading" @click="mutateRun('cancel')">{{ busyAction === 'cancel' ? '取消中…' : '取消运行' }}</button>
            <button v-else type="button" class="btn-hard min-h-12" :disabled="!run || runLoading" @click="refreshRun">刷新/恢复</button>
          </div>
          <button v-if="runIsM2 && run?.status === 'created'" type="button" class="min-h-11 text-xs font-bold underline" :disabled="busyAction !== null || runLoading || contextRestoring || Boolean(pendingCreate)" @click="leaveDraft">保留旧草稿，创建新运行</button>
          <button v-if="preservedDraftId && !run" type="button" class="min-h-11 text-xs font-bold underline" :disabled="busyAction !== null || runLoading || contextRestoring || Boolean(pendingCreate)" @click="loadRun(preservedDraftId, { recoverStyle: true })">返回保留的草稿 #{{ preservedDraftId }}</button>
          <p v-if="pendingCreate" class="text-xs font-bold text-amber">上一次创建尚未确认。点击恢复将重用同一请求，避免重复创建；不会自动开启新的训练。</p>
          <p v-if="run && ['queued', 'running'].includes(run.status)" class="text-[10px] font-bold text-ink-muted">新建运行会被锁定，当前运行未终止前不能开另一个。取消/失败后可创建新运行重试。</p>
        </div>
      </section>


      <section v-if="run" v-show="activeStep === 3 || activeStep === 4" class="rounded-2xl border-2 border-ink bg-surface p-4 shadow-[3px_3px_0_var(--color-ink)]" aria-labelledby="progress-heading">
        <div class="flex items-center justify-between"><h2 id="progress-heading" class="text-sm font-black">服务端进度</h2><button type="button" class="text-xs font-black underline" :disabled="runLoading" @click="refreshRun">{{ runLoading ? '读取中…' : '刷新' }}</button></div>
        <div v-if="readiness" class="mt-2 rounded-lg border border-ink/20 p-2 text-xs" data-testid="readiness"><p>工程状态：{{ stateLabel(readiness.status) }}</p><p>AI 视觉复核：{{ stateLabel(readiness.AI_VISUAL_REVIEW) }} · AI 盲评：{{ stateLabel(readiness.AI_BLIND_REVIEW ?? '未报告') }}</p><p>人工盲评：{{ stateLabel(readiness.HUMAN_BLIND_REVIEW ?? readiness.OWNER_BLIND_REVIEW) }}</p><p v-if="readiness.HUMAN_BLIND_REVIEW === 'WAIVED_BY_USER'" class="font-bold text-amber">用户明确豁免人工盲评；不表示真人评分通过。</p><p>未通过项：{{ Object.keys(readiness.blockers).map(stateLabel).join('、') || '无' }}</p><ul v-if="readiness.details?.length" class="mt-1 space-y-1"><li v-for="detail in readiness.details" :key="detail" class="break-all">{{ userText(detail) }}</li></ul></div>
        <div v-if="Object.keys(paneErrors).length" class="mt-2 rounded-lg bg-blush p-2 text-xs" role="alert"><p v-for="(message, pane) in paneErrors" :key="pane">{{ pane }}读取失败：{{ message }}</p><p>已有数值仅为上次成功读取的结果；请刷新恢复。</p></div>
        <p class="mt-2 break-all text-xs font-bold">阶段：{{ stateLabel(progress?.stage ?? '服务端尚未报告') }} · 引擎：{{ stateLabel(progress?.engine_mode ?? '暂无') }}</p>
        <p v-if="run.status === 'succeeded' && progress?.admission?.status === 'PASSED'" class="mt-2 text-xs font-bold">{{ published?.status === 'approved' ? '补验入库通过，模型已发布' : readiness?.evaluation_state === 'complete' ? '补验入库通过，效果已评估，发布门另行核验' : '补验入库通过，质量待评估' }}；质量与发布状态以上方正式发布门为准。</p>
        <details v-if="progress?.historical_execution" class="mt-2 text-xs" data-testid="historical-execution"><summary class="min-h-11 cursor-pointer font-bold">历史执行记录（保留原失败，不代表当前补验失败）</summary><p>历史阶段：{{ progress.historical_execution.stage }}</p><p v-if="progress.historical_execution.failure">{{ progress.historical_execution.failure.code ?? '' }} · {{ progress.historical_execution.failure.message ?? '' }}</p></details>
<p class="mt-2 text-xs">完成 {{ progress?.units_done ?? '—' }} / {{ progress?.units_total ?? '—' }} {{ ['epoch', 'epochs'].includes(progress?.unit ?? '') ? '轮' : progress?.unit ?? '' }} · 预计剩余 {{ etaLabel(progress?.eta_s ?? null) }}</p>
        <details><summary class="min-h-11 cursor-pointer text-xs font-bold">高级：训练指标</summary>        <div class="mt-2 grid grid-cols-2 gap-2 text-xs"><p>进度：{{ progress?.units_done ?? '—' }} / {{ progress?.units_total ?? '—' }} {{ progress?.unit ?? '' }}</p><p>epoch：{{ progress?.epoch ?? '—' }} / {{ progress?.total_epochs ?? '—' }}</p><p>loss：{{ formatProgress(progress?.loss ?? null) }}</p><p>ETA：{{ etaLabel(progress?.eta_s ?? null) }}</p></div>
</details>
        <p v-if="progress?.failure" class="mt-2 rounded-lg bg-blush p-2 text-xs font-black text-danger" role="alert">{{ userError(progress.failure) }}</p>
        <p v-if="progress?.updated_at" class="mt-1 text-[10px] text-ink-muted">服务端更新时间：{{ progress.updated_at }}</p>
        <details><summary class="min-h-11 cursor-pointer text-xs font-bold">高级：训练日志</summary>        <pre v-if="progress?.log_tail?.length" class="mt-2 max-h-32 overflow-auto whitespace-pre-wrap rounded-lg bg-canvas p-2 text-[10px]">{{ progress.log_tail.join('\n') }}</pre>
        <p class="mt-1 text-[10px] text-ink-muted">进度和 loss 只显示服务端结果。更新间隔可能跨越整个 epoch，暂未更新不能据此判断训练进程死亡。</p>
</details>
        <RunRecovery v-if="runIsM2 && ['failed', 'cancelled'].includes(run.status)" :key="`${auth.me?.user_id}-${run.run_id}`" :run-id="run.run_id" :status="run.status" :disabled="contextRestoring || runLoading || busyAction !== null" @problem="componentProblems['训练恢复'] = $event" @continued="loadRun($event, { recoverStyle: true })" @busy="busyAction = $event ? 'continuation' : busyAction === 'continuation' ? null : busyAction" />
<details><summary class="min-h-11 cursor-pointer text-xs font-bold">高级：评估指标与收据</summary>        <div class="mt-3 border-t border-ink/10 pt-3"><h3 class="text-xs font-black">真实运行指标</h3><p class="mt-1 text-xs">{{ metrics?.state ?? '尚未读取' }} · {{ metrics?.reason ?? '没有服务端结果' }}</p><template v-if="metrics?.metrics"><p class="mt-1 text-xs">{{ runIsM2 ? '优化更新步（服务端 samples_seen）' : '服务端 samples_seen' }} {{ metrics.metrics.samples_seen ?? '—' }} · loss {{ formatProgress(metrics.metrics.loss) }} · 引擎 {{ metrics.metrics.engine_mode ?? '—' }}</p><p class="break-all text-[10px] text-ink-muted">receipt SHA256：{{ metrics.receipt?.sha256 ?? '无' }}</p></template></div></details>
        <div v-if="runIsM2 && run.status === 'succeeded'" class="mt-3 grid grid-cols-2 gap-2">
          <button type="button" class="btn-hard min-h-12" :disabled="busyAction !== null || runLoading || !readinessFresh || deploymentRequired || !readiness?.allowed_actions?.evaluate" @click="runWorkflow('evaluate')">{{ busyAction === 'evaluate' ? '启动中…' : readiness?.evaluation_state === 'running' ? '真实评估运行中' : '运行评估' }}</button>
          <button type="button" class="btn-hard min-h-12" :disabled="busyAction !== null || runLoading || !readinessFresh || !readiness?.allowed_actions?.ai_review" @click="runWorkflow('ai-review')">{{ busyAction === 'ai-review' ? '复核中…' : '运行 AI 视觉盲评' }}</button>
        </div>
        <p v-if="runLoading" class="mt-2 text-xs" role="status">正在核验已有运行和推理部署，完成后启用操作。</p>
        <p v-else-if="deploymentRequired" class="mt-2 text-xs">请先登记下方推理兼容部署，再运行评估。</p>
        <p v-else-if="!readinessFresh" class="mt-2 text-xs text-danger">运行状态读取失败，请点击刷新核对后再运行评估。</p>
        <p v-else-if="!readiness?.allowed_actions?.ai_review" class="mt-2 text-xs">AI 视觉盲评需等待真实评估完成且审核服务就绪。</p>
        <div class="mt-3 border-t border-ink/10 pt-3"><div class="flex items-center justify-between"><h3 class="text-xs font-black">评估读取</h3><button type="button" class="text-xs underline" :disabled="!isTerminal" @click="loadEvaluation">读取评估</button></div><p class="mt-1 text-xs">{{ stateLabel(evaluation?.state ?? '未读取') }} · {{ userText(evaluation?.reason ?? '只有真实评估收据可展示') }}</p><template v-if="evaluation?.report"><p class="mt-1 text-xs">proxy：字符 {{ evaluation.report.glyph_accuracy }} · 风格相似 {{ evaluation.report.style_similarity }} · 覆盖 {{ evaluation.report.coverage }}</p><p class="text-[10px] font-bold text-amber">自动 proxy 不是人工质量结论，也不会自动批准风格。</p></template></div>
      </section>

      <div v-show="activeStep === 4" data-testid="training-step-4" class="space-y-4">
        <section v-if="deploymentSupported" data-testid="inference-deployment" class="rounded-2xl border-2 border-ink bg-surface p-4 text-xs">
          <h2 class="text-sm font-black">推理兼容部署（保留原训练身份）</h2>
          <p class="mt-2">{{ inferenceDeployment?.state === 'registered' ? '已登记（未表示质量通过或发布）' : '尚未登记可验证的推理部署；登记通过后再运行真实评估。' }}</p>
          <p v-if="inferenceDeployment?.reason" class="mt-2 break-all">{{ userText(inferenceDeployment.reason) }}</p>
          <p v-if="!inferenceDeployment?.available?.some(option => option.ready) && inferenceDeployment?.state !== 'registered'" class="mt-2 text-amber">当前源码没有已就绪的对应 GPU 执行资格，需要维护者完成正式资格验证。</p>
          <label v-if="inferenceDeployment?.state !== 'registered'" class="mt-2 block">推理执行资格<select v-model="inferenceRegistration" aria-label="推理执行资格" :disabled="deploymentPending || deploymentUnconfirmed" class="mt-1 min-h-11 w-full rounded border border-ink"><option value="">选择正式就绪的注册</option><option v-for="option in inferenceDeployment?.available ?? []" :key="option.registration_id" :value="option.registration_id" :disabled="!option.ready">{{ option.variant_id }} · {{ option.device }} · {{ option.registration_id }}</option></select></label>
          <button v-if="inferenceDeployment?.state !== 'registered'" type="button" class="btn-hard mt-2 min-h-11" :disabled="deploymentPending || deploymentUnconfirmed || busyAction !== null || !inferenceRegistration" @click="registerInferenceDeployment">{{ deploymentPending ? '登记中…' : '登记推理兼容部署' }}</button>
          <p v-if="deploymentUnconfirmed" class="mt-2 text-amber">部署结果尚未确认，只读取原请求状态；不要重复提交或切换新注册。</p>
          <button type="button" class="mt-2 min-h-11 font-bold underline" :disabled="deploymentPending || runLoading" @click="readInferenceDeployment">读取兼容部署状态</button>
          <details class="mt-2"><summary class="min-h-11 cursor-pointer font-bold">训练与推理身份（独立绑定）</summary><p>原训练身份：{{ JSON.stringify(inferenceDeployment?.training_runtime_binding ?? runRuntimeBinding) }}</p><p>当前推理身份：{{ JSON.stringify(inferenceDeployment?.runtime_binding ?? null) }}</p><p>兼容部署：{{ JSON.stringify(inferenceDeployment?.deployment_binding ?? null) }}</p></details>
        </section>
        <section class="rounded-2xl border-2 border-ink bg-surface p-4">
          <h2 class="text-sm font-black">4 · 查看效果并使用</h2>
          <p class="mt-2 text-xs">训练完成后，先运行评估，查看实际生成效果，再完成验收和发布。只有审核通过的字体才能用于批量生成。</p>
          <p v-if="run?.status !== 'succeeded'" class="mt-2 text-xs text-amber">还没有训练成功的结果，请先完成第 3 步。</p>
          <RouterLink v-if="published?.status === 'approved'" to="/batch" class="btn-hard mt-3 min-h-12 w-full">用这个字体批量生成图片</RouterLink>
        </section>
      <OwnerDirectVisualAcceptance @problem="componentProblems['本人效果验收'] = $event" v-if="run?.status === 'succeeded' && run.config.engine === 'm2'" :run-id="run.run_id" :disabled="busyAction !== null || runLoading || !readinessFresh" @accepted="refreshRun" />
      <details class="rounded-xl border border-ink/20 p-3"><summary class="min-h-11 cursor-pointer text-xs font-bold">高级：外部输出与收据核验</summary><OwnerOutputEvidence @problem="componentProblems['输出证据'] = $event" v-if="run?.status === 'succeeded' && run.config.engine === 'm2'" :run-id="run.run_id" :disabled="busyAction !== null || runLoading || !readinessFresh" /></details>
      <section v-if="run?.status === 'succeeded'" class="rounded-2xl border-2 border-ink bg-surface p-4 shadow-[3px_3px_0_var(--color-ink)]" aria-labelledby="blind-review-heading">
        <h2 id="blind-review-heading" class="text-sm font-black">验收与发布</h2>
        <p class="mt-1 text-[11px] font-bold text-ink-muted">发布前再次核验模型、S03、评估和验收政策。AI 结果与人工结果保持独立，不会默认通过。</p>
        <p v-if="runIsM2" class="mt-2 text-xs">先运行评估生成真实效果图片，再在上方一次同意本次效果；无需逐字评分。</p>
        <label v-else class="mt-2 flex min-h-11 items-center gap-2 text-xs"><input v-model="humanReviewOpen" type="checkbox">高级：本人手动逐项评分</label>
        <div v-if="humanReviewOpen && !runIsM2">
        <div v-for="sample in runSamples" :key="sample.sample_id" class="mt-2 rounded-xl border border-ink/15 p-2">
          <div class="flex items-center gap-2"><img v-if="sample.source_training_image_id" :src="imageUrl(sample.source_training_image_id)" alt="盲评参考图" class="h-12 w-12 rounded object-contain"><div><p class="text-xs font-black">样本 #{{ sample.sample_id }} · {{ sample.text_label ?? '无字符标签' }}</p><p class="text-[10px] text-ink-muted">逐项人工判断结果</p></div></div>
          <label class="mt-2 block text-xs font-bold">人工评分<select v-model.number="blindScores[sample.sample_id]" class="mt-1 min-h-10 w-full rounded-lg border border-ink bg-canvas px-2"><option :value="null">请选择 0–3</option><option :value="0">0 · 不可接受</option><option :value="1">1 · 较差</option><option :value="2">2 · 可接受</option><option :value="3">3 · 良好</option></select></label>
        </div>
        <label class="mt-3 block text-xs font-black">真人整体结论<select v-model="blindVerdict" class="mt-1 min-h-10 w-full rounded-lg border border-ink bg-canvas px-2"><option value="">请选择人工结论</option><option value="fail">不通过</option><option value="pass">通过</option></select></label>
        <button type="button" class="btn-hard mt-3 min-h-12 w-full" :disabled="!canSubmitBlindReview || busyAction !== null" @click="submitBlindReview">{{ busyAction === 'blind-review' ? '提交中…' : '提交真人盲评' }}</button>
        </div>
        <p v-if="!canPublish && !published" class="mt-2 text-xs font-bold text-amber">当前尚未满足发布门，请检查上方真实评估和复核状态。</p>
        <button type="button" class="btn-hard mt-2 min-h-12 w-full bg-primary text-white" :disabled="busyAction !== null || runLoading || !canPublish || Boolean(published)" @click="publishRun">{{ busyAction === 'publish' ? '提交中…' : published ? (published.status === 'approved' ? '已发布' : '已发布申请') : '请求发布（服务端重新校验）' }}</button>
        <div v-if="published" class="mt-3 rounded-xl border border-mint bg-mint/20 p-3 text-xs"><p class="font-black">{{ published.status === 'approved' ? '风格已发布' : '发布申请已落服务端' }}</p><details><summary class="min-h-11 cursor-pointer font-bold">高级：发布记录</summary><p>style #{{ published.style_id }} · 状态 {{ stateLabel(published.status) }} · 质量门 {{ stateLabel(published.quality_status) }}</p><p v-if="published.artifact" class="break-all">artifact {{ published.artifact.artifact_id }} · {{ published.artifact.engine_mode }} · {{ published.artifact.engine_version }} · hash {{ published.artifact.sha256 }}</p></details></div>
      </section>


      </div>
      <details class="rounded-xl border border-ink/20 p-3 text-xs"><summary class="min-h-11 cursor-pointer font-bold">高级：已登记样本清单</summary>      <section v-if="selectedStyle" class="rounded-2xl border-2 border-ink bg-surface p-4 shadow-[3px_3px_0_var(--color-ink)]"><div class="flex items-center justify-between"><h2 class="text-sm font-black">StyleSample · {{ selectedStyle.name }}</h2><span class="tag-code">{{ styleSamples.length }} 条</span></div><p class="mt-1 text-[10px] text-ink-muted">样本字段来自真实 styles API：sample_id / text_label / source_training_image_id / uploaded_at。</p><div v-for="sample in styleSamples" :key="sample.sample_id" class="mt-2 flex items-center justify-between rounded-lg bg-canvas px-2 py-1.5 text-xs"><span>{{ sample.text_label || '未标注字符' }} · #{{ sample.sample_id }} · 原图 #{{ sample.source_training_image_id ?? '无' }}</span><span>{{ sample.text_label?.length === 1 && sample.source_training_image_id != null && imageById.has(sample.source_training_image_id) ? '可用于 M1' : '未达训练资格' }}</span></div><p v-if="!loadingSamples && !styleSamples.length" class="mt-2 text-xs text-ink-muted">暂无 StyleSample；请从 P4 提取和登记，不会把未提取原图直接当字形样本。</p></section></details>
    </section>
    <div v-if="notice" role="status" class="fixed inset-x-0 bottom-5 z-[60] flex justify-center px-4"><span class="rounded-full border-2 border-ink bg-ink px-4 py-2 text-xs font-black text-white">{{ notice }}</span></div>
    <dialog ref="reviewDialog" aria-labelledby="candidate-review-title" class="m-auto max-h-[85dvh] w-[calc(100%-2rem)] max-w-lg overflow-y-auto rounded-2xl border-2 border-ink bg-surface p-4 text-ink backdrop:bg-black/50" @cancel.prevent="closeCandidateReview">
      <template v-if="reviewPlan">
        <h2 id="candidate-review-title" class="text-lg font-black">{{ reviewPlan.decision === 'confirm' ? '本人确认字符' : '排除候选' }}</h2>
        <p class="mt-2 text-xs">{{ reviewPlan.styleName }} · #{{ reviewPlan.styleId }} · {{ reviewPlan.items.length }} 条</p>
        <p class="mt-2 text-xs font-bold">{{ reviewPlan.decision === 'confirm' ? '请对照字形确认以下标签属于你的真实笔迹。确认只登记审核记录，不冻结 S03、不启动训练。' : '这些候选将退出训练池。请填写本次排除的真实原因。' }}</p>
        <ul class="mt-3 max-h-64 space-y-2 overflow-auto rounded-xl bg-canvas p-3 text-xs"><li v-for="item in reviewPlan.items" :key="item.sample_id" class="flex items-center gap-3"><img v-if="item.source_training_image_id" :src="imageUrl(item.source_training_image_id)" alt="本次待复核字形" style="background-color:#aaa;background-image:conic-gradient(#eee 25%,transparent 0 50%,#eee 0 75%,transparent 0);background-size:16px 16px" class="glyph-preview h-20 w-20 shrink-0 rounded-lg object-contain"><div class="min-w-0 break-all"><p>{{ item.text_label || '未标注' }} · {{ item.sample_id }}</p><p class="mt-1 text-[10px] text-ink-muted">SHA256 {{ item.final_alpha_sha256 ?? '缺失' }}</p></div></li></ul>
        <label v-if="reviewPlan.decision === 'reject'" class="mt-3 block text-xs font-bold">排除理由<textarea v-model="reviewReason" aria-label="排除理由" maxlength="500" :disabled="reviewBusy" class="input-hard mt-1 min-h-20 w-full"></textarea></label>
        <label class="mt-3 flex min-h-11 items-center gap-2 text-xs font-bold"><input v-model="reviewAcknowledged" type="checkbox" aria-label="本人已逐项复核" :disabled="reviewBusy">本人已逐项复核，提交所列 {{ reviewPlan.items.length }} 条记录</label>
        <p v-if="reviewBusy" role="status" class="mt-2 text-xs">正在保存审核，请等待服务端结果。</p>
        <p v-if="reviewError" role="alert" class="mt-2 text-xs font-bold text-danger">{{ reviewError }} 请继续编辑后刷新候选，核对服务端状态。</p>
        <div class="adaptive-columns mt-3 grid grid-cols-2 gap-2"><button type="button" class="btn-hard" :disabled="reviewBusy" @click="closeCandidateReview">继续编辑</button><button type="button" class="btn-hard btn-hard--primary" :disabled="reviewBusy || Boolean(reviewError) || !reviewAcknowledged || (reviewPlan.decision === 'reject' && !reviewReason.trim())" @click="submitCandidateReview">{{ reviewBusy ? '提交中…' : '提交本人复核' }}</button></div>
      </template>
    </dialog>
</main>
</template>

<!-- P7 acceptance: server-bound M2 config, idempotent recovery, real metrics, distinct AI/human actors, fail-closed publish gates. -->
