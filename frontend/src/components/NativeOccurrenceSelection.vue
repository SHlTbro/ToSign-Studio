<script setup lang="ts">
import { userError } from '@/utils/userLanguage'
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useAuthStore } from '@/stores/auth'
import { ApiRequestError, request } from '@/api/http'
import { ENDPOINT_NATIVE_OCCURRENCES, ENDPOINT_NATIVE_OCCURRENCE_SELECT, ENDPOINT_NATIVE_REVIEW_DETAIL, fill } from '@/api/endpoints'
import { assertJob, boxesOverlap, imagePath, isHash, isJobId, isBox, occurrenceGroups, refKey } from '@/api/nativeIntake'
import type { NativeCandidate, NativeRef, NativeReviewJob, NativeVersion, OccurrenceInventory, OccurrenceReceipt } from '@/types/nativeIntake'
const props = defineProps<{ styleId: number; knownJobIds: string[]; refreshToken: number }>()
const auth = useAuthStore()
const userId = computed(() => auth.me?.user_id ?? '')
const scope = computed(() => `${userId.value}:${props.styleId}`)
const inventory = ref<OccurrenceInventory | null>(null)
const origins = ref<{ ref: NativeRef; candidate: NativeCandidate }[]>([])
const assignment = ref<Record<string, string>>({})
const selected = ref<Record<string, string>>({})
const receipt = ref<OccurrenceReceipt | null>(null)
const loading = ref(false)
const saving = ref(false)
const fresh = ref(false)
const error = ref<string | null>(null)
const originWarnings = ref<string[]>([])
const draftNotice = ref('')
let generation = 0
let saveToken = 0
let lastScope = scope.value
let disposed = false
const current = (g: number, key: string) => !disposed && generation === g && scope.value === key
const endpoint = (path: string) => fill(path, { id: String(props.styleId) })
const message = (e: unknown) => e instanceof ApiRequestError ? userError(e) : e instanceof Error ? e.message : String(e)
const originRefs = computed(() => new Map(origins.value.map((origin) => [refKey(origin.ref), origin.ref])))
const groups = computed(() => inventory.value ? occurrenceGroups(inventory.value, assignment.value, selected.value, originRefs.value) : null)
const grouped = computed(() => {
  const rows = new Map<string, NativeVersion[]>()
  for (const version of inventory.value?.versions ?? []) {
    const origin = assignment.value[refKey(version.ref)]
    if (origin) rows.set(origin, [...(rows.get(origin) ?? []), version])
  }
  return [...rows.entries()].map(([key, revisions]) => ({ key, revisions, origin: origins.value.find((origin) => refKey(origin.ref) === key) }))
})
function allowedOrigins(version: NativeVersion) {
  return origins.value.filter((origin) => origin.candidate.source_asset_id === version.source_asset_id && origin.candidate.source_image_sha256 === version.parent_source_hash
    && boxesOverlap(origin.candidate.source_bbox_norm, version.source_bbox_norm))
}
const versionNumber = (reference: NativeRef) => (inventory.value?.versions.findIndex(version => refKey(version.ref) === refKey(reference)) ?? -1) + 1
const originNumber = (reference: NativeRef) => origins.value.findIndex(origin => refKey(origin.ref) === refKey(reference)) + 1
const boxLabel = (box: NativeVersion['source_bbox_norm']) => `横坐标 ${box.x_norm} · 纵坐标 ${box.y_norm} · 宽 ${box.w_norm} · 高 ${box.h_norm}（归一化）`
const validGroups = computed(() => Boolean(groups.value && (inventory.value?.versions ?? []).every((version) =>
  allowedOrigins(version).some((origin) => refKey(origin.ref) === assignment.value[refKey(version.ref)]))))
