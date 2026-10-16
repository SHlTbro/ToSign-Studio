<script setup lang="ts">
import { userError, userText, stateLabel } from '@/utils/userLanguage'
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useAuthStore } from '@/stores/auth'
import { ApiRequestError, request } from '@/api/http'
import { ENDPOINT_TRAINING_SNAPSHOT_PREFLIGHT, ENDPOINT_TRAINING_SNAPSHOT_PREPARE } from '@/api/endpoints'
import { assertSnapshotPreflight, parentAssignments, snapshotPlanIdentity } from '@/api/snapshotPlan'
import type { SnapshotPreflight } from '@/types/snapshot'
import type { SnapshotPreparation } from '@/types/domain'

const props = defineProps<{ styleId: number; disabled: boolean; refreshToken: string }>()
const emit = defineEmits<{ prepared: [result: SnapshotPreparation]; busy: [busy: boolean]; problem: [string] }>()
const auth = useAuthStore()
const userId = computed(() => auth.me?.user_id ?? '')
const scope = computed(() => `${userId.value}:${props.styleId}`)
const preflight = ref<SnapshotPreflight | null>(null)
const choices = ref<Record<string, string>>({})
const result = ref<SnapshotPreparation | null>(null)
const error = ref('')
watch(() => [error.value, ...(preflight.value?.blockers ?? []), ...(result.value?.blockers ?? [])].filter(Boolean).join('；'), value => emit('problem', value), { immediate: true })
const draftNotice = ref('')
const loading = ref(false)
const saving = ref(false)
const fresh = ref(false)
let generation = 0
let readGeneration = 0
let intakeRevision = 0
let disposed = false
const current = (token: number, key: string) => !disposed && generation === token && scope.value === key
const message = (value: unknown) => value instanceof ApiRequestError ? userError(value) : value instanceof Error ? value.message : String(value)
const storageKey = () => `yy:p7:family-plan:${scope.value}`
const assignments = computed(() => preflight.value ? parentAssignments(preflight.value, choices.value) : null)
const canPrepare = computed(() => Boolean(fresh.value && preflight.value?.base_intake_sha256 && !props.disabled && !loading.value && !saving.value
  && (preflight.value.explicit_family_split_required ? assignments.value : preflight.value.structural_dataset_ready)))

