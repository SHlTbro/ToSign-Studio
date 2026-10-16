<script setup lang="ts">
import { userError } from '@/utils/userLanguage'
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { ApiRequestError, request } from '@/api/http'
import { useAuthStore } from '@/stores/auth'
import { isRegistrationId } from '@/api/m2Runtime'
import { assertPreparation, preparationPath, type ScrPreparation } from '@/api/userScr'
const props = defineProps<{ styleId: number; registrationId: string; snapshotSha: string; blockers: { code: string; detail: string }[]; runtimeError: string | null; disabled: boolean }>()
const emit = defineEmits<{ prepared: [value: ScrPreparation]; busy: [value: boolean]; problem: [string] }>()
const auth = useAuthStore()
const scope = computed(() => `${auth.me?.user_id ?? ''}:${props.styleId}:${props.registrationId}:${props.snapshotSha}`)
const storageKey = () => `yy:p7:scr-preparation:${scope.value}`
const job = ref<string | null>(null)
const result = ref<ScrPreparation | null>(null)
const error = ref('')
watch(error, value => emit('problem', value), { immediate: true })
const busy = ref(false)
const uncertain = ref(false)
const recoveryJob = ref('')
const paused = ref(false)
const recordedPreparation = ref<string | null>(null)
let generation = 0
let disposed = false
let timer: number | null = null
const allowed = computed(() => Boolean(auth.me?.user_id && isRegistrationId(props.registrationId) && /^[0-9a-f]{64}$/.test(props.snapshotSha)
  && !props.runtimeError && props.blockers.every(row => ['PREPARATION_REQUIRED', 'STALE_CACHE'].includes(row.code))))
