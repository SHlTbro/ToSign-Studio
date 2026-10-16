<script setup lang="ts">
import { userError, stateLabel } from '@/utils/userLanguage'
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { ApiRequestError, newIdempotencyKey } from '@/api/http'
import { useAuthStore } from '@/stores/auth'
import { continueOwnedRun, parsePendingContinuation, readRecovery, recoveryStorageKey, type PendingContinuation, type RunRecovery } from '@/api/runRecovery'

const props = defineProps<{ runId: number; status: string; disabled?: boolean }>()
const emit = defineEmits<{ continued: [runId: number]; busy: [value: boolean]; problem: [string] }>()
const auth = useAuthStore()
const recovery = ref<RunRecovery | null>(null)
const pending = ref<PendingContinuation | null>(null)
const checkpointId = ref<number | null>(null)
const loading = ref(false)
const busy = ref(false)
const error = ref('')
watch(error, value => emit('problem', value), { immediate: true })
const storageFailed = ref(false)
let generation = 0
let disposed = false
const verified = computed(() => recovery.value?.checkpoints.filter(item => item.verified) ?? [])
const gateMessage = computed(() => {
  const code = recovery.value?.resume_gate ?? ''
  if (code === 'FULL_TRAINER_RESUME_QUALIFICATION_REQUIRED') return '当前环境尚无真实全状态恢复资格，保留 checkpoint 等待独立验收。'
  if (code === 'NO_VERIFIED_CHECKPOINT') return '尚无可校验的已提交 checkpoint。'
  if (code === 'READY') return '已校验当前 checkpoint 与独立恢复资格。'
  return code
})
function storageKey(): string {
  const userId = auth.me?.user_id
  if (!userId) throw new Error('登录状态不可用，已阻止恢复操作')
  return recoveryStorageKey(userId, props.runId)
}
async function refresh(): Promise<void> {
  const current = ++generation
  const id = props.runId
  recovery.value = null; loading.value = true; error.value = ''
  try {
    const result = await readRecovery(id)
    if (disposed || current !== generation) return
    if (result.run_id !== id) throw new Error('恢复状态的运行标识不一致')
    if (!Array.isArray(result.checkpoints) || typeof result.retry_allowed !== 'boolean'
      || typeof result.resume_allowed !== 'boolean' || typeof result.resume_gate !== 'string'
      || result.claims_quality_pass !== false || result.checkpoints.some(item => !item
        || !Number.isSafeInteger(item.checkpoint_id) || item.checkpoint_id < 1 || typeof item.verified !== 'boolean')) {
      throw new Error('恢复状态响应不完整，请重新读取服务端状态；当前不能继续训练')
    }
    recovery.value = result
    if (!result.checkpoints.some(item => item.verified && item.checkpoint_id === checkpointId.value)) {
      checkpointId.value = result.checkpoints.find(item => item.verified)?.checkpoint_id ?? null
    }
  } catch (cause) {
    if (!disposed && current === generation) error.value = cause instanceof ApiRequestError ? userError(cause) : String(cause)
  } finally { if (!disposed && current === generation) loading.value = false }
}
watch(() => [props.runId, props.status, auth.me?.user_id], () => {
  pending.value = null; checkpointId.value = null; storageFailed.value = false
  try { pending.value = parsePendingContinuation(localStorage.getItem(storageKey()), props.runId) }
  catch (cause) { storageFailed.value = true; error.value = String(cause) }
  void refresh()
}, { immediate: true })
async function submit(mode: 'retry' | 'resume'): Promise<void> {
  if (busy.value || loading.value || props.disabled || storageFailed.value || !recovery.value || recovery.value.continuation_run_id) return
  if (!pending.value && (mode === 'retry' ? !recovery.value.retry_allowed : !recovery.value.resume_allowed || !checkpointId.value)) return
  const current = generation
  const submittedRunId = props.runId
  const submittedUserId = auth.me?.user_id
  busy.value = true; emit('busy', true); error.value = ''
  let submitted: PendingContinuation | null = null
  try {
    const key = storageKey()
    if (!pending.value) {
      submitted = { version: 1, parent_run_id: props.runId, key: newIdempotencyKey(), mode,
        ...(mode === 'resume' ? { checkpoint_id: checkpointId.value! } : {}) }
      // Persist before POST. Failure here means no request was sent.
      try { localStorage.setItem(key, JSON.stringify(submitted)) }
      catch { storageFailed.value = true; throw new Error('无法保存幂等恢复请求，尚未发送提交') }
      pending.value = submitted
    } else submitted = pending.value
    const child = await continueOwnedRun(submitted)
    // Remove only this operation's record; another tab may own a new record.
    const stored = parsePendingContinuation(localStorage.getItem(key), submitted.parent_run_id)
    if (stored?.key === submitted.key) localStorage.removeItem(key)
    if (disposed || current !== generation) return
    pending.value = null
    emit('continued', child.run_id) // new attempt remains created, never auto-started
  } catch (cause) {
    if (disposed || current !== generation) return
    const message = cause instanceof ApiRequestError ? userError(cause) : String(cause)
    if (submitted && cause instanceof ApiRequestError && cause.status >= 400 && cause.status < 500
      && !['CONTINUATION_PENDING', 'CONTINUATION_EXISTS'].includes(cause.code)) {
      try {
        const key = storageKey()
        if (parsePendingContinuation(localStorage.getItem(key), submitted.parent_run_id)?.key === submitted.key) localStorage.removeItem(key)
        pending.value = null
      } catch { storageFailed.value = true }
    }
    // Preserve every uncertain operation and its exact key/body for explicit
    // reconciliation; refresh never automatically replays a POST.
    await refresh()
    if (!disposed && props.runId === submittedRunId && auth.me?.user_id === submittedUserId) error.value = message
  } finally { busy.value = false; if (!disposed) emit('busy', false) }
}
onBeforeUnmount(() => { disposed = true; generation += 1; if (busy.value) emit('busy', false) })
</script>

