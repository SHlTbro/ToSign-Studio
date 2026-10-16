<script setup lang="ts">
import { userError } from '@/utils/userLanguage'
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useAuthStore } from '@/stores/auth'
import { ApiRequestError, newIdempotencyKey, request } from '@/api/http'
import { ENDPOINT_NATIVE_REVIEW_DETAIL, ENDPOINT_NATIVE_REVIEW_INGRESS, ENDPOINT_NATIVE_REVIEW_REQUEST, ENDPOINT_NATIVE_REVIEW_IMPORT, ENDPOINT_NATIVE_REVIEW_REQUEST_CANCEL, fill } from '@/api/endpoints'
import { assertJob, isHash, isJobId } from '@/api/nativeIntake'
import { assertReviewIngress, ingressVersion } from '@/api/nativeReviewIngress'
import type { NativeReviewIngress, NativeReviewJob } from '@/types/nativeIntake'

const props = defineProps<{ job: NativeReviewJob; fresh: boolean; disabled: boolean }>()
const emit = defineEmits<{ busy: [value: boolean]; uncertain: [value: boolean]; blocked: [value: boolean]; changed: [] }>()
const auth = useAuthStore()
const identity = computed(() => `${auth.me?.user_id ?? ''}:${props.job.package.style_id}:${props.job.job_id}:${props.job.package_sha256}`)
const storageKey = computed(() => `yy:p4:review-ingress:pending:${identity.value}`)
type Action = 'request' | 'import' | 'cancel'
interface Pending { action: Action; request_id: string | null; bundle_sha256: string | null; operation_id?: string }
let generation = 0
let readGeneration = 0
let uncertaintyGeneration = 0
let disposed = false
const state = ref<NativeReviewIngress | null>(null)
const ingressFresh = ref(false)
const reading = ref(false)
const busy = ref<Action | null>(null)
const pending = ref<Pending | null>(null)
const error = ref('')
const notice = ref('')
const path = (template: string) => fill(template, { id: encodeURIComponent(props.job.job_id) })
const current = (g: number, key: string) => !disposed && g === generation && key === identity.value
const available = computed(() => props.fresh && !props.disabled && !busy.value && !reading.value)
const permitsApply = computed(() => ingressFresh.value && props.fresh && Boolean(state.value && (state.value.status === 'not_requested'
  || (state.value.status === 'imported' && state.value.stage?.bundle_sha256 === props.job.imported_bundle_sha256))))
