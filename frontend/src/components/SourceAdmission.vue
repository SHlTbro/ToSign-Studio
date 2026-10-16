<script setup lang="ts">
import { stateLabel, userText } from '@/utils/userLanguage'
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { request } from '@/api/http'
interface Admission { asset_id: string; style_id: number; source_sha256: string; source_status: string; history_status: string; status: string; blocker?: string; receipt?: { actor_kind: string; actor: string; receipt_sha256: string } | null }
const props = defineProps<{ assetId: string; styleId: number; locked?: boolean }>()
const state = ref<Admission | null>(null)
const error = ref(''); const loading = ref(false); const working = ref(false)
const rights = ref(false); const training = ref(false); const unknown = ref(false); const reason = ref('')
let generation = 0; let disposed = false
const disabled = computed(() => props.locked || loading.value || working.value)
const canSubmit = computed(() => !disabled.value && !error.value && state.value?.source_status === 'approved'
  && state.value.status !== 'ADMITTED' && /^[0-9a-f]{64}$/.test(state.value.source_sha256) && rights.value && training.value && unknown.value && reason.value.trim().length > 0 && reason.value.length <= 1000)
function cancel() { if (!working.value) { rights.value = false; training.value = false; unknown.value = false; reason.value = '' } }
async function refresh() {
  if (working.value || disposed) return
  const current = ++generation; const id = props.assetId; const styleId = props.styleId
  loading.value = true; error.value = ''; state.value = null; cancel()
  try {
    const value = await request<Admission>(`/api/v1/training-sources/${encodeURIComponent(id)}/admission?style_id=${styleId}`, { retries: 0 })
    if (disposed || current !== generation) return
    if (value.asset_id !== id || value.style_id !== styleId || !/^[0-9a-f]{64}$/.test(value.source_sha256)) throw new Error('来源版本响应不完整')
    state.value = value
  } catch (e) { if (!disposed && current === generation) error.value = userText(String(e)) }
  finally { if (!disposed && current === generation) loading.value = false }
}
async function submit() {
  if (!canSubmit.value || !state.value) return
  const id = props.assetId; const styleId = props.styleId; const sourceHash = state.value.source_sha256
  working.value = true
  try {
    await request(`/api/v1/training-sources/${encodeURIComponent(id)}/admission`, { method: 'POST', retries: 0, body: {
      style_id: styleId, source_sha256: sourceHash, origin: 'user_supplied_attachment', history_status: 'UNKNOWN', allow_unknown_history: true,
      actor_kind: 'authenticated_user', rights_confirmed: true, personal_training_confirmed: true, reason: reason.value,
    } })
  } catch (e) { if (!disposed && id === props.assetId && styleId === props.styleId) error.value = `${userText(String(e))}；请刷新核对记录，再决定是否重新操作` }
  finally { working.value = false }
  if (!disposed && (!error.value || id !== props.assetId || styleId !== props.styleId)) await refresh()
}
watch(() => [props.assetId, props.styleId], () => { ++generation; state.value = null; rights.value = false; training.value = false; unknown.value = false; reason.value = ''; error.value = ''; void refresh() }, { immediate: true })
onBeforeUnmount(() => { disposed = true; ++generation })
</script>
<template>
  <section class="rounded-xl border-2 border-ink p-3 text-xs" aria-labelledby="source-use-heading">
    <div class="flex flex-wrap items-center justify-between gap-2"><h2 id="source-use-heading" class="font-black">本张原图的训练使用声明</h2><button class="btn-hard btn-hard--compact" type="button" :disabled="disabled" @click="refresh">刷新来源声明</button></div>
    <p class="mt-2">管理员批准原图后，本人还须明确声明本张图用于所选档案 的个人训练。既往使用历史未知；台账禁用和相关来源组限制不能豁免。此操作不确认字样，也不批准模型质量。</p>
    <p v-if="loading" role="status" class="mt-2">正在核对当前来源…</p><p v-if="error" role="alert" class="mt-2 break-words">{{ error }}</p>
    <template v-if="state">
      <details class="mt-2 break-all"><summary>高级信息</summary><p>档案标识：{{ styleId }} · 原图标识：{{ assetId }}</p><p>文件校验码：{{ state.source_sha256 }}</p><p v-if="state.receipt">声明者：{{ state.receipt.actor_kind }} · {{ state.receipt.actor }} · 收据校验码：{{ state.receipt.receipt_sha256 }}</p></details>
      <p class="mt-2 break-all">原图 {{ stateLabel(state.source_status) }} · {{ state.history_status === 'UNKNOWN' ? '使用历史未知' : stateLabel(state.history_status) }}</p>
      <p v-if="state.status === 'ADMITTED' && state.receipt" role="status" class="mt-2 break-all">已有来源声明；仍需单独确认字样</p>
      <template v-else>
        <p v-if="state.blocker" class="mt-2 break-words">{{ userText(state.blocker) }}</p>
        <fieldset class="mt-2 space-y-2" :disabled="disabled || state.source_status !== 'approved'">
          <label class="flex min-h-11 items-center gap-2"><input v-model="rights" type="checkbox" aria-label="本人确认本张原图使用权">我有权上传并使用本张原图进行个人风格训练。</label>
          <label class="flex min-h-11 items-center gap-2"><input v-model="training" type="checkbox" aria-label="本人同意当前档案个人训练用途">我同意将本张原图及提取字形用于当前档案的个人训练与验证。</label>
          <label class="flex min-h-11 items-center gap-2"><input v-model="unknown" type="checkbox" aria-label="确认来源使用历史未知">我理解使用历史未知，仅声明本次用途；不声称从未用于训练。</label>
          <label class="block">本次来源与用途说明<textarea v-model="reason" maxlength="1000" aria-label="来源与用途说明" class="mt-1 min-h-20 w-full rounded-lg border border-ink bg-canvas p-2" /></label>
        </fieldset>
        <div class="mt-2 flex flex-wrap gap-2"><button type="button" class="btn-hard" :disabled="disabled" @click="cancel">取消来源声明</button><button type="button" class="btn-hard" :disabled="!canSubmit" @click="submit">{{ working ? '保存来源声明中…' : '本人提交本张来源声明' }}</button></div>
      </template>
    </template>
  </section>
</template>
