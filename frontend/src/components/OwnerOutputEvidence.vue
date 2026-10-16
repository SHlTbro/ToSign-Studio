<script setup lang="ts">
import { ref, computed, watch, onBeforeUnmount, nextTick } from 'vue'
import { request } from '@/api/http'
import { importOutputEvidence, type OutputEvidenceContext } from '@/api/ownerOutput'
import { useAuthStore } from '@/stores/auth'
const props = defineProps<{ runId: number; disabled: boolean }>()
const emit = defineEmits<{ problem: [string] }>()
const auth = useAuthStore()
const targets = ref<{ item_id: string; label: string; render_version: number }[]>([])
const itemId = ref(''), packageId = ref(''), file = ref<File | null>(null)
const data = ref<OutputEvidenceContext | null>(null), acknowledged = ref(false), error = ref(''), busy = ref(false)
watch(error, value => emit('problem', value), { immediate: true })
const loaded = ref<string[]>([])
const allImagesLoaded = computed(() => data.value?.images.length === 2 && data.value.images.every(v => loaded.value.includes(v.url)))
const canAccept = computed(() => !props.disabled && !busy.value && data.value?.status === 'READY' && data.value.package_ok === true
  && data.value.target_binding.ok === true && allImagesLoaded.value && acknowledged.value && data.value.acceptance?.status !== 'ACCEPTED')
