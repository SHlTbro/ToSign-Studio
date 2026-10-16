<script setup lang="ts">
import { computed, onBeforeUnmount, ref, toRaw, watch } from 'vue'
import { request } from '@/api/http'
interface Item { sample_id: string; label: string; source_image_url: string; glyph_image_url: string; source_bbox_norm: { x_norm: number; y_norm: number; w_norm: number; h_norm: number }; source_sha256?: string; image_sha256?: string; binding_sha256: string | null; blocker?: string | null; structural_checks?: { clear_errors: string[]; suspicious_reasons: string[] }; receipt?: { decision: string; actor: string; receipt_sha256: string } | null }
const props = defineProps<{ styleId: number; locked?: boolean; reviewKey?: string }>()
const emit = defineEmits<{ reviewed: []; busy: [boolean]; problem: [string] }>()
const items = ref<Item[]>([]); const selected = ref<Item | null>(null); const loading = ref(false); const working = ref(false); const error = ref('')
const checks = ref({ source_label_matches: false, strokes_complete: false, alpha_usable: false, suspicions_checked: false })
const reason = ref(''); const loaded = ref({ source: false, glyph: false }); const imageError = ref(false)
watch(() => error.value || (imageError.value ? '输入图片读取失败，不能接受当前输入' : ''), value => emit('problem', value), { immediate: true })
let generation = 0; let disposed = false
const disabled = computed(() => props.locked || loading.value || working.value)
const canAccept = computed(() => !disabled.value && !error.value && !imageError.value && loaded.value.source && loaded.value.glyph && Boolean(selected.value?.binding_sha256) && !selected.value?.structural_checks?.clear_errors.length && Object.values(checks.value).every(Boolean) && reason.value.trim().length > 0)
const canReject = computed(() => !disabled.value && !error.value && Boolean(selected.value?.binding_sha256) && reason.value.trim().length > 0)
function open(item: Item) { if (disabled.value) return; selected.value = structuredClone(toRaw(item)); checks.value = { source_label_matches: false, strokes_complete: false, alpha_usable: false, suspicions_checked: false }; reason.value = ''; loaded.value = { source: false, glyph: false }; imageError.value = false; error.value = '' }
function cancel() { if (!working.value) { selected.value = null; error.value = '' } }
async function refresh() {
  if (working.value || disposed) return
  const current = ++generation; const styleId = props.styleId
  loading.value = true; error.value = ''; items.value = []; selected.value = null
  try {
    const response = await request<{ style_id: number; items: Item[] }>(`/api/v1/training-samples/human-visual-review?style_id=${styleId}`, { retries: 0 })
    if (disposed || current !== generation) return
    if (response.style_id !== styleId || !Array.isArray(response.items)) throw new Error('人工视觉列表响应不完整')
    items.value = response.items
  } catch (e) { if (!disposed && current === generation) error.value = String(e) }
  finally { if (!disposed && current === generation) loading.value = false }
}
async function submit(decision: 'accept' | 'reject') {
  if (!selected.value || !(decision === 'accept' ? canAccept.value : canReject.value)) return
  const item = selected.value; const styleId = props.styleId
  working.value = true; emit('busy', true)
  try {
    await request(`/api/v1/training-samples/${encodeURIComponent(item.sample_id)}/human-visual-review`, { method: 'POST', retries: 0, body: { style_id: styleId, expected_binding_sha256: item.binding_sha256, decision, checks: { ...checks.value }, reason: reason.value } })
    if (!disposed && styleId === props.styleId) { selected.value = null; emit('reviewed') }
  } catch (e) { if (!disposed && styleId === props.styleId) error.value = `${String(e)}；先取消并刷新核对已保存记录` }
  finally { working.value = false; emit('busy', false) }
  if (!disposed && (!error.value || styleId !== props.styleId)) await refresh()
}
watch(() => [props.styleId, props.reviewKey], () => { ++generation; items.value = []; selected.value = null; error.value = ''; void refresh() }, { immediate: true })
onBeforeUnmount(() => { disposed = true; ++generation })
</script>
<template>
  <section class="mt-3 rounded-xl border border-ink/30 p-3 text-xs" aria-labelledby="human-input-heading">
    <div class="flex flex-wrap items-center justify-between gap-2"><h3 id="human-input-heading" class="font-black">本人输入图片视觉审核</h3><button type="button" class="btn-hard btn-hard--compact" :disabled="disabled" @click="refresh">刷新人工视觉审核</button></div>
    <p class="mt-2">字符确认后，查看真实原图与透明字形，另行审核标签、完整笔画及 alpha。人工意见单独绑定来源、字形和声明哈希，无需 AI provider。它不代表生成模型质量通过，也不授予发布资格。</p>
    <p v-if="loading" role="status" class="mt-2">读取当前输入审核…</p><p v-if="error" role="alert" class="mt-2 break-words">{{ error }}</p>
    <p v-if="!loading && !error && !items.length" class="mt-2">尚无本人明确确认的字符；请先完成 P7 字符审核。</p>
    <ul class="mt-2 space-y-2"><li v-for="item in items" :key="item.sample_id" class="rounded-lg border border-ink/20 p-2"><p class="break-words">{{ item.label }} · {{ item.receipt ? `人工意见 ${item.receipt.decision}` : '尚未审核当前像素' }}</p><p v-if="item.blocker" class="mt-1 break-words">{{ item.blocker }}</p><button class="btn-hard mt-2" type="button" :disabled="disabled || !item.binding_sha256" @click="open(item)">查看并审核字 {{ item.label }}</button></li></ul>
    <div v-if="selected" class="mt-3 rounded-lg border-2 border-amber p-3" @keydown.esc="cancel">
      <h4 class="text-sm font-black">人工审核当前输入字 {{ selected.label }}</h4>
      <div class="mt-2 grid gap-3 sm:grid-cols-2">
        <figure><figcaption class="mb-1">原图 · 框线为来源位置</figcaption><div class="relative"><img :src="selected.source_image_url" alt="待审核字的真实原图" class="w-full object-contain" @load="loaded.source = true" @error="imageError = true"><div class="pointer-events-none absolute border-2 border-red-500" :style="{ left: `${selected.source_bbox_norm.x_norm * 100}%`, top: `${selected.source_bbox_norm.y_norm * 100}%`, width: `${selected.source_bbox_norm.w_norm * 100}%`, height: `${selected.source_bbox_norm.h_norm * 100}%` }" /></div></figure>
        <figure><figcaption class="mb-1">当前透明字形 · 标签 {{ selected.label }}</figcaption><img :src="selected.glyph_image_url" alt="待审核的透明字形" class="h-64 w-full rounded-lg border border-ink/30 object-contain" style="background-color:#aaa;background-image:conic-gradient(#eee 25%,transparent 0 50%,#eee 0 75%,transparent 0);background-size:20px 20px" @load="loaded.glyph = true" @error="imageError = true"></figure>
      </div>
      <p v-if="imageError" role="alert" class="mt-2">图片读取失败，不能接受当前输入。</p>
      <p class="mt-2 break-all text-[10px]">来源 {{ selected.source_sha256 }} · alpha {{ selected.image_sha256 }} · 绑定 {{ selected.binding_sha256 }}</p>
      <p class="mt-2">结构错误：{{ selected.structural_checks?.clear_errors.join(' · ') || '未检出' }}；疑点：{{ selected.structural_checks?.suspicious_reasons.join(' · ') || '未检出' }}</p>
      <fieldset class="mt-2 space-y-2" :disabled="disabled">
        <label class="flex min-h-11 items-center gap-2"><input v-model="checks.source_label_matches" type="checkbox" aria-label="本人已核对原图与标签一致">原图对应位置与当前字样标签一致。</label>
        <label class="flex min-h-11 items-center gap-2"><input v-model="checks.strokes_complete" type="checkbox" aria-label="本人已核对笔画完整">笔画完整，没有错误切割、缺笔或其他字残留。</label>
        <label class="flex min-h-11 items-center gap-2"><input v-model="checks.alpha_usable" type="checkbox" aria-label="本人已核对透明字形可用">alpha 方向和透明背景正确，可用于当前输入。</label>
        <label class="flex min-h-11 items-center gap-2"><input v-model="checks.suspicions_checked" type="checkbox" aria-label="本人已逐项核对结构疑点">我已查看两张图片并逐项核对上述结构疑点。</label>
        <label class="block">人工审核说明<textarea v-model="reason" maxlength="1000" aria-label="人工输入审核说明" class="mt-1 min-h-20 w-full rounded-lg border border-ink bg-canvas p-2" /></label>
      </fieldset>
      <div class="mt-2 flex flex-wrap gap-2"><button class="btn-hard" type="button" :disabled="working" @click="cancel">取消输入审核</button><button class="btn-hard" type="button" :disabled="!canReject" @click="submit('reject')">本人拒绝当前输入</button><button class="btn-hard" type="button" :disabled="!canAccept" @click="submit('accept')">本人接受当前输入</button></div>
    </div>
  </section>
</template>