const canMutate = computed(() => available.value && ingressFresh.value && !pending.value && props.job.status === 'pending' && props.job.code_version_matches === true)
const canRequest = computed(() => canMutate.value && !props.job.imported_bundle_sha256 && state.value?.status === 'not_requested')
const canImport = computed(() => canMutate.value && state.value?.status === 'staged' && Boolean(state.value.stage && state.value.request))
const canCancel = computed(() => canMutate.value && Boolean(state.value?.request) && ['requested', 'staged', 'imported'].includes(state.value?.status ?? ''))
const canResume = computed(() => {
  const operation = pending.value; const value = state.value
  if (!operation || !value || !available.value || !ingressFresh.value || props.job.status !== 'pending' || props.job.code_version_matches !== true) return false
  return operation.action === 'request' ? value.status === 'not_requested'
    : value.request?.request_id === operation.request_id && (operation.action === 'import'
      ? value.status === 'staged' && value.stage?.bundle_sha256 === operation.bundle_sha256
      : ['requested', 'staged', 'imported'].includes(value.status))
})
function resumeFromServer(): void {
  if (!canResume.value || !pending.value || !persist(null, storageKey.value, pending.value)) return
  pending.value = null; notice.value = '已按你明确核对的服务端状态解除恢复锁；没有重发、导入或应用。后续操作仍需单独选择。'
}
const statusLabel = computed(() => ({ not_requested: '尚未提交审核请求', requested: '请求已登记，等待授权审核者与操作员', staged: '真实复核证据已暂存，等待显式受信导入', imported: props.job.status === 'applied' ? '受信证据已导入；应用结果见下方服务端记录' : '受信证据已导入，仍需单独应用', cancelled: '审核任务已取消，证据保留', blocked: '当前请求已阻止' })[state.value?.status ?? 'not_requested'])
const message = (value: unknown) => value instanceof ApiRequestError ? userError(value) : value instanceof Error ? value.message : String(value)
function readPending(): Pending | null {
  try {
    const value = JSON.parse(localStorage.getItem(storageKey.value) ?? 'null') as Pending | null
    if (value && ['request', 'import', 'cancel'].includes(value.action) && (value.request_id === null || isJobId(value.request_id))
      && (value.bundle_sha256 === null || isHash(value.bundle_sha256))) return value
  } catch { error.value = '无法读取浏览器恢复记录；请先刷新服务端状态' }
  return null
}
function samePending(a: Pending | null, b: Pending | null): boolean {
  return a === null || b === null ? a === b : a.action === b.action && a.request_id === b.request_id && a.bundle_sha256 === b.bundle_sha256 && a.operation_id === b.operation_id
}
function persist(value: Pending | null, key = storageKey.value, expected?: Pending): boolean {
  try {
    if (expected && !samePending(JSON.parse(localStorage.getItem(key) ?? 'null') as Pending | null, expected)) return false
    if (value) localStorage.setItem(key, JSON.stringify(value)); else localStorage.removeItem(key)
    return true
  } catch { error.value = '浏览器无法保存操作恢复记录；已阻止新提交，请保留任务编号'; return false }
}
function reconcile(value: NativeReviewIngress): void {
  const operation = pending.value
  if (!operation) return
  const terminalApplied = props.fresh && props.job.status === 'applied' && props.job.result?.status === 'APPLIED'
  const resolved = terminalApplied || value.status === 'cancelled' || (props.fresh && props.job.status === 'cancelled') || (operation.action === 'request' ? Boolean(value.request)
    : operation.action === 'import' && value.request?.request_id === operation.request_id && value.stage?.bundle_sha256 === operation.bundle_sha256 && value.status === 'imported')
  if (resolved && persist(null, storageKey.value, operation)) {
    pending.value = null
    if (terminalApplied) notice.value = operation.action === 'cancel'
      ? '服务端已确认任务已应用；先前取消未生效，已解除恢复锁。没有重发取消或重新应用。'
      : '服务端已确认任务已应用，已解除恢复锁；没有重复提交或重新应用。'
  }
}
async function refresh(notifyParent = false): Promise<NativeReviewIngress | null> {
  const g = generation; const key = identity.value; const read = ++readGeneration; const job = props.job
  ingressFresh.value = false; reading.value = true; error.value = ''
  emit('blocked', true)
  try {
    assertJob(job, auth.me?.user_id ?? '', job.package.style_id, job.job_id)
    const value = await request<NativeReviewIngress>(path(ENDPOINT_NATIVE_REVIEW_INGRESS.path), { retries: 0 })
    if (!current(g, key) || read !== readGeneration) return null
    assertReviewIngress(value, job)
    state.value = value; ingressFresh.value = true; reconcile(value)
    if (notifyParent) emit('changed')
    return value
  } catch (e) { if (current(g, key) && read === readGeneration) error.value = message(e); return null }
  finally { if (current(g, key) && read === readGeneration) reading.value = false }
}
async function mutate(action: Action): Promise<void> {
  if (!(action === 'request' ? canRequest.value : action === 'import' ? canImport.value : canCancel.value) || !state.value) return
  const g = generation; const key = identity.value; const storeKey = storageKey.value; const prior = props.job; const expected = ingressVersion(state.value)
  const uncertainty = uncertaintyGeneration
  const preflightCurrent = () => current(g, key) && uncertainty === uncertaintyGeneration && !pending.value
  let sent = false
  let operation: Pending | null = null
  const recoveryUnchanged = () => current(g, key) && uncertainty === uncertaintyGeneration
    && samePending(pending.value, readPending()) && (pending.value === null || samePending(pending.value, operation))
  busy.value = action; emit('busy', true); emit('blocked', true); ingressFresh.value = false; error.value = ''; notice.value = ''
  try {
    // Revalidate both the package owner and the selected opaque ingress record before every write.
    const latest = await request<NativeReviewJob>(path(ENDPOINT_NATIVE_REVIEW_DETAIL.path), { retries: 0 })
    if (!preflightCurrent()) return
    assertJob(latest, auth.me?.user_id ?? '', prior.package.style_id, prior.job_id)
    if (latest.package_sha256 !== prior.package_sha256 || latest.status !== 'pending' || latest.code_version_matches !== true
      || latest.imported_bundle_sha256 !== prior.imported_bundle_sha256) { ingressFresh.value = false; emit('changed'); throw new Error('服务端任务或证据已变化；请核对最新状态后重新选择') }
    const value = await refresh()
    if (!preflightCurrent() || !value) return
    if (ingressVersion(value) !== expected) throw new Error('服务端审核请求或暂存证据已变化；已停止提交，请检查最新状态')
    // A storage event may still be queued: synchronously re-read recovery state before replacing it.
    const observedPending = readPending()
    if (!preflightCurrent() || observedPending) {
      if (observedPending) pending.value = observedPending
      ingressFresh.value = false; emit('blocked', true); notice.value = '检测到其他标签页的待核实操作，已停止本次提交；请刷新核对。'; return
    }
    operation = { action, request_id: value.request?.request_id ?? null, bundle_sha256: value.stage?.bundle_sha256 ?? null, operation_id: newIdempotencyKey() }
    if (!persist(operation, storeKey)) return
    pending.value = operation
    const endpoint = action === 'request' ? ENDPOINT_NATIVE_REVIEW_REQUEST : action === 'import' ? ENDPOINT_NATIVE_REVIEW_IMPORT : ENDPOINT_NATIVE_REVIEW_REQUEST_CANCEL
    const body = action === 'request' ? { package_sha256: prior.package_sha256 } : action === 'import'
      ? { request_id: operation.request_id, bundle_sha256: operation.bundle_sha256 } : { request_id: operation.request_id }
    try {
      sent = true
      await request(path(endpoint.path), { method: 'POST', body, retries: 0, timeoutMs: 60000 })
    } catch (e) {
      // A failed response is never a reason to replay a write. A 4xx is a definitive rejection.
      if (e instanceof ApiRequestError && e.status >= 400 && e.status < 500 && recoveryUnchanged() && persist(null, storeKey, operation)) pending.value = null
      throw e
    }
    if (!recoveryUnchanged()) return
    await refresh(true)
    if (current(g, key)) notice.value = pending.value ? '提交结果尚未核实，请刷新服务端状态；不会自动重发。' : '已重新读取服务端状态。请求和导入都不会自动应用、确认样本或训练模型。'
  } catch (e) {
    if (!current(g, key) || (sent && !recoveryUnchanged())) return
    const failure = message(e)
    if (sent) await refresh(true)
    if (current(g, key)) error.value = failure + (pending.value ? '；结果未明，已停止再次提交，请刷新核对' : '')
  } finally {
    if (current(g, key)) { busy.value = null; emit('busy', false) }
  }
}
function storageChanged(event: StorageEvent): void {
  if (event.key !== storageKey.value && event.key !== null) return
  ++uncertaintyGeneration; ++readGeneration; emit('blocked', true); ingressFresh.value = false; reading.value = false; pending.value = readPending()
  notice.value = '其他标签页的操作记录已变化，请刷新服务端状态后继续。'
  emit('changed')
}
function focusChanged(): void { ingressFresh.value = false; if (!busy.value) void refresh(true) }
watch(permitsApply, (value) => emit('blocked', !value), { immediate: true, flush: 'sync' })
watch(pending, (value) => emit('uncertain', Boolean(value)), { flush: 'sync' })
watch(() => props.job.status, () => { if (!busy.value) void refresh() })
watch(identity, () => {
  ++generation; ++readGeneration; state.value = null; ingressFresh.value = false; reading.value = false; busy.value = null; error.value = ''; notice.value = ''
  emit('busy', false); emit('blocked', true); pending.value = readPending(); void refresh()
}, { immediate: true, flush: 'sync' })
window.addEventListener('storage', storageChanged)
window.addEventListener('focus', focusChanged)
onBeforeUnmount(() => { disposed = true; ++generation; ++readGeneration; emit('busy', false); emit('uncertain', false); emit('blocked', false); window.removeEventListener('storage', storageChanged); window.removeEventListener('focus', focusChanged) })
</script>