const canSave = computed(() => Boolean(fresh.value && validGroups.value && !loading.value && !saving.value))
function validateInventory(value: OccurrenceInventory): void {
  if (!value || !Array.isArray(value.versions) || typeof value.selection_required !== 'boolean'
    || (value.selection_required && (!isHash(value.native_intake_sha256) || !value.versions.length))
    || (!value.selection_required && (value.native_intake_sha256 !== null || value.versions.length))
    || new Set(value.versions.map((row) => refKey(row.ref))).size !== value.versions.length
    || value.versions.some((row) => !isJobId(row.ref?.job_id) || !/^[A-Za-z0-9_-]{1,80}$/.test(row.ref?.candidate_id)
      || !row.sample_id || !row.source_asset_id || !isHash(row.parent_source_hash) || !isBox(row.source_bbox_norm) || !isHash(row.image_sha256) || !isHash(row.candidate_version_sha256))) throw new Error('服务端候选版本清单无效；已阻止选择')
}
async function load(): Promise<void> {
  const g = ++generation; const key = scope.value; const uid = userId.value; const styleId = props.styleId
  if (!uid) return
  loading.value = true; fresh.value = false; error.value = null; receipt.value = null; originWarnings.value = []
  try {
    const next = await request<OccurrenceInventory>(endpoint(ENDPOINT_NATIVE_OCCURRENCES.path), { retries: 0 })
    if (!current(g, key)) return
    validateInventory(next)
    const changed = inventory.value?.native_intake_sha256 !== next.native_intake_sha256
    inventory.value = next
    if (changed) { assignment.value = {}; selected.value = {}; draftNotice.value = '候选版本清单已重新读取；需要显式指定原始位置与唯一采用版本' }
    const mandatory = new Set(next.versions.map((version) => version.ref.job_id))
    const ids = [...new Set([...mandatory, ...props.knownJobIds.filter(isJobId)])]
    const loaded: { ref: NativeRef; candidate: NativeCandidate }[] = []
    const warnings: string[] = []
    for (const id of ids) {
      try {
        const job = await request<NativeReviewJob>(fill(ENDPOINT_NATIVE_REVIEW_DETAIL.path, { id }), { retries: 0 })
        if (!current(g, key)) return
        assertJob(job, uid, styleId, id)
        if (job.code_version_matches !== true) throw new Error('任务代码绑定尚未匹配当前版本')
        for (const version of next.versions.filter((row) => row.ref.job_id === id)) {
          const candidate = job.package.items.find((item) => item.candidate_id === version.ref.candidate_id)
          const decision = job.result?.items.find((item) => item.candidate_id === version.ref.candidate_id)
          if (job.status !== 'applied' || decision?.canonical_sample_id !== version.sample_id || decision.review_status !== 'ai_confirmed_authorized'
            || !candidate || candidate.candidate_version_sha256 !== version.candidate_version_sha256 || candidate.image_sha256 !== version.image_sha256
            || candidate.source_image_sha256 !== version.parent_source_hash || candidate.source_asset_id !== version.source_asset_id) throw new Error('可选版本与原始 AI 任务结果不一致')
        }
        loaded.push(...job.package.items.map((candidate) => ({ ref: { job_id: id, candidate_id: candidate.candidate_id }, candidate })))
      } catch (e) { if (mandatory.has(id)) throw e; warnings.push(`${id}：${message(e)}`) }
    }
    if (!current(g, key)) return
    origins.value = loaded; originWarnings.value = warnings; fresh.value = true
    if (changed && next.native_intake_sha256) {
      try {
        const cached = JSON.parse(localStorage.getItem(`yy:p4:occurrence-draft:${key}`) ?? 'null')
        if (cached?.native_intake_sha256 === next.native_intake_sha256 && cached.assignment && cached.selected
          && Object.values(cached.assignment).every((value) => typeof value === 'string') && Object.values(cached.selected).every((value) => typeof value === 'string')) {
          assignment.value = cached.assignment; selected.value = cached.selected; draftNotice.value = '已恢复本机选择草稿；尚未确认服务端当前选择，需显式重新提交核验'
        }
      } catch { draftNotice.value = '本机选择草稿不可读取，请重新选择' }
    }
  } catch (e) { if (current(g, key)) { error.value = message(e); origins.value = []; fresh.value = false } }
  finally { if (current(g, key)) loading.value = false }
}
function changed(): void {
  receipt.value = null
  try { localStorage.setItem(`yy:p4:occurrence-draft:${scope.value}`, JSON.stringify({ native_intake_sha256: inventory.value?.native_intake_sha256, assignment: assignment.value, selected: selected.value })) }
  catch { draftNotice.value = '浏览器无法保存选择草稿；刷新后需重新选择' }
}
async function save(): Promise<void> {
  if (!canSave.value || !inventory.value || !groups.value) return
  const g = generation; const key = scope.value; const expected = inventory.value.native_intake_sha256
  const body = { native_intake_sha256: expected, groups: JSON.parse(JSON.stringify(groups.value)) }
  const wanted = groups.value.map((group) => inventory.value!.versions.find((version) => refKey(version.ref) === refKey(group.selected))!.sample_id).sort()
  const excluded = inventory.value.versions.map((version) => version.sample_id).filter((id) => !wanted.includes(id)).sort()
  const token = ++saveToken
  saving.value = true; error.value = null; receipt.value = null
  try {
    const latest = await request<OccurrenceInventory>(endpoint(ENDPOINT_NATIVE_OCCURRENCES.path), { retries: 0 })
    if (!current(g, key)) return
    validateInventory(latest)
    if (latest.native_intake_sha256 !== expected) { const nextGeneration = generation + 1; await load(); if (current(nextGeneration, key)) error.value = '候选清单已变化，旧选择未提交；请按当前版本重新选择'; return }
    const result = await request<OccurrenceReceipt>(endpoint(ENDPOINT_NATIVE_OCCURRENCE_SELECT.path), { method: 'POST', body, retries: 0 })
    if (!current(g, key)) return
    if (result.schema !== 'native-occurrence-selection/1' || String(result.user_id) !== userId.value || result.style_id !== props.styleId
      || result.native_intake_sha256 !== expected || !isHash(result.plan_sha256) || result.preserves_all_review_rows !== true
      || result.authority?.actor_kind !== 'delegated_ai' || result.authority.human_review !== false || result.authority.changes_review_decisions !== false
      || JSON.stringify([...result.selected_sample_ids].sort()) !== JSON.stringify(wanted) || JSON.stringify([...result.excluded_sample_ids].sort()) !== JSON.stringify(excluded)) throw new Error('选择收据与当前账号、档案或唯一版本不匹配')
    receipt.value = result; draftNotice.value = ''
  } catch (e) { if (current(g, key)) { error.value = `${message(e)}；未假定选择成功。刷新清单后可显式重试`; fresh.value = false } }
  finally { if (!disposed && scope.value === key && token === saveToken) saving.value = false }
}
watch(() => [scope.value, props.refreshToken, props.knownJobIds.join(',')], () => { if (lastScope !== scope.value) { ++saveToken; saving.value = false; lastScope = scope.value } void load() }, { immediate: true })
onBeforeUnmount(() => { disposed = true; ++generation })
</script>