const canPrepare = computed(() => allowed.value && !props.disabled && !busy.value && !job.value && !uncertain.value && !result.value && !recordedPreparation.value)
function stop() { if (timer !== null) window.clearTimeout(timer); timer = null }
function schedule() { stop(); if (!disposed && job.value && !paused.value) timer = window.setTimeout(() => { void poll() }, 2200) }
function remember() { localStorage.setItem(storageKey(), JSON.stringify({ job_id: job.value, uncertain: uncertain.value })) }
function adopt(value: unknown) {
  assertPreparation(value, props.styleId, props.snapshotSha)
  const previousJob = job.value
  result.value = value.status === 'PREPARED' ? value : null
  job.value = value.status === 'PREPARING' ? value.job_id! : null
  uncertain.value = false
  if (result.value) {
    stop()
    // Keep the server job pointer for another verified GET on re-entry. An
    // immediate result without a job is only a local pointer, never a new proof.
    localStorage.setItem(storageKey(), JSON.stringify(previousJob ? { job_id: previousJob, uncertain: false }
      : { preparation_id: result.value.preparation_id, uncertain: false }))
    emit('prepared', result.value)
  }
  else { remember(); schedule() }
}
function describe(value: unknown) { return value instanceof ApiRequestError ? userError(value) : value instanceof Error ? value.message : String(value) }
async function poll() {
  if (!job.value || busy.value || disposed) return
  const token = generation; const id = job.value
  busy.value = true
  try {
    const value = await request<unknown>(preparationPath(props.styleId, id), { retries: 0, timeoutMs: 600000 })
    if (disposed || token !== generation) return
    adopt(value); error.value = ''
  } catch (reason) { if (!disposed && token === generation) { error.value = describe(reason); paused.value = true; stop() } }
  finally { if (!disposed && token === generation) busy.value = false }
}
async function prepare() {
  if (!canPrepare.value) return
  const token = generation
  busy.value = true; emit('busy', true); error.value = ''; paused.value = false
  // Persist before POST: there is no documented idempotency key for this endpoint.
  uncertain.value = true
  try {
    remember()
    const value = await request<unknown>(preparationPath(props.styleId), { method: 'POST', retries: 0, timeoutMs: 600000,
      body: { snapshot_sha256: props.snapshotSha, runtime_registration_id: props.registrationId } })
    if (disposed || token !== generation) return
    adopt(value)
  } catch (reason) {
    if (disposed || token !== generation) return
    if (reason instanceof ApiRequestError && reason.status >= 400 && reason.status < 500) { uncertain.value = false; localStorage.removeItem(storageKey()) }
    error.value = describe(reason)
  } finally { if (!disposed && token === generation) { busy.value = false; emit('busy', false) } }
}
function pause() { paused.value = true; stop() }
function resume() { paused.value = false; void poll() }
function recover() {
  if (!uncertain.value || busy.value || !allowed.value || !/^[0-9a-f]{64}$/.test(recoveryJob.value.trim())) return
  job.value = recoveryJob.value.trim(); paused.value = false
  remember(); void poll()
}
watch(scope, () => {
  ++generation; stop()
  if (busy.value) emit('busy', false)
  busy.value = false; job.value = null; result.value = null; error.value = ''; uncertain.value = false; paused.value = false; recordedPreparation.value = null
  try {
    const saved = JSON.parse(localStorage.getItem(storageKey()) ?? 'null')
    if (saved && typeof saved.job_id === 'string' && saved.job_id.length > 0 && saved.job_id.length <= 200) { job.value = saved.job_id; void poll() }
    else if (typeof saved?.preparation_id === 'string' && saved.preparation_id.length > 0) recordedPreparation.value = saved.preparation_id
    else if (saved?.uncertain === true) uncertain.value = true
  } catch { error.value = '本机准备指针无法读取；不会自动重复准备' }
}, { immediate: true, flush: 'sync' })
onBeforeUnmount(() => { disposed = true; ++generation; stop(); if (busy.value) emit('busy', false) })
</script>
<template>
  <section class="mt-3 rounded-lg border border-ink/20 p-3 text-xs" data-testid="user-scr-preparation">
    <h4 class="font-black">个人快照 SCR 准备</h4>
    <p class="mt-1">先冻结 S03，再在合格注册环境准备训练缓存。缓存完成不表示获准启动或质量通过。</p>
    <p v-if="!registrationId">请选择明确的注册环境；此配方不能回退到默认 CPU。</p>
    <p v-if="!snapshotSha">请先冻结或复用已审核的 S03 快照。</p>
    <button type="button" class="btn-hard mt-2 min-h-11" :disabled="!canPrepare" @click="prepare">{{ busy && !job ? '准备请求中…' : '准备个人快照 SCR' }}</button>
    <div v-if="job" role="status" class="mt-2 break-all">
      <p>服务端准备作业：{{ job }} · {{ paused ? '进度读取已暂停' : '等待服务端结果' }}</p>
      <button type="button" class="min-h-11 underline" :disabled="busy" @click="resume">刷新准备进度</button>
      <button type="button" class="ml-3 min-h-11 underline" :disabled="paused" @click="pause">暂停进度读取</button>
      <p>暂停仅停止本页轮询；服务端没有已确认的准备取消接口，作业可能继续。</p>
    </div>
    <p v-if="uncertain && !job" class="mt-2 text-amber" role="alert">准备请求结果未确认，未取得作业标识；刷新页面不会自动重发。请重新检查服务端准备记录。</p>
    <div v-if="uncertain && !job" class="mt-2">
      <label>已有准备作业标识<input v-model="recoveryJob" aria-label="服务端准备作业标识" class="mt-1 w-full rounded border p-2" :disabled="busy" /></label>
      <button type="button" class="btn-hard mt-2 min-h-11" :disabled="busy || !allowed || !/^[0-9a-f]{64}$/.test(recoveryJob.trim())" @click="recover">读取已有准备作业</button>
      <p>只读取本账户作业并核验当前快照；不会重新提交准备请求。</p>
    </div>
    <p v-if="recordedPreparation" class="mt-2 break-all text-amber">已保留上次准备标识 {{ recordedPreparation }}；当前缓存状态需重新检查服务端环境，不能用本机记录放行或重复准备。</p>
    <p v-if="result" class="mt-2 break-all" role="status">缓存已准备：{{ result.preparation_id }} · S03 {{ result.snapshot_sha256 }}；启动与发布仍受服务端门控。</p>
    <p v-if="error" class="mt-2 text-danger" role="alert">{{ error }}</p>
  </section>
</template>
