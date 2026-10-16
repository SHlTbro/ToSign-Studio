<script setup lang="ts">
import { userError, userText } from '@/utils/userLanguage'
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { request, ApiRequestError } from '@/api/http'
import { ENDPOINT_RENDER_CAPABILITIES } from '@/api/endpoints'
import { useDraft } from '@/composables/useDraft'
import { inkBindingIdentity, inkHex, isInkPaletteId, validInkCapabilities, rgbFromHex } from '@/api/inkColor'
import type { LayoutProfile, RenderCapabilities, RenderPreferences, RenderSelection } from '@/types/renderControls'

const props = withDefaults(defineProps<{ disabled: boolean; scopeKey?: string }>(), { scopeKey: '' })
const emit = defineEmits<{ ready: [selection: RenderSelection | null, reason?: string]; changed: []; layout: [layout: LayoutProfile]; v3reset: [] }>()
interface StyleRow { style_id: number; name: string; source_type: string; enabled: boolean; status: string; model_version?: string; receipt_id?: string }
function styleOptionLabel(style: StyleRow): string {
  return style.source_type === 'M2_ADAPTER' && style.model_version === 'adapter@017881442b7f8b90'
    && style.receipt_id === 'imported_inference/16ec01496e0b472b9f561ce383a7ed33'
    ? '经典手写 V3' : `${style.name} · ${style.source_type}`
}
const initial: RenderPreferences = { styleId: '', sourcePolicy: 'fallback_allowed', layout: 'baseline-v1', seed: 42, inkMode: 'auto-contrast-v1', inkPaletteId: '' }
const hadSavedPreferences = localStorage.getItem(`draft:batch-render-preferences:${props.scopeKey}`) !== null
let v3DefaultsApplied = false
const { data: preferences } = useDraft<RenderPreferences>(`batch-render-preferences:${props.scopeKey}`, initial)
const invalidPreferences = ref(false)
let unreadablePreference = false
try { const raw = localStorage.getItem(`draft:batch-render-preferences:${props.scopeKey}`); if (raw !== null && JSON.parse(raw) === null) unreadablePreference = true }
catch { unreadablePreference = true }
if (unreadablePreference || !preferences.value || preferences.value.needsExplicitReset || typeof preferences.value.styleId !== 'string' || !/^(|[1-9]\d*)$/.test(preferences.value.styleId)
  || !['fallback_allowed', 'm2_required', 'm2_cjk_required'].includes(preferences.value.sourcePolicy) || !['baseline-v1', 'natural-layout-v1'].includes(preferences.value.layout)
  || !['number', 'string'].includes(typeof preferences.value.seed)
  || (preferences.value.inkMode !== undefined && !['auto-contrast-v1', 'source-solid-srgb-v1', 'custom-solid-srgb-v1'].includes(preferences.value.inkMode))
  || (preferences.value.inkHex !== undefined && typeof preferences.value.inkHex !== 'string')
  || (preferences.value.inkPaletteId !== undefined && preferences.value.inkPaletteId !== '' && !isInkPaletteId(preferences.value.inkPaletteId))
  || (preferences.value.inkPaletteIdentity !== undefined && typeof preferences.value.inkPaletteIdentity !== 'string')
  || (preferences.value.mixedFontsEnabled !== undefined && typeof preferences.value.mixedFontsEnabled !== 'boolean')
  || [preferences.value.latinFontId, preferences.value.otherFontId].some((id) => id !== undefined && (typeof id !== 'string' || !/^[A-Za-z0-9_.-]{0,128}$/.test(id)))
  || (preferences.value.inkMode === undefined && Boolean(preferences.value.inkPaletteId))) { invalidPreferences.value = true; preferences.value = { ...initial, needsExplicitReset: true } }
