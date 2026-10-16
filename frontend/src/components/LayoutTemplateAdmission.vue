<script setup lang="ts">
import { userError } from '@/utils/userLanguage'
import { stateLabel } from '@/utils/userLanguage'
import { onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ApiRequestError, request } from '@/api/http'
import type { LayoutTemplate } from '@/types/domain'
import { useAuthStore } from '@/stores/auth'

const props = defineProps<{ assetId: string }>()
const emit = defineEmits<{ verified: [id: string | null] }>()
const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const saved = ref<LayoutTemplate | null>(null)
const confirmed = ref(false)
const busy = ref(false)
const loading = ref(false)
const error = ref('')
const candidates = ref<LayoutTemplate[]>([])
const lookupDone = ref(false)
let generation = 0
let disposed = false

function failure(error: unknown): string {
  return error instanceof ApiRequestError ? userError(error)
    : error instanceof Error && error.message ? error.message : '布局模板操作失败'
}
async function readSaved(): Promise<void> {
  const current = ++generation
  saved.value = null; confirmed.value = false; error.value = ''; loading.value = false
  emit('verified', null)
  const id = route.query.template_id
  if (id == null) return
  if (typeof id !== 'string' || !id || !props.assetId) { error.value = '布局模板编号或源图无效'; return }
  loading.value = true
  try {
    const value = await request<LayoutTemplate>(`/api/v1/layout-templates/${encodeURIComponent(id)}`, { retries: 0 })
    if (disposed || generation !== current) return
    if (value.template_id !== id || value.base_asset_id !== props.assetId) throw new Error('该布局模板不属于当前源图，请重新选择')
    saved.value = value
    emit('verified', value.template_id)
  } catch (e) { if (!disposed && generation === current) error.value = failure(e) }
  finally { if (!disposed && generation === current) loading.value = false }
}
watch(() => [props.assetId, route.query.template_id, auth.me?.user_id], () => {
  candidates.value = []; lookupDone.value = false; void readSaved()
}, { immediate: true })
onBeforeUnmount(() => { disposed = true; generation += 1 })

async function approve(): Promise<void> {
  const target = saved.value
  if (!target || !confirmed.value || busy.value || !['draft', 'pending_review'].includes(target.status)) return
  const current = generation
  busy.value = true; error.value = ''
  try {
    const result = await request<LayoutTemplate>(`/api/v1/layout-templates/${encodeURIComponent(target.template_id)}/approve`, { method: 'POST', body: {}, retries: 0 })
    if (disposed || current !== generation) return
    if (result.template_id !== target.template_id || result.base_asset_id !== props.assetId || result.status !== 'approved') throw new Error('准入响应尚未确认，请重新读取模板状态')
    saved.value = result; confirmed.value = false
  } catch (e) {
    if (!disposed && current === generation) {
      error.value = `${failure(e)}；请重新读取模板状态，已准入的模板无需重复提交`
      // Approval is not idempotent (already-approved => 409). Require a
      // server re-read before any further approval attempt.
      saved.value = null; confirmed.value = false
    }
  } finally { if (!disposed) busy.value = false }
}

async function findSaved(): Promise<void> {
  if (loading.value || busy.value || !props.assetId) return
  const current = generation
  loading.value = true; error.value = ''; candidates.value = []
  try {
    // A bounded discovery page, never infer that an absent name means an
    // ambiguous create failed. A known ID always has the exact read path.
    const result = await request<{ items: LayoutTemplate[] }>('/api/v1/layout-templates?page=1&page_size=100', { retries: 0 })
    if (disposed || current !== generation) return
    candidates.value = result.items.filter(item => item.base_asset_id === props.assetId)
    lookupDone.value = true
  } catch (e) { if (!disposed && current === generation) error.value = failure(e) }
  finally { if (!disposed && current === generation) loading.value = false }
}
function selectSaved(id: string): void {
  void router.replace({ path: route.path, query: { ...route.query, template_id: id } })
}
</script>

<template>
  <section class="max-h-[35vh] shrink-0 space-y-2 overflow-y-auto border-b-2 border-ink bg-sky p-3" aria-label="已保存布局模板准入">
    <h2 class="text-sm font-black">已保存布局模板 · 单独确认准入</h2>
    <p class="text-xs">保存只生成草稿。此处准入的是服务端已保存的布局版本，不确认手写字符或模型质量，也不会自动生成图片。</p>
    <p v-if="loading" role="status" class="text-xs">正在读取布局模板…</p>
    <template v-else-if="saved">
      <p class="text-xs font-bold" role="status">{{ saved.name }} · v{{ saved.version }} · {{ saved.status === 'approved' ? '布局模板已准入' : saved.status === 'draft' ? '草稿' : saved.status }} · #{{ saved.template_id }}</p>
      <details class="text-xs"><summary class="cursor-pointer font-bold">查看已保存的源图与落点</summary><p>源图：{{ saved.base_asset_id }} · {{ saved.canvas_width }} × {{ saved.canvas_height }}</p><pre class="max-h-40 overflow-auto whitespace-pre-wrap">{{ JSON.stringify(saved.slots, null, 2) }}</pre></details>
      <template v-if="['draft', 'pending_review'].includes(saved.status)">
        <label class="flex items-start gap-2 text-xs font-bold"><input v-model="confirmed" type="checkbox" :disabled="busy" aria-label="确认已保存布局模板准入" />将上述已保存布局版本设为已准入，允许在批量生成时选用；画布上尚未保存的修改不包括在内。</label>
        <button type="button" class="btn-hard btn-hard--compact" :disabled="busy || !confirmed" @click="approve">{{ busy ? '正在准入…' : '确认准入此布局模板' }}</button>
      </template>
      <RouterLink v-else-if="saved.status === 'approved'" to="/batch" class="block text-xs font-bold underline">进入批量生成，选择已准入布局模板</RouterLink>
    </template>
    <p v-else-if="!error" class="text-xs">保存后会在此显示草稿，也可查找当前源图已有的模板继续。</p>
    <p v-if="error" role="alert" class="text-xs font-bold text-danger">{{ error }}</p>
    <div class="flex flex-wrap gap-2">
      <button v-if="route.query.template_id != null" type="button" class="btn-hard btn-hard--compact" :disabled="busy || loading" @click="readSaved">重新读取模板状态</button>
      <button type="button" class="btn-hard btn-hard--compact" :disabled="busy || loading" @click="findSaved">查找本图已保存模板</button>
    </div>
    <div v-if="lookupDone" class="space-y-1 text-xs">
      <p>以下来自当前账号最近 100 条模板；未列出不代表此前保存一定失败。</p>
      <button v-for="item in candidates" :key="item.template_id" type="button" class="block font-bold underline" :disabled="busy || loading" @click="selectSaved(item.template_id)">{{ item.name }} · {{ stateLabel(item.status) }} · #{{ item.template_id }}</button>
      <p v-if="!candidates.length">当前范围内没有此源图的已保存模板。</p>
    </div>
  </section>
</template>
