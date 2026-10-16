<script setup lang="ts">
import { userError, stateLabel } from '@/utils/userLanguage'
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { ApiRequestError, request } from '@/api/http'
import HumanDatasetReview from '@/components/HumanDatasetReview.vue'

interface Family { component_id: string; parent_hashes: string[]; source_asset_ids: string[]; family_ids: string[] }
interface Preflight {
  target_style_id: number; status: string; base_intake_sha256: string | null
  eligible_parent_sources: { source_asset_id: string; source_asset_sha256: string }[]
  family_components: Family[]; explicit_family_split_required: boolean
  eligible_confirmed_sample_count?: number; eligible_confirmed_characters?: string[]
  m2_missing_unique_char_count?: number; blockers: string[]
}
interface SnapshotResult {
  status: string; snapshot_ready: boolean; snapshot_created: boolean; snapshot_reused?: boolean
  dataset_snapshot_sha256?: string; snapshot_id?: string; blockers?: string[]
}
export interface M2Preparation { config: Record<string, unknown>; sample_ids: number[] }
interface Runtime extends M2Preparation {
  status: string; ready: boolean; blockers: { code: string; detail: string }[]
  snapshot?: { snapshot_id: string; snapshot_sha256: string; sample_count: number; distinct_labels: number } | null
  runtime_options?: { registration_id: string; variant_id?: string; device?: string; ready: boolean; reason?: string }[]
  capabilities?: { create_m2_run: boolean }; provider_mode?: string
}
const props = defineProps<{ styleId: number; locked?: boolean; reviewKey?: string }>()
const emit = defineEmits<{ busy: [boolean]; createM2: [M2Preparation] }>()
const preflight = ref<Preflight | null>(null)
const runtime = ref<Runtime | null>(null)
const snapshot = ref<SnapshotResult | null>(null)
const readError = ref<string | null>(null)
const actionError = ref<string | null>(null)
const loading = ref(false)
const working = ref(false)
const humanWorking = ref(false)
const selectedRuntime = ref('')
const sides = ref<Record<string, '' | 'train' | 'val'>>({})
const acknowledged = ref(false)
const freezePlan = ref<{ styleId: number; intakeHash: string; parentAssignments: Record<string, 'train' | 'val'> } | null>(null)
let generation = 0
let disposed = false
const hashValid = (value: unknown) => typeof value === 'string' && /^[0-9a-f]{64}$/.test(value)
const disabled = computed(() => props.locked || working.value || humanWorking.value || loading.value)
const assignments = computed(() => {
  const value: Record<string, 'train' | 'val'> = {}
  const expected = new Set(preflight.value?.eligible_parent_sources.map(source => source.source_asset_sha256) ?? [])
  for (const group of preflight.value?.family_components ?? []) {
    const side = sides.value[group.component_id]
    if (!side) return null
    for (const parent of group.parent_hashes) if (expected.has(parent)) value[parent] = side
  }
  return Object.keys(value).length === expected.size && new Set(Object.values(value)).size === 2 ? value : null
})
const canFreeze = computed(() => !disabled.value && !readError.value && Boolean(preflight.value)
  && hashValid(preflight.value?.base_intake_sha256) && (preflight.value?.m2_missing_unique_char_count ?? 0) === 0
  && Boolean(assignments.value))