async function readPreflight(styleId: number, uid: string): Promise<SnapshotPreflight> {
  const value = await request<SnapshotPreflight>(`${ENDPOINT_TRAINING_SNAPSHOT_PREFLIGHT.path}?style_id=${styleId}`, { retries: 0, timeoutMs: 60000 })
  assertSnapshotPreflight(value, uid, styleId)
  return value
}
function adopt(value: SnapshotPreflight, recoverDraft: boolean): void {
  const changed = !preflight.value || snapshotPlanIdentity(preflight.value) !== snapshotPlanIdentity(value)
  if (changed) { choices.value = {}; result.value = null; draftNotice.value = preflight.value ? '样本、来源或版本选择已变化，请重新分组' : '' }
  preflight.value = value
  fresh.value = true
  if (!recoverDraft || !changed) return
  try {
    const saved = JSON.parse(localStorage.getItem(storageKey()) ?? 'null')
    if (saved?.identity === snapshotPlanIdentity(value) && saved.choices && typeof saved.choices === 'object' && !Array.isArray(saved.choices)
      && Object.entries(saved.choices).every(([id, side]) => value.family_components.some((component) => component.component_id === id) && ['train', 'val', ''].includes(String(side)))) {
      choices.value = saved.choices
      draftNotice.value = '已恢复当前来源的分组草稿；尚未确认或冻结快照，提交时会重新核验'
    } else if (saved) draftNotice.value = '旧分组草稿与当前样本不匹配，已忽略，请重新选择'
  } catch { draftNotice.value = '无法读取本机分组草稿，请重新选择' }
}
async function refresh(): Promise<void> {
  if (saving.value || !userId.value) return
  const token = generation; const key = scope.value; const read = ++readGeneration
  loading.value = true; fresh.value = false; result.value = null; error.value = ''
  try {
    const next = await readPreflight(props.styleId, userId.value)
    if (current(token, key) && read === readGeneration) adopt(next, true)
  } catch (reason) { if (current(token, key) && read === readGeneration) error.value = message(reason) }
  finally { if (current(token, key) && read === readGeneration) loading.value = false }
}
function changed(): void {
  result.value = null
  if (!preflight.value) return
  try { localStorage.setItem(storageKey(), JSON.stringify({ identity: snapshotPlanIdentity(preflight.value), choices: choices.value })) }
  catch { draftNotice.value = '浏览器无法保存草稿；刷新后需重新选择' }
}
async function prepare(): Promise<void> {
  if (!canPrepare.value || !preflight.value) return
  const token = generation; const key = scope.value; const uid = userId.value; const styleId = props.styleId
  const revision = intakeRevision
  const identity = snapshotPlanIdentity(preflight.value)
  const body = preflight.value.explicit_family_split_required
    ? { style_id: styleId, parent_assignments: { ...assignments.value! }, expected_base_intake_sha256: preflight.value.base_intake_sha256 }
    : { style_id: styleId }
  saving.value = true; emit('busy', true); result.value = null; error.value = ''
  try {
    const next = await readPreflight(styleId, uid)
    if (!current(token, key)) return
    if (revision !== intakeRevision) { fresh.value = false; error.value = '样本状态在准备期间变化，本次未提交，请刷新来源'; return }
    if (snapshotPlanIdentity(next) !== identity) {
      adopt(next, false); error.value = '当前来源已变化，本次未提交。请检查新的来源分组'; return
    }
    const saved = await request<SnapshotPreparation>(ENDPOINT_TRAINING_SNAPSHOT_PREPARE.path, { method: 'POST', body, retries: 0, timeoutMs: 60000 })
    if (!current(token, key)) return
    if (revision !== intakeRevision) { fresh.value = false; error.value = '请求期间样本状态变化，返回结果未作为当前来源快照显示，请刷新后核验'; return }
    if (!saved || !['READY', 'BLOCKED', 'BLOCKED_DEPENDENCY'].includes(saved.status) || !Array.isArray(saved.blockers)
      || typeof saved.snapshot_ready !== 'boolean' || typeof saved.snapshot_created !== 'boolean' || typeof saved.snapshot_reused !== 'boolean'
      || (saved.status !== 'READY' && saved.snapshot_ready)
      || (saved.status === 'READY' && (saved.snapshot_ready !== true || saved.snapshot_created === saved.snapshot_reused
        || String(saved.target_user_id) !== uid || saved.target_style_id !== styleId || saved.base_intake_sha256 !== next.base_intake_sha256
        || !/^[0-9a-f]{64}$/.test(saved.dataset_snapshot_sha256 ?? '')))) throw new Error('服务端快照结果不完整或身份不匹配，请重新核验')
    result.value = saved
    emit('prepared', saved)
  } catch (reason) {
    if (!current(token, key)) return
    fresh.value = false
    error.value = reason instanceof ApiRequestError && reason.code === 'INTAKE_PREFLIGHT_STALE'
      ? '服务端检测到来源已变化，未冻结此选择。请刷新后重新分组'
      : `${message(reason)}。快照结果尚未确认；刷新来源后可显式重试，服务端会复用同一份已验证快照`
  } finally { if (current(token, key)) { saving.value = false; emit('busy', false) } }
}
watch(scope, () => {
  ++generation; ++readGeneration
  preflight.value = null; choices.value = {}; result.value = null; error.value = ''; draftNotice.value = ''; fresh.value = false; loading.value = false
  if (saving.value) emit('busy', false)
  saving.value = false; void refresh()
}, { immediate: true, flush: 'sync' })
watch(() => props.refreshToken, () => { ++intakeRevision; fresh.value = false; result.value = null; if (!saving.value) void refresh() })
onBeforeUnmount(() => { disposed = true; ++generation; ++readGeneration; if (saving.value) emit('busy', false) })
</script>

