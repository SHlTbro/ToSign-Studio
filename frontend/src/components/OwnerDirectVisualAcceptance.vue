<script setup lang="ts">
import { stateLabel, userText, userError } from '@/utils/userLanguage'
import { ref, computed, watch, onBeforeUnmount } from 'vue'
import { request, ApiRequestError } from '@/api/http'
import { useAuthStore } from '@/stores/auth'
interface Context {
  status: 'READY' | 'BLOCKED'; candidate_id?: string; detail?: string; model_version?: string; evaluation_gate?: string
  acceptance?: { status: string }; images: { url: string; label: string; role: string; sha256: string }[]
}
const props = defineProps<{ runId: number; disabled: boolean }>()
const emit = defineEmits<{ accepted: []; problem: [string] }>()
const auth = useAuthStore()
const data = ref<Context | null>(null), busy = ref(false), error = ref('')
watch(() => error.value || (data.value?.status === 'BLOCKED' ? data.value.detail ?? '效果预览暂不可验收' : ''), value => emit('problem', value), { immediate: true })
const loadedImages = ref<string[]>([])
const previewsLoaded = computed(() => Boolean(data.value?.images.length && data.value.images.every(row => loadedImages.value.includes(row.url))))
function imageFailed(url: string) { loadedImages.value = loadedImages.value.filter(value => value !== url); error.value = '真实效果图片读取失败，请刷新当前输出；图片未显示时不能接受。' }
function imageLoaded(url: string) { if (data.value?.images.some(row => row.url === url) && !loadedImages.value.includes(url)) loadedImages.value.push(url) }
let generation = 0, disposed = false
const path = () => `/api/v1/style-runs/${props.runId}/owner-visual-acceptance`
function reset() { ++generation; data.value = null; loadedImages.value = []; error.value = ''; busy.value = false }
watch(() => `${auth.me?.user_id}:${props.runId}`, reset)
onBeforeUnmount(() => { disposed = true; ++generation })
async function load() {
  const token = ++generation; data.value = null; loadedImages.value = []; busy.value = true; error.value = ''
  try {
    const value = await request<Context>(path(), { retries: 0 })
    if (disposed || token !== generation) return
    if (!['READY', 'BLOCKED'].includes(value.status) || !Array.isArray(value.images)) throw new Error('服务端外观接受上下文不完整')
    if (value.status === 'READY' && (!/^[0-9a-f]{64}$/.test(value.candidate_id ?? '') || value.images.length < 3
      || value.images.some(row => !row.url.startsWith(`/api/v1/style-runs/${props.runId}/owner-visual-preview/`) || !/^[0-9a-f]{64}$/.test(row.sha256)))) throw new Error('当前预览绑定不完整，请刷新')
    data.value = value
  } catch (exc) { if (!disposed && token === generation) error.value = exc instanceof ApiRequestError ? userError(exc) : exc instanceof Error ? userText(exc.message) : '读取失败' }
  finally { if (token === generation) busy.value = false }
}
async function accept() {
  if (props.disabled || busy.value || !previewsLoaded.value || data.value?.status !== 'READY' || data.value.acceptance?.status === 'ACCEPTED') return
  const token = generation, id = props.runId, candidate = data.value.candidate_id
  busy.value = true; error.value = ''
  try {
    await request(path(), { method: 'POST', retries: 0, body: { candidate_id: candidate, decision: 'accept', acknowledge_viewed_images: true,
      statement: '我已查看本次显示的真实输出并接受其外观，限定当前模型、快照和预览；不接受全部未来输出。' } })
    if (disposed || token !== generation || props.runId !== id) return
    await load()
    if (!disposed && props.runId === id && data.value?.acceptance?.status === 'ACCEPTED') emit('accepted')
  } catch (exc) {
    if (!disposed && token === generation) {
      error.value = exc instanceof ApiRequestError ? userError(exc) : exc instanceof Error ? userText(exc.message) : '提交结果未确认，请刷新核对'
      busy.value = false
      // No automatic POST retry, even if the response was lost after recording.
    }
  } finally { if (token === generation && props.runId === id && !disposed) busy.value = false }
}
</script>
<template>
  <section class="mt-3 rounded-xl border border-ink/20 p-3 text-xs" data-testid="owner-direct-visual">
    <h3 class="font-black">查看效果并同意本次输出</h3>
    <p class="mt-1">这是本人直接同意，不是盲审。仅同意当前显示的外观，发布仍检查真实模型、快照和客观评估。</p>
    <button type="button" class="mt-2 min-h-11 underline" :disabled="disabled || busy" @click="load">{{ busy ? '读取中…' : '查看或刷新当前真实输出' }}</button>
    <template v-if="data?.status === 'READY'">
      <p class="mt-2 break-all">当前模型 {{ data.model_version }} · 客观评估 {{ stateLabel(data.evaluation_gate) }}</p>
      <div class="mt-2 grid grid-cols-2 gap-2 sm:grid-cols-3">
        <figure v-for="image in data.images" :key="image.url" class="min-w-0 rounded-lg border border-ink/10 p-2">
          <img :src="image.url" :alt="`当前真实输出 ${image.label}`" class="h-40 w-full object-contain" style="background-color:#aaa;background-image:conic-gradient(#eee 25%,transparent 0 50%,#eee 0 75%,transparent 0);background-size:16px 16px" @load="imageLoaded(image.url)" @error="imageFailed(image.url)">
          <figcaption>{{ image.label }} · {{ image.role === 'evaluation' ? '评估输出' : '新字输出' }}</figcaption>
        </figure>
      </div>
      <p v-if="data.evaluation_gate !== 'PASS'" class="mt-2 font-bold text-amber" data-testid="owner-objective-blocked">客观评估尚未通过；可以记录外观接受，发布仍受阻。</p>
      <p v-if="data.acceptance?.status === 'ACCEPTED'" class="mt-2 font-bold" role="status">服务端已记录本次直接接受；没有生成盲审投票。</p>
      <template v-else>
        <p class="mt-2">点击下方按钮即确认：我已查看本次显示的图片并同意其外观。</p>
        <button type="button" class="btn-hard mt-2 min-h-11" :disabled="disabled || busy || !previewsLoaded" @click="accept">同意本次效果</button>
      </template>
    </template>
    <p v-else-if="data?.status === 'BLOCKED'" class="mt-2 break-all text-amber">当前真实预览尚未就绪：{{ userText(data.detail) }}</p>
    <p v-if="error" class="mt-2 break-all text-danger" role="alert">{{ error }}</p>
  </section>
</template>