function isReady(value: Runtime | null): value is Runtime {
  return Boolean(value && value.status === 'READY' && value.ready === true && value.capabilities?.create_m2_run === true
    && value.config?.engine === 'm2' && Number(value.config.style_id) === props.styleId
    && hashValid(value.config.snapshot_sha256) && value.snapshot?.snapshot_sha256 === value.config.snapshot_sha256
    && value.sample_ids?.length && value.sample_ids.every(id => Number.isInteger(id) && id > 0))
}
const canCreateM2 = computed(() => !disabled.value && freezePlan.value === null && isReady(runtime.value))
function errorMessage(error: unknown) { return error instanceof ApiRequestError ? userError(error) : error instanceof Error ? error.message : String(error) }
function runtimePath(styleId = props.styleId) {
  const query = selectedRuntime.value ? `?runtime_registration_id=${encodeURIComponent(selectedRuntime.value)}` : ''
  return `/api/v1/styles/${styleId}/m2-runtime${query}`
}
async function refresh() {
  if (working.value || disposed) return
  const currentGeneration = ++generation; const styleId = props.styleId
  loading.value = true; readError.value = null; runtime.value = null; preflight.value = null
  sides.value = {}; freezePlan.value = null; acknowledged.value = false
  const current = () => !disposed && currentGeneration === generation && styleId === props.styleId
  try {
    const [p, r] = await Promise.all([
      request<Preflight>(`/api/v1/training-samples/snapshot-preflight?style_id=${styleId}`, { retries: 0, timeoutMs: 120000 }),
      request<Runtime>(runtimePath(styleId), { retries: 0, timeoutMs: 120000 }),
    ])
    if (!current()) return
    if (p?.target_style_id !== styleId || !Array.isArray(p.blockers) || !Array.isArray(p.family_components)
      || !Array.isArray(p.eligible_parent_sources) || !r || !Array.isArray(r.blockers)) throw new Error('服务端预检响应缺少来源、分组或能力字段')
    preflight.value = p; runtime.value = r
  } catch (error) { if (current()) readError.value = errorMessage(error) }
  finally { if (current()) loading.value = false }
}
function previewFreeze() {
  if (!canFreeze.value || !preflight.value || !hashValid(preflight.value.base_intake_sha256)) return
  freezePlan.value = { styleId: props.styleId, intakeHash: preflight.value.base_intake_sha256!, parentAssignments: { ...(assignments.value ?? {}) } }
  actionError.value = null; acknowledged.value = false
}
function cancelFreeze() { if (!working.value) { freezePlan.value = null; acknowledged.value = false } }
async function freeze() {
  const plan = freezePlan.value
  if (!plan || plan.styleId !== props.styleId || !acknowledged.value || disabled.value || actionError.value) return
  working.value = true; emit('busy', true); actionError.value = null
  try {
    const body = Object.keys(plan.parentAssignments).length
      ? { style_id: plan.styleId, expected_base_intake_sha256: plan.intakeHash, parent_assignments: plan.parentAssignments }
      : { style_id: plan.styleId }
    const result = await request<SnapshotResult>('/api/v1/training-samples/prepare-snapshot', { method: 'POST', body, retries: 0, timeoutMs: 120000 })
    if (disposed || plan.styleId !== props.styleId) return
    if (!result || typeof result.snapshot_ready !== 'boolean' || (result.snapshot_ready && (result.status !== 'READY' || !hashValid(result.dataset_snapshot_sha256)))) throw new Error('冻结响应不完整；请刷新核对服务端记录')
    snapshot.value = result; freezePlan.value = null; acknowledged.value = false
    runtime.value = null
    runtime.value = await request<Runtime>(runtimePath(plan.styleId), { retries: 0, timeoutMs: 120000 })
  } catch (error) { if (!disposed) actionError.value = `${errorMessage(error)}；先取消预览并刷新核对，禁止盲目重复提交` }
  finally { working.value = false; emit('busy', false) }
}
async function createM2() {
  if (!canCreateM2.value) return
  working.value = true; emit('busy', true); actionError.value = null
  const styleId = props.styleId
  try {
    const verified = await request<Runtime>(runtimePath(styleId), { retries: 0, timeoutMs: 120000 })
    if (disposed || styleId !== props.styleId) return
    runtime.value = verified
    if (!isReady(verified)) throw new Error('M2 能力或快照已变化，请核对当前阻断原因')
    emit('createM2', { config: structuredClone(verified.config), sample_ids: [...verified.sample_ids] })
  } catch (error) { if (!disposed) actionError.value = errorMessage(error) }
  finally { working.value = false; emit('busy', false) }
}
watch(() => [props.styleId, props.reviewKey], () => { snapshot.value = null; actionError.value = null; selectedRuntime.value = ''; void refresh() }, { immediate: true })
watch(selectedRuntime, () => { void refresh() })
onBeforeUnmount(() => { disposed = true; ++generation })
</script>