let generation = 0, disposed = false, restoring = false, upload: AbortController | null = null
const path = () => `/api/v1/style-runs/${props.runId}`
const key = () => `owner-output-evidence:${auth.me?.user_id}:${props.runId}`
function clearView() { data.value = null; loaded.value = []; acknowledged.value = false; error.value = '' }
function cancel() { ++generation; upload?.abort(); upload = null; busy.value = false; clearView() }
function selectFile(event: Event) { file.value = (event.target as HTMLInputElement).files?.[0] ?? null; packageId.value = ''; cancel() }
function remember() { if (auth.me?.user_id && packageId.value && itemId.value) localStorage.setItem(key(), JSON.stringify({ package_id: packageId.value, item_id: itemId.value })) }
async function inspect() {
  cancel(); const token = generation
  if (!packageId.value || !itemId.value || props.disabled) return
  busy.value = true
  try {
    const value = await request<OutputEvidenceContext>(`${path()}/owner-output-evidence/${encodeURIComponent(itemId.value)}?package_id=${packageId.value}`, { retries: 0 })
    if (disposed || token !== generation) return
    if (!['READY', 'BLOCKED'].includes(value.status) || !Array.isArray(value.images)) throw new Error('输出绑定响应不完整')
    if (value.status === 'READY' && (value.package_ok !== true || value.target_binding?.ok !== true || !/^[0-9a-f]{64}$/.test(value.candidate_id ?? '')
      || value.images.length !== 2 || value.images.some(row => !row.url.startsWith(`${path()}/owner-output-preview/`)))) throw new Error('实际目标尚未通过校验')
    data.value = value; remember()
  } catch (exc) { if (!disposed && token === generation) error.value = exc instanceof Error ? exc.message : '校验失败' }
  finally { if (token === generation) busy.value = false }
}
async function initialize() {
  restoring = true; cancel(); busy.value = true; targets.value = []; packageId.value = ''; itemId.value = ''; file.value = null
  const token = generation
  if (!auth.me?.user_id || props.disabled) { busy.value = false; restoring = false; return }
  try {
    const value = await request<{ items: typeof targets.value }>(`${path()}/owner-output-targets`, { retries: 0 })
    if (disposed || token !== generation) return
    if (!Array.isArray(value.items) || value.items.some(row => typeof row.item_id !== 'string' || typeof row.label !== 'string' || !Number.isInteger(row.render_version))) throw new Error('成品列表响应不完整，请刷新核对')
    targets.value = value.items
    const saved = JSON.parse(localStorage.getItem(key()) ?? 'null')
    if (saved && /^[0-9a-f]{64}$/.test(saved.package_id) && targets.value.some(v => v.item_id === saved.item_id)) {
      packageId.value = saved.package_id; itemId.value = saved.item_id; await nextTick(); restoring = false; await inspect()
    }
  } catch (exc) { if (!disposed && token === generation) error.value = exc instanceof Error ? exc.message : '读取失败' }
  finally { restoring = false; if (token === generation) busy.value = false }
}
async function importPackage() {
  if (busy.value || props.disabled || !file.value) return
  cancel(); const token = generation; upload = new AbortController(); busy.value = true
  try {
    const result = await importOutputEvidence(`${path()}/owner-output-packages`, file.value, upload.signal)
    if (disposed || token !== generation) return
    packageId.value = result.package_id; await inspect()
  } catch (exc) { if (!disposed && token === generation && !(exc instanceof DOMException && exc.name === 'AbortError')) error.value = exc instanceof Error ? exc.message : '导入失败' }
  finally { if (token === generation) { busy.value = false; upload = null } }
}
async function accept() {
  if (!canAccept.value) return
  const token = generation, current = data.value?.candidate_id
  busy.value = true
  try {
    await request(`${path()}/owner-output-evidence/${encodeURIComponent(itemId.value)}?package_id=${packageId.value}`, { method: 'POST', retries: 0,
      body: { candidate_id: current, decision: 'accept', acknowledge_viewed_images: true, statement: '我已查看导入参考和当前实际输出，仅接受此模型、字体、布局、seed 与输出版本的外观，不接受全部模型或未来输出。' } })
    if (!disposed && token === generation) await inspect()
  } catch (exc) { if (!disposed && token === generation) error.value = exc instanceof Error ? exc.message : '结果未确认，请刷新核对' }
  finally { if (token === generation) busy.value = false }
}
async function resetContext() {
  restoring = true; cancel(); targets.value = []; packageId.value = ''; itemId.value = ''; file.value = null
  await nextTick(); restoring = false
  if (disposed || props.disabled || !auth.me?.user_id) return
  try {
    const saved = JSON.parse(localStorage.getItem(key()) ?? 'null')
    if (saved && /^[0-9a-f]{64}$/.test(saved.package_id) && typeof saved.item_id === 'string') await initialize()
  } catch { error.value = '上次选择未能恢复，请刷新成品列表重新选择。' }
}
watch(() => `${auth.me?.user_id}:${props.runId}:${props.disabled}`, resetContext, { immediate: true })
watch(itemId, () => { if (!restoring) cancel() })
onBeforeUnmount(() => { disposed = true; cancel() })
</script>
<template>
  <section class="mt-3 rounded-xl border border-ink/20 p-3 text-xs" data-testid="owner-output-evidence">
    <h3 class="font-black">导入参考并接受指定输出</h3>
    <p class="mt-1">导入只校验证据。查看参考图和本人的当前输出后，才能记录此输出的外观接受；不产生盲审票，不改变评估与发布门。</p>
    <label class="mt-2 block">证据 ZIP<input type="file" accept=".zip" class="mt-1 min-h-11 w-full" :disabled="disabled || busy" @change="selectFile"></label>
    <label class="mt-2 block">当前可验证的输出<select v-model="itemId" class="mt-1 min-h-11 w-full rounded-lg border border-ink/20 bg-surface" :disabled="disabled || busy">
      <option value="">请选择</option><option v-for="target in targets" :key="target.item_id" :value="target.item_id">{{ target.label }} · 版本 {{ target.render_version }}</option>
    </select></label>
    <p v-if="!targets.length" class="mt-2">刷新成品列表后，仅能选择此模型生成且具备完整输出绑定的成品。历史示例不能代替实际输出。</p>
    <div class="mt-2 flex flex-wrap gap-2">
      <button type="button" class="min-h-11 underline" :disabled="disabled || busy" @click="initialize">刷新成品列表</button>
      <button type="button" class="min-h-11 underline" :disabled="disabled || busy || !file" @click="importPackage">导入并校验</button>
      <button type="button" class="min-h-11 underline" :disabled="disabled || busy || !packageId || !itemId" @click="inspect">刷新实际绑定</button>
      <button v-if="busy" type="button" class="min-h-11 underline" @click="cancel">取消等待</button>
    </div>
    <template v-if="data?.status === 'READY'">
      <p class="mt-2 break-all">限定模型 {{ data.model_sha256 }} · 仅当前输出版本 {{ data.render_version }}</p>
      <div class="mt-2 grid grid-cols-2 gap-2"><figure v-for="image in data.images" :key="image.url">
        <img :src="image.url" :alt="image.label" class="h-32 w-full object-contain" @load="loaded.push(image.url)"><figcaption>{{ image.label }}</figcaption>
      </figure></div>
      <p v-if="data.acceptance?.status === 'ACCEPTED'" role="status" class="mt-2 font-bold">已记录此输出的直接外观接受；发布资格仍单独判断。</p>
      <template v-else><label class="mt-2 flex min-h-11 items-center gap-2"><input v-model="acknowledged" type="checkbox" :disabled="disabled || busy || !allImagesLoaded">我已查看两张图，仅接受此实际输出的外观</label>
        <button type="button" class="btn-hard mt-2 min-h-11" :disabled="!canAccept" @click="accept">记录此输出的接受</button></template>
    </template>
    <p v-if="data?.status === 'BLOCKED'" class="mt-2 text-amber">尚不能接受：{{ data.detail }}</p>
    <p v-if="error" role="alert" class="mt-2 text-danger">{{ error }}</p>
  </section>
</template>