// R5 drafts have no ink fields. Missing fields mean the unchanged monochrome default only.
if (!invalidPreferences.value && preferences.value.inkMode === undefined) preferences.value.inkMode = 'auto-contrast-v1'
if (!invalidPreferences.value && preferences.value.inkHex === undefined) preferences.value.inkHex = '#000000'
const commonColors = [
  { name: '黑色', hex: '#000000' }, { name: '白色', hex: '#FFFFFF' }, { name: '红色', hex: '#D32F2F' },
  { name: '蓝色', hex: '#2455C7' }, { name: '紫色', hex: '#6B3CC9' }, { name: '绿色', hex: '#237A57' },
]
const customRgb = computed(() => rgbFromHex(preferences.value.inkHex))
const pickerHex = computed(() => customRgb.value ? inkHex(customRgb.value) : '#000000')
const customAvailable = computed(() => capabilities.value?.ink_colors?.custom_solid?.available === true)
function pickColor(event: Event): void { preferences.value.inkHex = (event.target as HTMLInputElement).value }
const styles = ref<StyleRow[]>([])
const stylesError = ref('')
const capabilities = ref<RenderCapabilities | null>(null)
const v3Available = computed(() => {
  const value = capabilities.value?.accepted_v3
  return value?.available === true && value.fonts.latin === 'preset-caveat-600' && value.fonts.other === 'preset-noto-cjk-sc-face2'
    && value.binding?.schema === 'accepted-v3-layout-binding/1'
    && value.binding.adapter_sha256 === '017881442b7f8b909f7d8d8c4c5e7ad1893d0caa3c3267ae3c793a9a96175f81'
    && hash(value.binding.profile_sha256) && hash(value.binding.implementation_sha256)
})
function applyV3(): void {
  if (!v3Available.value) return
  Object.assign(preferences.value, { sourcePolicy: 'm2_cjk_required', layout: 'baseline-v1', seed: 2026100401,
    mixedFontsEnabled: true, latinFontId: 'preset-caveat-600', otherFontId: 'preset-noto-cjk-sc-face2',
    inkMode: 'custom-solid-srgb-v1', inkHex: '#8C3044', inkPaletteId: '', inkPaletteIdentity: '' })
  v3DefaultsApplied = true
  emit('v3reset')
}
const capabilityError = ref('')
const loading = ref(false)
function pendingFontName(id: string): string {
  return ({ 'preset-caveat-600': 'Caveat 600', 'preset-noto-cjk-sc-face2': 'Noto 简体中文 face 2', 'preset-mengshen': 'MengShen' } as Record<string, string>)[id] ?? '已选字体'
}
let generation = 0
let disposed = false
const hash = (value: unknown) => typeof value === 'string' && /^[0-9a-f]{64}$/.test(value)
const mode = computed(() => preferences.value.styleId ? 'profile' : 'system')
const visibleStyles = computed(() => styles.value.filter((style) => style.status === 'approved' && style.enabled === true
  && ['M0_SYSTEM', 'SYSTEM_FONT', 'M2_ADAPTER'].includes(style.source_type)))
const missingSelection = computed(() => Boolean(preferences.value.styleId && !visibleStyles.value.some((style) => String(style.style_id) === preferences.value.styleId)))
const naturalCapability = computed(() => capabilities.value?.layouts.find((layout) => layout.id === 'natural-layout-v1'))
const inkCapabilities = computed(() => capabilities.value && validInkCapabilities(capabilities.value.ink_colors, capabilities.value, props.scopeKey)
  ? capabilities.value.ink_colors : null)