<template>
  <section class="rounded-2xl border-2 border-ink bg-surface p-4" aria-labelledby="s03-heading">
    <div class="flex flex-wrap items-center justify-between gap-2"><h2 id="s03-heading" class="text-sm font-black">S03 冻结训练集与真实 M2</h2><button class="btn-hard btn-hard--compact" type="button" :disabled="disabled" @click="refresh">刷新冻结预检</button></div>
    <p class="mt-2 text-xs">只读取已审核候选和服务端来源链。冻结不启动训练；来源、划分、视觉审计或模型不满足时，服务端会阻断。人工审核与 AI 证据分别记录。</p>
    <HumanDatasetReview :style-id="styleId" :review-key="reviewKey" :locked="props.locked || working || loading || freezePlan !== null" @busy="humanWorking = $event; emit('busy', $event)" @reviewed="refresh" />
    <p v-if="loading" role="status" class="mt-3 text-xs">正在核对来源、快照与已注册模型…</p>
    <p v-if="readError" role="alert" class="mt-3 break-words rounded-lg bg-blush p-3 text-xs">预检读取失败：{{ readError }}</p>
    <template v-if="preflight">
      <p class="mt-3 text-xs font-bold">已审核可用样本 {{ preflight.eligible_confirmed_sample_count ?? '—' }} · 不同字符 {{ preflight.eligible_confirmed_characters?.length ?? '—' }} · M2 还缺 {{ preflight.m2_missing_unique_char_count ?? '—' }} 字</p>
      <p class="mt-1 break-all text-[10px]">当前 intake SHA256：{{ preflight.base_intake_sha256 ?? '尚不能验证' }}</p>
      <ul v-if="preflight.blockers.length" role="status" class="mt-2 space-y-1 break-words rounded-lg bg-amber/10 p-3 text-xs"><li v-for="reason in preflight.blockers" :key="reason">{{ reason }}</li></ul>
      <fieldset v-if="preflight.family_components.length" class="mt-3 space-y-2" :disabled="disabled || freezePlan !== null">
        <legend class="text-xs font-black">按完整来源组选择训练 / 验证集</legend>
        <p class="text-xs">同一原图及相关来源组必须同侧。不会自动选择，提交后再次校验当前 intake 哈希。</p>
        <label v-for="family in preflight.family_components" :key="family.component_id" class="block rounded-lg border border-ink/20 p-2 text-xs">
          <span class="break-all">来源组 {{ family.component_id }} · {{ family.source_asset_ids.length }} 张原图</span>
          <select v-model="sides[family.component_id]" :aria-label="`划分来源组 ${family.component_id}`" class="mt-1 min-h-11 w-full rounded-lg border border-ink bg-canvas p-2"><option value="">请选择</option><option value="train">训练 train</option><option value="val">验证 validation</option></select>
          <span class="mt-1 block break-all text-[10px]">{{ family.parent_hashes.join(' · ') }}</span>
        </label>
      </fieldset>
      <button type="button" class="btn-hard mt-3 min-h-11" :disabled="!canFreeze || freezePlan !== null" @click="previewFreeze">预览并冻结 S03</button>
    </template>
    <div v-if="freezePlan" class="mt-3 rounded-xl border-2 border-amber p-3" aria-labelledby="s03-confirm-heading">
      <h3 id="s03-confirm-heading" class="text-sm font-black">确认当前来源划分与冻结操作</h3>
      <p class="mt-2 break-all text-xs">档案 #{{ freezePlan.styleId }} · intake {{ freezePlan.intakeHash }}</p>
      <p class="mt-1 text-xs">train {{ Object.values(freezePlan.parentAssignments).filter(side => side === 'train').length }} 张原图 · validation {{ Object.values(freezePlan.parentAssignments).filter(side => side === 'val').length }} 张原图。后端按来源组核对全部候选，不覆盖旧快照，不代替字符审核。</p>
      <label class="mt-3 flex items-start gap-2 text-xs"><input v-model="acknowledged" type="checkbox" aria-label="确认当前来源分组与冻结操作" :disabled="working"><span>我已核对上述来源分组与当前 intake，提交冻结预检；冻结成功与否以服务端核验结果为准。</span></label>
      <div class="mt-3 flex flex-wrap gap-2"><button class="btn-hard min-h-11" type="button" :disabled="working" @click="cancelFreeze">取消冻结预览</button><button class="btn-hard min-h-11" type="button" :disabled="!acknowledged || disabled || Boolean(actionError)" @click="freeze">{{ working ? '服务端核验中…' : '提交冻结请求' }}</button></div>
    </div>
    <p v-if="actionError" role="alert" class="mt-3 break-words rounded-lg bg-blush p-3 text-xs">{{ actionError }}</p>
    <div v-if="snapshot" role="status" class="mt-3 rounded-lg border border-ink/20 p-3 text-xs"><p>{{ snapshot.snapshot_ready ? '服务端已验证冻结快照' : '冻结被阻断' }} · {{ stateLabel(snapshot.status) }}</p><p v-if="snapshot.snapshot_ready" class="mt-1 break-all">{{ snapshot.snapshot_id }} · SHA256 {{ snapshot.dataset_snapshot_sha256 }}</p><p v-for="reason in snapshot.blockers" :key="reason" class="mt-1 break-words">{{ reason }}</p></div>
    <div v-if="runtime" class="mt-4 border-t border-ink/20 pt-3">
      <h3 class="text-xs font-black">M2 runtime 与快照能力</h3>
      <label class="mt-2 block text-xs">已注册 runtime<select v-model="selectedRuntime" aria-label="已注册 M2 runtime" :disabled="disabled || freezePlan !== null" class="mt-1 min-h-11 w-full rounded-lg border border-ink bg-canvas p-2"><option value="">服务端默认 runtime</option><option v-for="option in runtime.runtime_options" :key="option.registration_id" :value="option.registration_id" :disabled="!option.ready">{{ option.variant_id ?? option.registration_id }} · {{ option.device }} · {{ option.ready ? '已验证' : option.reason ?? '未就绪' }}</option></select></label>
      <p class="mt-2 text-xs">{{ isReady(runtime) ? '当前能力和快照已核验，可创建真实 M2 草稿运行' : '真实 M2 尚未就绪' }} · {{ runtime.provider_mode ?? '视觉 provider 状态未知' }}</p>
      <p v-for="blocker in runtime.blockers" :key="blocker.code" class="mt-1 break-words text-xs">{{ blocker.code }}：{{ blocker.detail }}</p>
      <p v-if="runtime.snapshot" class="mt-2 break-all text-[10px]">快照 {{ runtime.snapshot.snapshot_id }} · {{ runtime.snapshot.sample_count }} 样本 / {{ runtime.snapshot.distinct_labels }} 字 · {{ runtime.snapshot.snapshot_sha256 }}</p>
      <button type="button" class="btn-hard mt-3 min-h-11" :disabled="!canCreateM2" @click="createM2">创建真实 M2 草稿运行</button>
      <p class="mt-2 text-[10px]">此按钮不启动优化器。启动前仍须核对执行门；模型质量、评估和发布门保持独立。</p>
    </div>
  </section>
</template>