<template>
  <div class="mt-3 rounded-xl border border-ink/20 bg-canvas p-3" data-testid="snapshot-preparation">
    <div class="flex items-center justify-between gap-2"><h3 class="text-xs font-black">准备 S03 训练快照</h3><button type="button" class="min-h-11 text-xs font-bold underline" :disabled="loading || saving" @click="refresh">{{ loading ? '读取来源中…' : '刷新来源分组' }}</button></div>
    <p class="text-[11px] text-ink-muted">先读取当前来源；选择训练和验证组后，服务端重新核验来源、样本数量及真实视觉证据。分组选择本身不代表人工或 AI 审核通过。</p>
    <p v-if="error" class="mt-2 text-xs font-bold text-danger" role="alert">{{ error }}</p>
    <p v-if="draftNotice" class="mt-2 text-[11px] text-amber">{{ draftNotice }}</p>
    <template v-if="preflight">
      <ul v-if="preflight.blockers.length" class="mt-2 text-[11px] text-amber"><li v-for="blocker in preflight.blockers" :key="blocker">{{ userText(blocker) }}</li></ul>
      <div v-if="preflight.explicit_family_split_required" class="mt-3 space-y-2" data-testid="family-plan">
        <p class="text-xs font-bold">每个来源组必须明确选择用途。同源原图与相关版本共同分组，避免训练内容混入验证集。</p>
        <article v-for="(component, index) in preflight.family_components" :key="component.component_id" class="rounded-lg border border-ink/20 bg-surface p-2">
          <label class="text-xs font-black">来源组 {{ index + 1 }} · {{ component.source_asset_ids.length }} 张原图
            <select v-model="choices[component.component_id]" class="mt-1 min-h-11 w-full rounded-lg border border-ink bg-white px-2" :aria-label="`来源组 ${index + 1} 用途`" :disabled="loading || saving || disabled || !fresh" @change="changed">
              <option :value="undefined" disabled>请选择训练或验证</option><option value="train">训练 · 学习个人字形</option><option value="val">验证 · 保留用于独立检查</option>
            </select>
          </label>
          <p v-for="source in preflight.eligible_parent_sources.filter(row => component.parent_hashes.includes(row.source_asset_sha256))" :key="source.source_asset_id" class="mt-1 break-all text-[10px]">原图 {{ source.source_asset_id }} · {{ stateLabel(source.source_admission.history_status ?? '历史状态未报告') }} · {{ source.source_asset_sha256.slice(0, 12) }}</p>
        </article>
        <p v-if="!assignments" class="text-[11px] text-amber">请完整选择每个来源组，训练与验证各至少一组；实际样本与字符数量门槛由服务端核验。</p>
      </div>
      <p v-else class="mt-2 text-[11px]">当前来源沿用服务端既有分组规则；准备操作不会修改历史快照。</p>
      <p class="mt-2 break-all text-[10px] text-ink-muted">当前来源版本：{{ preflight.base_intake_sha256?.slice(0, 12) ?? '不可用' }} · {{ preflight.eligible_parent_sources.length }} 张原图</p>
    </template>
    <button type="button" class="btn-hard mt-2 min-h-12 w-full" :disabled="!canPrepare" @click="prepare">{{ saving ? '校验并准备中…' : '准备训练快照' }}</button>
    <template v-if="result">
      <p class="mt-2 text-xs font-black" :class="result.status === 'READY' && result.snapshot_ready ? 'text-emerald-800' : 'text-danger'">{{ result.status === 'READY' && result.snapshot_ready ? (result.snapshot_reused ? '已复用验证通过的 S03' : 'S03 已冻结并通过校验') : '尚未满足快照冻结条件' }}</p>
      <ul class="mt-1 text-[11px]"><li v-for="blocker in result.blockers" :key="blocker">{{ userText(blocker) }}</li></ul>
    </template>
  </div>
</template>