<template>
  <section class="mt-3 space-y-2 rounded-xl border border-ink/30 p-3 text-xs" aria-label="训练运行恢复">
    <div class="flex items-center justify-between"><h3 class="font-black">保留 checkpoint 与创建后续运行</h3><button type="button" :disabled="loading || busy" class="underline" @click="refresh">重新读取恢复状态</button></div>
    <p v-if="loading" role="status">正在读取真实执行归属与 checkpoint…</p>
    <template v-else-if="recovery">
      <p>执行状态：{{ stateLabel(recovery.execution_state) }}</p>
      <p>{{ gateMessage }}</p>
      <p>后续运行使用新目录；旧运行、checkpoint 和质量门继续保留。恢复资格不表示模型质量或发布通过。</p>
      <button v-if="recovery.continuation_run_id" type="button" class="btn-hard" :disabled="busy || disabled" @click="emit('continued', recovery.continuation_run_id!)">查看已创建的运行 #{{ recovery.continuation_run_id }}</button>
      <template v-else>
        <label v-if="verified.length" class="block">已校验 checkpoint<select v-model="checkpointId" class="ml-2 rounded border border-ink px-2 py-1" :disabled="busy || !!pending || disabled"><option v-for="item in verified" :key="item.checkpoint_id" :value="item.checkpoint_id">#{{ item.checkpoint_id }} · step {{ item.position.global_step }} · 下一个 batch {{ item.position.batch_ptr.batch_idx }}</option></select></label>
        <p v-for="item in recovery.checkpoints.filter(item => !item.verified)" :key="item.checkpoint_id" class="text-danger">checkpoint #{{ item.checkpoint_id }} 未通过：{{ item.failure_code }}</p>
        <p v-if="pending" role="status" class="text-amber">上次请求结果尚待确认：{{ pending.mode === 'resume' ? `从 checkpoint #${pending.checkpoint_id} 恢复` : '重新训练' }}。刷新只读取状态；再次提交使用原请求与同一 key。</p>
        <button v-if="pending" type="button" class="btn-hard" :disabled="busy || loading || disabled || storageFailed" @click="submit(pending.mode)">使用原请求确认后续运行</button>
        <div v-else class="flex flex-wrap gap-2">
          <button type="button" class="btn-hard" :disabled="busy || loading || disabled || storageFailed || !recovery.retry_allowed" @click="submit('retry')">创建新运行重新训练</button>
          <button type="button" class="btn-hard" :disabled="busy || loading || disabled || storageFailed || !recovery.resume_allowed || !checkpointId" @click="submit('resume')">从 checkpoint 创建恢复运行</button>
        </div>
      </template>
    </template>
    <p v-if="storageFailed" role="alert" class="text-danger">无法可靠保存或读取当前账号的恢复请求，已阻止提交。</p>
    <p v-if="error" role="alert" class="text-danger">{{ error }}</p>
  </section>
</template>