<template>
  <section class="space-y-3 rounded-lg border-2 border-ink/30 p-3" data-testid="native-occurrences">
    <h3 class="text-xs font-black">4. 原始字形位置与唯一采用版本</h3>
    <p class="text-xs">同一个原图位置的重复提取/修正稿必须归为同组，只能采用一个已获 自动视觉 接受的版本。被拒绝和不确定的版本不能作为训练版本；旧记录全部保留。同字不同位置仍是不同组，不能只按字名合并。</p>
    <button type="button" class="btn-hard min-h-11" :disabled="loading || saving" @click="load">{{ loading ? '读取当前版本…' : '刷新当前候选与来源' }}</button>
    <p v-if="error" role="alert" class="break-all text-xs text-danger">{{ error }}</p>
    <p v-if="draftNotice" class="text-xs text-amber">{{ draftNotice }}</p>
    <p v-if="inventory && !inventory.selection_required && fresh" class="text-xs">当前没有需要去重选择的已接受原生版本；这不代表训练数据已足够</p>
    <p v-if="originWarnings.length" class="break-all text-xs text-amber">部分历史任务不可作为来源：{{ originWarnings.join('；') }}。需要的原始任务不在此处时，请从上方任务列表继续加载后重试。</p>
    <details v-if="inventory?.native_intake_sha256" class="break-all text-[10px]"><summary>高级来源信息</summary>当前来源集合校验码：{{ inventory.native_intake_sha256 }}</details>
    <fieldset v-if="inventory?.versions.length" :disabled="!fresh || loading || saving" class="space-y-3">
      <article v-for="version in inventory.versions" :key="refKey(version.ref)" class="rounded border border-ink/20 p-2">
        <p class="break-all text-xs font-bold">自动视觉已接受版本 {{ versionNumber(version.ref) }} · {{ version.label }}</p>
        <div class="grid grid-cols-2 gap-2"><img :src="imagePath(version.ref, 'alpha')" :alt="`${version.ref.candidate_id} 可选字形底稿`" class="h-24 w-full bg-black object-contain"><img :src="imagePath(version.ref, 'source-context')" :alt="`${version.ref.candidate_id} 来源上下文`" class="h-24 w-full object-contain"></div>
        <details class="break-all text-[10px]"><summary>高级版本信息</summary><p>候选标识：{{ version.ref.candidate_id }} · 任务标识：{{ version.ref.job_id }}</p><p>原图 {{ version.source_asset_id }} · 原框 {{ version.source_bbox_norm }} · 版本校验码 {{ version.candidate_version_sha256 }}</p></details>
        <label class="block text-xs">它对应哪个原始位置？<select v-model="assignment[refKey(version.ref)]" :aria-label="`原始位置 ${version.ref.candidate_id} ${version.ref.job_id}`" class="min-h-11 w-full rounded border border-ink" @change="changed"><option value="">明确选择原始位置，不自动推断</option><option v-for="origin in allowedOrigins(version)" :key="refKey(origin.ref)" :value="refKey(origin.ref)">原始位置 {{ originNumber(origin.ref) }} · {{ boxLabel(origin.candidate.source_bbox_norm) }}</option></select></label>
      </article>
      <article v-for="group in grouped" :key="group.key" class="space-y-2 rounded border border-ink p-2">
        <p class="break-all text-xs font-black">同一原始位置<span v-if="group.origin"> {{ originNumber(group.origin.ref) }} · {{ boxLabel(group.origin.candidate.source_bbox_norm) }}</span></p>
        <img v-if="group.origin" :src="imagePath(group.origin.ref, 'source-context')" alt="所选原始位置上下文" class="h-28 w-full object-contain">
        <label v-for="version in group.revisions" :key="refKey(version.ref)" class="flex items-start gap-2 text-xs"><input v-model="selected[group.key]" type="radio" :name="`occurrence-${group.key}`" :value="refKey(version.ref)" :aria-label="`唯一采用 ${version.ref.candidate_id} ${version.ref.job_id}`" @change="changed">唯一采用版本 {{ versionNumber(version.ref) }} · {{ version.label }}</label>
      </article>
      <button type="button" class="btn-hard min-h-11" :disabled="!canSave" @click="save">提交唯一版本选择</button>
    </fieldset>
    <div v-if="receipt" class="break-all text-xs text-emerald-800" data-testid="native-occurrence-receipt">服务端已核验：保留 {{ receipt.selected_sample_ids.length }} 个原始位置版本，排除 {{ receipt.excluded_sample_ids.length }} 个重复版本。不是人工复核；未改写旧审核。<details><summary>高级收据信息</summary>收据校验码：{{ receipt.plan_sha256 }}</details></div>
    <p class="text-xs">后续快照仍需显式原图 训练组和验证组 家族分配和全部最低样本门槛。去重选择不等于数据足够，也不会自动冻结快照或训练。</p>
  </section>
</template>
