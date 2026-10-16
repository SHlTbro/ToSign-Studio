<script setup lang="ts">
import { computed, watch } from 'vue'
import { useDraft } from '@/composables/useDraft'
import type { FrozenRenderSettings } from '@/types/renderControls'
const props = withDefaults(defineProps<{ disabled: boolean; scopeKey: string; v3Reset?: number }>(), { v3Reset: 0 })
export interface TypographySelection {
  text_style: NonNullable<FrozenRenderSettings['text_style']>
  handwriting: NonNullable<FrozenRenderSettings['handwriting']>
}
const emit = defineEmits<{ ready: [value: TypographySelection | null, reason?: string]; changed: [] }>()
const defaults = { ordinaryScale: 1.3, sizeVariation: 0, tiltVariation: 0, seed: 2026100401 }
const { data: values } = useDraft(`batch-typography:${props.scopeKey}`, { ...defaults })
const unreadable = computed(() => !values.value || typeof values.value !== 'object')
const inRange = (value: unknown, low: number, high: number) => typeof value === 'number' && Number.isFinite(value) && value >= low && value <= high
const problem = computed(() => {
  if (unreadable.value) return '保存的排版设置无法读取，请恢复默认。'
  if (!inRange(values.value.ordinaryScale, .5, 3)) return '基础字号请输入 0.5–3 倍。'
  if (!inRange(values.value.sizeVariation, 0, 20)) return '大小不规则程度请输入 0–20%。'
  if (!inRange(values.value.tiltVariation, 0, 8)) return '倾斜不规则程度请输入 0–8 度。'
  if (!inRange(values.value.seed, 0, 4294967295) || !Number.isInteger(values.value.seed)) return '重复种子请输入 0–4294967295 的整数。'
  return ''
})
const selection = computed<TypographySelection | null>(() => problem.value ? null : ({
  text_style: { ordinary_scale: values.value.ordinaryScale, to_ratio: 2, header_gap: 10 },
  handwriting: { size_variation_pct: values.value.sizeVariation, tilt_variation_deg: values.value.tiltVariation, seed: values.value.seed },
}))
watch(values, () => emit('changed'), { deep: true, flush: 'sync' })
watch(() => props.v3Reset, () => { values.value = { ...defaults } }, { flush: 'sync' })
watch([selection, problem], ([value, reason]) => emit('ready', value, reason), { immediate: true, flush: 'sync' })
</script>
<template>
  <section class="rounded-2xl border-2 border-ink bg-surface p-4" aria-labelledby="batch-typography-title">
    <div class="flex flex-wrap items-center justify-between gap-2">
      <h2 id="batch-typography-title" class="text-sm font-black">调整字的大小与倾斜</h2>
      <button type="button" data-testid="reset-typography" class="min-h-11 text-xs font-bold underline" :disabled="disabled" @click="values = { ...defaults }">恢复默认</button>
    </div>
    <p class="mb-3 text-xs leading-relaxed text-ink-muted">只调整本次图片排版，无需重新训练。普通字默认 1.3 倍，To 默认是普通字的 2 倍，与姓名保持 10 像素的墨迹间距。</p>
    <div v-if="!unreadable" class="grid gap-3 sm:grid-cols-3">
      <label class="text-xs font-bold">基础字号 · {{ values.ordinaryScale }} 倍
        <input v-model.number="values.ordinaryScale" aria-label="基础字号（倍）" type="number" min="0.5" max="3" step="0.1" class="input-hard mt-1 min-h-11 w-full" :disabled="disabled">
        <span class="mt-1 block font-normal text-ink-muted">V3 相对原始字号基准，0.5–3 倍</span>
      </label>
      <label class="text-xs font-bold">逐字大小不规则程度 · ±{{ values.sizeVariation }}%
        <input v-model.number="values.sizeVariation" aria-label="逐字大小不规则程度（%）" type="number" min="0" max="20" step="1" class="input-hard mt-1 min-h-11 w-full" :disabled="disabled">
        <span class="mt-1 block font-normal text-ink-muted">0 不扰动；20 最多上下变化 20%</span>
      </label>
      <label class="text-xs font-bold">逐字倾斜不规则程度 · ±{{ values.tiltVariation }}°
        <input v-model.number="values.tiltVariation" aria-label="逐字倾斜不规则程度（度）" type="number" min="0" max="8" step="0.5" class="input-hard mt-1 min-h-11 w-full" :disabled="disabled">
        <span class="mt-1 block font-normal text-ink-muted">0 不扰动；8 最多左右倾斜 8 度</span>
      </label>
    </div>
    <details v-if="!unreadable" class="mt-2 text-xs">
      <summary class="min-h-11 cursor-pointer font-bold">高级：保持相同的随机效果</summary>
      <label class="block">排版重复种子<input v-model.number="values.seed" aria-label="排版重复种子" type="number" min="0" max="4294967295" step="1" class="input-hard mt-1 min-h-11 w-full" :disabled="disabled"></label>
      <p class="mt-1 text-ink-muted">相同文字、字体、模板、参数和种子可复现相同效果；预览与批量共用此设置。自然排版的字距种子仍由上方控件决定。</p>
    </details>
    <p class="mt-2 text-xs text-ink-muted">V3 保留原词字号差异、两处 10 像素墨迹间距和原位置；空间不足时提示扩大文字区域或减小字号。先放大预览，再正式生成。</p>
    <p v-if="problem" role="alert" class="mt-2 text-xs font-bold text-danger">{{ problem }}</p>
  </section>
</template>