const inkProblem = computed(() => {
  if (!inkCapabilities.value) return capabilities.value?.ink_colors ? '服务端墨色能力身份不完整或不匹配，来源取色不可用' : '服务端尚未提供已核验的来源墨色'
  if (!inkCapabilities.value.source_solid.available) return inkCapabilities.value.source_solid.reason || '来源墨色尚未完成独立审核，当前不可用'
  return ''
})
const inkPalettes = computed(() => inkCapabilities.value?.source_solid.palettes ?? [])
const selectedInkPalette = computed(() => inkPalettes.value.find((palette) => palette.id === preferences.value.inkPaletteId))
const missingInkPalette = computed(() => Boolean(preferences.value.inkPaletteId && !selectedInkPalette.value))
function selectInkPalette(): void {
  preferences.value.inkPaletteIdentity = selectedInkPalette.value ? inkBindingIdentity(selectedInkPalette.value.binding) : ''
}
const mixedFontCatalog = computed(() => {
  const value = capabilities.value?.non_cjk_fonts
  if (!value || value.schema !== 'non-cjk-font-catalog/1' || !Array.isArray(value.fonts)
    || value.fonts.some((font) => !font || typeof font.id !== 'string' || !/^[A-Za-z0-9_.-]{1,128}$/.test(font.id)
      || typeof font.name !== 'string' || typeof font.available !== 'boolean' || !Array.isArray(font.roles)
      || font.roles.some((role) => !['latin', 'other'].includes(role)))
    || new Set(value.fonts.map((font) => font.id)).size !== value.fonts.length) return null
  return value
})
const latinFonts = computed(() => mixedFontCatalog.value?.fonts.filter((font) => font.roles.includes('latin')) ?? [])
const otherFonts = computed(() => mixedFontCatalog.value?.fonts.filter((font) => font.roles.includes('other')) ?? [])
const mixedFontProblem = computed(() => {
  if (!mixedFontCatalog.value) return '服务端未提供可核验的混合字体目录'
  if (!latinFonts.value.some((font) => font.id === preferences.value.latinFontId && font.available)) return '请明确选择可用的英文手写字体'
  if (!otherFonts.value.some((font) => font.id === preferences.value.otherFontId && font.available)) return '请明确选择可用的标点及其他非汉字字体'
  return ''
})
const problem = computed(() => {
  if (invalidPreferences.value) return '本机保存的字形或排版选择无法识别，请明确重新选择，当前不能生成'
  if (loading.value) return '正在核验所选风格的实际可用能力'
  if (capabilityError.value) return capabilityError.value
  const value = capabilities.value
  if (!value) return '尚未取得服务端风格能力，不能生成'
  if (!value.renderable) return value.reason || '所选风格当前不可渲染'
  if (!value.source_policies.includes(preferences.value.sourcePolicy)) return '所选字形来源要求当前不可用，请明确选择可用策略'
  if (['m2_required', 'm2_cjk_required'].includes(preferences.value.sourcePolicy)
    && (value.style_mode !== 'profile' || !Number.isSafeInteger(value.style_id) || (value.style_id ?? 0) < 1
      || value.source_type !== 'M2_ADAPTER' || !value.artifact)) return '仅使用 M2 需要已验证的个人适配器'
  if (preferences.value.sourcePolicy === 'm2_cjk_required' && preferences.value.mixedFontsEnabled && mixedFontProblem.value) return mixedFontProblem.value
  const layout = value.layouts.find((item) => item.id === preferences.value.layout)
  if (!layout?.available) return layout?.reason || '所选排版当前不可用，请明确选择可用排版'
  if (preferences.value.layout === 'natural-layout-v1') {
    if (preferences.value.sourcePolicy !== 'm2_required' || value.source_type !== 'M2_ADAPTER') return '自然排版需要明确选择「仅使用当前 M2」'
    if (!hash(layout.profile_sha256)) return '自然排版配置尚未冻结，暂不可用'
    const seed = preferences.value.seed
    if (seed === '' || typeof seed === 'string' && !/^\d+$/.test(seed) || !Number.isInteger(Number(seed)) || Number(seed) < 0 || Number(seed) > 4294967295) return '重复种子须为 0 到 4294967295 的整数'
  }
  if (preferences.value.inkMode === 'source-solid-srgb-v1') {
    if (inkProblem.value) return inkProblem.value
    if (preferences.value.layout !== 'natural-layout-v1' || preferences.value.sourcePolicy !== 'm2_required') return '来源墨色需要明确选择「自然手写排版」和「仅使用当前 M2」，不会自动更换'
    if (!selectedInkPalette.value) return '请明确选择一份已核验的训练原图墨色，不会自动选择调色板'
    if (preferences.value.inkPaletteIdentity !== inkBindingIdentity(selectedInkPalette.value.binding)) return '所选墨色身份尚未确认或已改变，请重新明确选择墨色'
  }
  if (preferences.value.inkMode === 'custom-solid-srgb-v1') {
    if (!customAvailable.value) return '当前服务端尚不支持自选字体颜色，请重新检查或保留原有颜色'
    if (!customRgb.value) return '字体颜色请输入 6 位 HEX，例如 #5B28C5；不支持透明度或简写'
  }
  return ''
})
const selection = computed<RenderSelection | null>(() => problem.value ? null : ({
  style_mode: mode.value,
  ...(mode.value === 'profile' ? { style_id: Number(preferences.value.styleId) } : {}),
  glyph_source_policy: preferences.value.sourcePolicy,
  ...(preferences.value.sourcePolicy === 'm2_cjk_required' && preferences.value.mixedFontsEnabled
    ? { non_cjk_fonts: { latin: preferences.value.latinFontId!, other: preferences.value.otherFontId! } } : {}),
  layout_profile: preferences.value.layout,
  ...(preferences.value.layout === 'natural-layout-v1' ? { layout_seed: Number(preferences.value.seed) } : {}),
  ...(preferences.value.inkMode === 'source-solid-srgb-v1' && selectedInkPalette.value
    ? { ink_color_mode: 'source-solid-srgb-v1' as const, ink_palette_id: selectedInkPalette.value.id } : {}),
  ...(preferences.value.inkMode === 'custom-solid-srgb-v1' && customRgb.value
    ? { ink_color_mode: 'custom-solid-srgb-v1' as const, ink_color_rgb: customRgb.value } : {}),
}))
function message(reason: unknown): string { return reason instanceof ApiRequestError ? userError(reason) : userText(reason instanceof Error ? reason.message : reason) }
async function loadCapabilities(): Promise<void> {
  const token = ++generation; const styleId = preferences.value.styleId
  capabilities.value = null; capabilityError.value = ''; loading.value = true
  try {
    const value = await request<RenderCapabilities>(ENDPOINT_RENDER_CAPABILITIES.path + (styleId ? `?style_id=${styleId}` : ''), { retries: 0, timeoutMs: styleId ? 600000 : 60000 })
    if (disposed || token !== generation) return
    if (!value || value.schema !== 'render-style-capabilities/1' || value.style_mode !== (styleId ? 'profile' : 'system')
      || value.style_id !== (styleId ? Number(styleId) : null) || typeof value.renderable !== 'boolean' || typeof value.font_ready !== 'boolean'
      || typeof value.source_type !== 'string' || !(value.reason === null || typeof value.reason === 'string')
      || !Array.isArray(value.source_policies) || value.source_policies.some((policy) => !['fallback_allowed', 'm2_required', 'm2_cjk_required'].includes(policy))
      || !Array.isArray(value.layouts) || new Set(value.layouts.map((layout) => layout.id)).size !== value.layouts.length
      || value.layouts.some((layout) => !['baseline-v1', 'natural-layout-v1'].includes(layout.id) || typeof layout.available !== 'boolean')
      || (value.artifact !== null && (!value.artifact || !value.artifact.artifact_id || !value.artifact.engine_version || !hash(value.artifact.manifest_sha256)))) throw new Error('服务端能力与所选风格不匹配，请刷新核验')
    capabilities.value = value
    if (!hadSavedPreferences && !v3DefaultsApplied && v3Available.value) applyV3()
  } catch (reason) { if (!disposed && token === generation) capabilityError.value = message(reason) }
  finally { if (!disposed && token === generation) loading.value = false }
}
async function loadStyles(): Promise<void> {
  const all: StyleRow[] = []; const ids = new Set<number>()
  try {
    for (let page = 1; ; page++) {
      const data = await request<{ items: StyleRow[] }>(`/api/v1/styles?status=approved&enabled=true&page=${page}&page_size=100`, { retries: 0 })
      if (disposed) return
      if (!Array.isArray(data.items) || data.items.some((style) => !Number.isSafeInteger(style.style_id) || style.style_id < 1 || ids.has(style.style_id))) throw new Error('风格分页重复或不完整，请刷新')
      for (const style of data.items) { ids.add(style.style_id); all.push(style) }
      if (data.items.length < 100) break
    }
    styles.value = all
  } catch (reason) { if (!disposed) stylesError.value = message(reason) }
}
watch(() => preferences.value.styleId, () => { emit('changed'); void loadCapabilities() }, { immediate: true, flush: 'sync' })
watch(() => [preferences.value.sourcePolicy, preferences.value.layout, preferences.value.seed, preferences.value.inkMode, preferences.value.inkHex, preferences.value.inkPaletteId, preferences.value.inkPaletteIdentity, preferences.value.mixedFontsEnabled, preferences.value.latinFontId, preferences.value.otherFontId], () => emit('changed'), { flush: 'sync' })
// Presentation-only signal: keep multiline editing available while capability/seed validation blocks submission.
watch(() => preferences.value.layout, (value) => emit('layout', value), { immediate: true, flush: 'sync' })
watch([selection, problem], ([value, reason]) => emit('ready', value, userText(reason)), { immediate: true, flush: 'sync' })
onMounted(() => { void loadStyles() })
onBeforeUnmount(() => { disposed = true; ++generation })
</script>