<template>
  <section class="space-y-2 rounded border-2 border-ink/30 p-3" data-testid="native-review-ingress">
    <h4 class="text-xs font-black">提交审核与受信证据入口</h4>
    <p v-if="state && ingressFresh && props.fresh" class="text-xs">在线视觉服务尚未配置；提交只登记本任务的审核请求，不会自动启动 自动视觉 处理。请将本任务导出的请求交给本次授权审核者，由授权服务端操作员独立核验并暂存真实复核证据。</p>
    <p v-else class="text-xs text-amber">服务端渠道状态尚未核验；当前无法确认审核请求或可用受信证据。</p>
    <p class="text-xs">网页只能选择服务端已暂存的请求编号和证据校验码，不能填写原始评分、文件路径或审核者身份。导入后仍需单独点击应用。</p>
    <p v-if="error" role="alert" class="break-all text-xs text-danger">{{ error }}</p>
    <p v-if="notice" role="status" class="text-xs">{{ notice }}</p>
    <div v-if="pending" class="space-y-2 text-xs text-amber" data-testid="native-ingress-uncertain"><p>有待核实的 {{ pending.action === 'request' ? '提交审核请求' : pending.action === 'import' ? '导入证据' : '取消审核' }} 操作，已停止再次提交。恢复或刷新只读取服务端状态，不会自动重发。</p><p>若服务端状态仍未变化，可明确核对后解除恢复锁；这不会重发请求，之后仍需单独选择操作。</p><button type="button" class="btn-hard min-h-11" :disabled="!canResume" @click="resumeFromServer">确认按当前服务端状态继续（不重发）</button></div>
    <p v-if="state" class="text-xs font-bold" data-testid="native-ingress-status">{{ statusLabel }}{{ !ingressFresh || !props.fresh ? ' · 尚未重新核验' : '' }}</p>
    <p v-else class="text-xs text-amber">尚未读取服务端审核请求；当前不能确认是否已提交或暂存。</p>
    <p v-if="state?.reason" class="break-all text-xs text-amber" data-testid="native-ingress-reason">{{ userError(state.reason) }}</p>
    <details v-if="state?.request" class="break-all text-xs" data-testid="native-ingress-request"><summary>高级审核请求信息</summary>
      <p>请求编号 {{ state.request.request_id }} · 创建时间 {{ state.request.created_at }}</p>
      <p>请求 SHA256 {{ state.request.request_sha256 }} · 任务包 {{ state.package_sha256 }}</p>
    </details>
    <details v-if="state?.stage" class="break-all text-xs" data-testid="native-ingress-stage"><summary>高级复核证据信息</summary>
      <p>服务端暂存证据校验码 {{ state.stage.bundle_sha256 }}</p>
      <p>审核者 {{ state.stage.reviewer.provider }} / {{ state.stage.reviewer.model_id }} / {{ state.stage.reviewer.reviewer_kind }}</p>
      <p>原始复核 SHA256 {{ state.stage.source_review_sha256 }}</p>
      <p>复核时间 {{ state.stage.reviewed_at }} · 暂存时间 {{ state.stage.staged_at }}</p>
      <p>暂存和导入都不是样本验收，也不是人工确认</p>
    </details>
    <div class="flex flex-wrap gap-2">
      <button type="button" class="btn-hard min-h-11" :disabled="!canRequest" @click="mutate('request')">提交本任务审核请求</button>
      <button type="button" class="btn-hard min-h-11" :disabled="!available" @click="refresh(true)">刷新审核请求与暂存证据</button>
      <button type="button" class="btn-hard min-h-11" :disabled="!canImport" @click="mutate('import')">导入这份服务端受信证据</button>
      <button type="button" class="btn-hard min-h-11" :disabled="!canCancel" @click="mutate('cancel')">取消请求及整个审核任务（保留证据）</button>
    </div>
    <p v-if="state?.status === 'imported' && job.status !== 'applied'" class="text-xs">证据导入完成；尚不能据此宣称样本已接受。请在下方检查并显式应用服务端证据。</p>
  </section>
</template>