<template>
  <div class="motion-rise rounded-2xl border-2 border-ink bg-surface p-4 shadow-[3px_3px_0_var(--color-ink)]" data-testid="render-style-controls">
    <label for="render-style" class="mb-2 block text-sm font-black">02 · 字形来源、排版与墨色</label>
    <select id="render-style" v-model="preferences.styleId" class="input-hard min-h-11 w-full text-sm" :disabled="disabled">
      <option value="">系统字体 M0</option>
      <option v-if="missingSelection" :value="preferences.styleId" disabled>所选档案 #{{ preferences.styleId }} · 正在核验或列表中不可用</option>
      <option v-for="style in visibleStyles" :key="style.style_id" :value="String(style.style_id)">{{ styleOptionLabel(style) }}</option>
    </select>
    <p class="mt-2 text-[11px] text-ink-muted">系统字体使用明确的 M0 来源；个人风格以当前服务端核验结果为准。原图采集档案不能直接作为已训练模型。</p>
    <p v-if="stylesError" class="mt-2 text-[11px] text-danger">风格列表：{{ stylesError }}</p>
    <button v-if="v3Available" type="button" data-testid="restore-v3" class="mt-2 min-h-11 text-xs font-bold underline" :disabled="disabled || loading" @click="applyV3">恢复认可的 V3 字体、墨色与排版参数</button>
    <label class="mt-3 block text-xs font-bold">字形来源要求
      <select v-model="preferences.sourcePolicy" aria-label="字形来源要求" class="input-hard mt-1 min-h-11 w-full" :disabled="disabled || loading">
        <option value="fallback_allowed">允许回退，逐字显示实际来源</option>
        <option value="m2_required" :disabled="!capabilities?.source_policies.includes('m2_required')">仅使用当前 M2，失败则停止该项</option>
        <option value="m2_cjk_required" :disabled="!capabilities?.source_policies.includes('m2_cjk_required')">汉字仅使用当前 M2，其他字符使用 M0</option>
      </select>
    </label>
    <p v-if="preferences.sourcePolicy === 'm2_cjk_required'" class="mt-2 text-[11px] text-ink-muted" data-testid="cjk-source-policy-help">汉字（含〇及兼容表意文字）必须由当前个人适配器生成；拉丁字母、数字、标点（含全角冒号）等非汉字明确使用系统字体 M0。汉字失败会停止该项，不回退。此策略支持原有排版，可使用自动对比墨色或自定义单色。</p>
    <section v-if="preferences.sourcePolicy === 'm2_cjk_required'" class="mt-3 text-xs" data-testid="mixed-font-selection">
      <label class="flex min-h-11 items-center gap-2 font-bold"><input v-model="preferences.mixedFontsEnabled" type="checkbox" :disabled="disabled || loading" aria-label="分别指定英文和标点字体">分别指定英文和标点字体</label>
      <p class="text-ink-muted">中文必须由个人 M2 成功生成；英文可用通用手写字体，标点可用普通字体，均记录为 M0。英文不要求同一作者。未启用时保留原有共用字体。</p>
      <template v-if="preferences.mixedFontsEnabled">
        <label class="mt-2 block font-bold">英文手写字体（拉丁字母）
          <select v-model="preferences.latinFontId" aria-label="英文手写字体" class="input-hard mt-1 min-h-11 w-full" :disabled="disabled || loading">
            <option value="">请明确选择</option>
            <option v-if="preferences.latinFontId && !latinFonts.some((font) => font.id === preferences.latinFontId)" :value="preferences.latinFontId" disabled>{{ pendingFontName(preferences.latinFontId) }} · {{ loading || !capabilities ? '待核验' : '当前不可用' }}</option>
            <option v-for="font in latinFonts" :key="font.id" :value="font.id" :disabled="!font.available">{{ font.name }}{{ font.available ? '' : ' · 未安装' }}</option>
          </select>
        </label>
        <label class="mt-2 block font-bold">标点及其他非汉字字体
          <select v-model="preferences.otherFontId" aria-label="标点及其他字体" class="input-hard mt-1 min-h-11 w-full" :disabled="disabled || loading">
            <option value="">请明确选择</option>
            <option v-if="preferences.otherFontId && !otherFonts.some((font) => font.id === preferences.otherFontId)" :value="preferences.otherFontId" disabled>{{ pendingFontName(preferences.otherFontId) }} · {{ loading || !capabilities ? '待核验' : '当前不可用' }}</option>
            <option v-for="font in otherFonts" :key="font.id" :value="font.id" :disabled="!font.available">{{ font.name }}{{ font.available ? '' : ' · 未安装' }}</option>
          </select>
        </label>
        <p class="mt-2 text-ink-muted">全角冒号保留原字符。字体、字重和集合 face 与文件校验一同冻结到任务；不可用或字形缺失时停止。V3 使用 Caveat 600 与 Noto face 2。</p>
      </template>
    </section>
    <label class="mt-3 block text-xs font-bold">排版方式
      <select v-model="preferences.layout" aria-label="排版方式" class="input-hard mt-1 min-h-11 w-full" :disabled="disabled || loading">
        <option value="baseline-v1">原有排版</option>
        <option value="natural-layout-v1" :disabled="naturalCapability?.available !== true">自然手写排版 · 保留轻微不均匀</option>
      </select>
    </label>
    <p v-if="!loading && capabilities && naturalCapability?.available !== true" class="mt-1 text-[11px] text-amber">自然排版：{{ userText(naturalCapability?.reason ?? '服务端尚未提供可用能力') }}</p>
    <template v-if="preferences.layout === 'natural-layout-v1'">
      <label class="mt-3 block text-xs font-bold">重复种子
        <input v-model="preferences.seed" aria-label="自然排版重复种子" type="number" min="0" max="4294967295" step="1" class="input-hard mt-1 min-h-11 w-full" :disabled="disabled">
      </label>
      <p class="mt-1 text-[11px] text-ink-muted">同一输入、模型、排版配置和种子可复现相同结果。轻微间距、大小与落点变化由已冻结的服务端配置控制；不会把回退字体称为个人手写。</p>
    </template>
    <label class="mt-3 block text-xs font-bold">字体颜色
      <select v-model="preferences.inkMode" aria-label="墨色方式" class="input-hard mt-1 min-h-11 w-full" :disabled="disabled || loading">
        <option value="auto-contrast-v1">原有自动对比单色 · 默认</option>
        <option value="custom-solid-srgb-v1" :disabled="!customAvailable">自选单色 · 常用色或自定义</option>
        <option value="source-solid-srgb-v1" :disabled="Boolean(inkProblem)">训练原图取色 · 明确选择</option>
      </select>
    </label>
    <div v-if="preferences.inkMode === 'custom-solid-srgb-v1'" class="mt-3 space-y-3" data-testid="custom-font-color">
      <div class="grid grid-cols-3 gap-2 sm:grid-cols-6" aria-label="常用字体颜色">
        <button v-for="color in commonColors" :key="color.hex" type="button" :aria-label="`字体颜色：${color.name}`" :aria-pressed="!!customRgb && inkHex(customRgb) === color.hex" :disabled="disabled || loading || !customAvailable" class="flex min-h-11 items-center justify-center gap-1 rounded-lg border border-ink text-xs font-bold" @click="preferences.inkHex = color.hex"><span class="h-4 w-4 shrink-0 rounded-full border border-ink" aria-hidden="true" :style="{ backgroundColor: color.hex }"></span>{{ color.name }}</button>
      </div>
      <div class="grid gap-3 sm:grid-cols-2">
        <label class="text-xs font-bold">自定义取色<input type="color" aria-label="自定义字体取色" :value="pickerHex" :disabled="disabled || loading || !customAvailable" class="mt-1 min-h-11 w-full rounded-lg border border-ink" @input="pickColor"></label>
        <label class="text-xs font-bold">HEX 颜色值<input v-model="preferences.inkHex" type="text" aria-label="字体颜色 HEX" maxlength="9" placeholder="#5B28C5" spellcheck="false" :disabled="disabled || loading || !customAvailable" class="input-hard mt-1 min-h-11 w-full uppercase"></label>
      </div>
      <p class="text-xs text-ink-muted">只改变生成文字的颜色，保留透明边缘和字形；无需重新训练。先预览，再正式生成。已创建任务继续使用当时的颜色。</p>
    </div>
    <p class="mt-1 text-[11px] text-ink-muted">默认保留原有单色效果。来源墨色只改变已排好字形的 RGB，不改变透明度、笔画、排版或种子。</p>
    <p v-if="!loading && capabilities && inkProblem" class="mt-1 text-[11px] text-amber" data-testid="ink-capability-warning">来源墨色：{{ userText(inkProblem) }}</p>
    <template v-if="preferences.inkMode === 'source-solid-srgb-v1'">
      <label class="mt-3 block text-xs font-bold">来源墨色
        <select v-model="preferences.inkPaletteId" aria-label="来源墨色" class="input-hard mt-1 min-h-11 w-full" :disabled="disabled || loading || Boolean(inkProblem)" @change="selectInkPalette">
          <option value="">请明确选择训练原图墨色</option>
          <option v-if="missingInkPalette" :value="preferences.inkPaletteId" disabled>{{ preferences.inkPaletteId }} · 当前不可用</option>
          <option v-for="palette in inkPalettes" :key="palette.id" :value="palette.id">{{ palette.label }} · {{ palette.id }} · {{ inkHex(palette.rgb) }}</option>
        </select>
      </label>
      <div v-if="selectedInkPalette" class="mt-2 text-[11px]" data-testid="selected-ink-identity">
        <p class="flex items-center gap-2"><span aria-hidden="true" class="inline-block h-5 w-5 shrink-0 rounded border border-ink" :style="{ backgroundColor: inkHex(selectedInkPalette.rgb) }"></span>{{ selectedInkPalette.id }} · RGB {{ selectedInkPalette.rgb.join(', ') }} · {{ inkHex(selectedInkPalette.rgb) }}</p>
        <p class="break-all">原图校验：{{ selectedInkPalette.binding.source_image_sha256 }}</p>
        <p class="break-all">墨色配置校验：{{ selectedInkPalette.binding.profile_sha256 }}</p>
        <p>绑定档案 #{{ selectedInkPalette.binding.profile_id }} · 模型 #{{ selectedInkPalette.binding.artifact_id }}</p>
      </div>
      <p class="mt-2 text-xs font-bold text-amber" data-testid="ink-visibility-warning">来自训练原图照片的表观墨色，按 sRGB 假设处理，不代表恢复了真实颜料或透明度。不同底图局部可见度会变化；请放大检查实际照片预览。不会自动改色或宣称可读性已通过。</p>
    </template>
    <p v-if="loading" class="mt-2 text-xs text-ink-muted" role="status" data-testid="capability-pending">正在核验模型与字体，请稍候</p>
    <p v-else-if="problem" class="mt-2 text-xs font-bold text-danger" role="alert">{{ userText(problem) }}</p>
    <button v-if="invalidPreferences" type="button" class="mt-1 min-h-11 text-xs font-bold underline" :disabled="disabled" @click="preferences = { ...initial }; invalidPreferences = false">重新选择系统字体与原有排版</button>
    <p v-if="!problem" class="mt-2 text-xs font-bold text-emerald-800">已核验所选来源与排版能力；实际逐字推理结果以任务记录为准</p>
    <button type="button" class="mt-1 min-h-11 text-xs font-bold underline" :disabled="disabled || loading" @click="loadCapabilities">重新核验所选风格</button>
  </div>
</template>
