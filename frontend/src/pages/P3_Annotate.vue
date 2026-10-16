<!-- src/pages/P3_Annotate.vue -->
<script setup lang="ts">
import { userError } from '@/utils/userLanguage'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import type { Region, Slot, SlotKey, TextStyle } from '@/types/domain'
import { ApiRequestError, request } from '@/api/http'
import { useAuthStore } from '@/stores/auth'
import LayoutTemplateAdmission from '@/components/LayoutTemplateAdmission.vue'
import { aspectRatioValue, orientationOf } from '@/utils/aspect'
import { useDraft } from '@/composables/useDraft'

const router = useRouter()
const route = useRoute()
const auth = useAuthStore()
let disposed = false
let assetGeneration = 0
const saveError = ref('')
const saveUncertain = ref(false)
const saveReconciled = ref(false)
const verifiedTemplateId = ref<string | null>(null)
const pendingSaveMessage = '保存结果尚未确认，请先查找本图已保存模板，避免重复创建'
function saveKey(): string { return `yy:p3:pending-template:${auth.me?.user_id ?? 'signed-out'}:${String(route.params.id ?? '')}` }
function clearPendingSave(): void {
  if (!verifiedTemplateId.value && !saveReconciled.value) return
  try { sessionStorage.removeItem(saveKey()); saveUncertain.value = false; saveReconciled.value = false; saveError.value = '' }
  catch { saveError.value = '无法保存恢复状态，请检查浏览器会话存储后重试' }
}

// ---------- 初始槽位：mock 模板提供 to_cn/blessing，按公理 A2 补 signature 第三槽 ----------

const DEFAULT_TEXT_STYLE: TextStyle = { ordinary_scale: 1.3, to_ratio: 2.0, header_gap: 10 }

function buildInitialSlots(): Slot[] {
  // 公理 A2：三槽静态默认（To 名称/祝福必备，签名可选），加载真图后由博主拖拽微调
  return [
    { key: 'to_cn', text_style: { ...DEFAULT_TEXT_STYLE }, text_template: 'To {cn}', region: { x_norm: 0.1, y_norm: 0.12, w_norm: 0.5, h_norm: 0.12 } },
    { key: 'blessing', text_style: { ...DEFAULT_TEXT_STYLE }, text_template: '{blessing}', region: { x_norm: 0.12, y_norm: 0.28, w_norm: 0.6, h_norm: 0.14 } },
    { key: 'signature', text_style: { ...DEFAULT_TEXT_STYLE }, text_template: '{cn}', region: { x_norm: 0.62, y_norm: 0.78, w_norm: 0.24, h_norm: 0.1 } },
  ]
}

const INITIAL_SLOTS = buildInitialSlots()
const draftKey = computed(() => `annotate-slots-draft:${String(route.params.id ?? '')}`)

// R2 草稿：500ms 防抖落盘 + pagehide 立即 flush（useDraft 内部已实现）
const { data: slots, commit, resume, hasDraft } = useDraft<Slot[]>(draftKey, INITIAL_SLOTS)

const SLOT_META: Record<SlotKey, { label: string; tool: string; border: string; bg: string; tag: string }> = {
  to_cn: {
    label: 'To 签名称框',
    tool: '画名称',
    border: 'border-primary',
    bg: 'bg-primary/10',
    tag: 'bg-primary text-white',
  },
  blessing: {
    label: '祝福语框',
    tool: '画祝福',
    border: 'border-amber',
    bg: 'bg-amber/10',
    tag: 'bg-amber text-ink',
  },
  signature: {
    label: '个人签名框',
    tool: '画签名',
    border: 'border-emerald-600',
    bg: 'bg-emerald-600/10',
    tag: 'bg-emerald-600 text-white',
  },
}

const activeTool = ref<SlotKey>('to_cn')
const showSaveSheet = ref(false)
const saving = ref(false)
const savedToast = ref(false)

// ---------- 撤销栈（仅记录拖拽前快照） ----------

const history = ref<Slot[][]>([])
function snapshot(): void {
  resume()
  history.value.push(slots.value.map((s) => ({ ...s, region: { ...s.region }, text_style: s.text_style ? { ...s.text_style } : undefined })))
  if (history.value.length > 30) history.value.shift()
}
function undo(): void {
  const prev = history.value.pop()
  if (prev) { slots.value = prev; resume() }
}
function resetSlots(): void {
  slots.value = buildInitialSlots().map((s) => ({ ...s, region: { ...s.region }, text_style: s.text_style ? { ...s.text_style } : undefined }))
  history.value = []
  activeTool.value = 'to_cn'
  sizeError.value = ''
  resume()
}

// 公理 A2：To 名称/祝福为必备槽，个人签名为可选槽，可添加或移除
const hasSignature = computed(() => slots.value.some((s) => s.key === 'signature'))
function selectTool(key: SlotKey): void {
  if (key === 'signature' && !hasSignature.value) {
    snapshot()
    slots.value.push({
      key: 'signature',
      text_style: { ...DEFAULT_TEXT_STYLE },
      text_template: '{cn}',
      region: { x_norm: 0.62, y_norm: 0.78, w_norm: 0.24, h_norm: 0.1 },
    })
  }
  activeTool.value = key
}
function removeSignature(): void {
  snapshot()
  slots.value = slots.value.filter((s) => s.key !== 'signature')
  if (activeTool.value === 'signature') activeTool.value = 'to_cn'
}

// ---------- 指针拖拽：像素位移换算为归一化坐标，钳制在 [0,1] ----------

const canvasRef = ref<HTMLDivElement | null>(null)
const displayCanvasWidth = ref(300)
let canvasObserver: ResizeObserver | null = null
watch(canvasRef, (element) => {
  canvasObserver?.disconnect()
  if (!element) return
  const update = (): void => { displayCanvasWidth.value = element.clientWidth || 300 }
  update()
  if (typeof ResizeObserver !== 'undefined') {
    canvasObserver = new ResizeObserver(update)
    canvasObserver.observe(element)
  }
})
interface DragState {
  key: SlotKey
  pointerId: number
  startX: number
  startY: number
  origin: Region
}
const drag = ref<DragState | null>(null)

const clamp01 = (v: number): number => Math.min(1, Math.max(0, v))

function onSlotPointerDown(e: PointerEvent, slot: Slot): void {
  if (!canvasRef.value) return
  activeTool.value = slot.key
  snapshot()
  drag.value = {
    key: slot.key,
    pointerId: e.pointerId,
    startX: e.clientX,
    startY: e.clientY,
    origin: { ...slot.region },
  }
  ;(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId)
  e.preventDefault()
}

function onPointerMove(e: PointerEvent): void {
  const d = drag.value
  const canvas = canvasRef.value
  if (!d || !canvas) return
  const rect = canvas.getBoundingClientRect()
  const target = slots.value.find((s) => s.key === d.key)
  if (!target) return
  const dx = (e.clientX - d.startX) / rect.width
  const dy = (e.clientY - d.startY) / rect.height
  target.region.x_norm = clamp01(d.origin.x_norm + dx)
  target.region.y_norm = clamp01(d.origin.y_norm + dy)
}

function endDrag(e: PointerEvent): void {
  if (!drag.value) return
  try {
    canvasRef.value?.releasePointerCapture(e.pointerId)
  } catch {
    /* 指针已释放 */
  }
  drag.value = null
}

// ---------- 保存抽屉与 [0,1] 校验 ----------

const sizeError = ref('')
const restoringTemplate = ref(false)
const ordinaryScale = computed(() => slots.value[0]?.text_style?.ordinary_scale ?? 1.3)
const toRatio = computed(() => slots.value.find(s => s.key === 'to_cn')?.text_style?.to_ratio ?? 2)
const preview = ref({ cn: '', blessing: '', blogger: '' })
function changeSize(field: 'ordinary_scale' | 'to_ratio', event: Event): void {
  const raw = (event.target as HTMLInputElement).value
  const value = Number(raw)
  if (!raw.trim() || !Number.isFinite(value) || value < 0.5 || value > 3) {
    sizeError.value = '请输入 0.5 到 3 之间的字号比例'
    return
  }
  snapshot()
  for (const slot of slots.value) slot.text_style = { ...DEFAULT_TEXT_STYLE, ...slot.text_style, [field]: value }
  sizeError.value = ''
  // Resume an explicit edit after a successful save without reviving the committed draft automatically.
  resume()
}
function previewText(slot: Slot): string {
  return slot.text_template.replace(/\{cn\}/g, preview.value.cn || '昵称')
    .replace(/\{blessing\}/g, preview.value.blessing || '祝福语')
    .replace(/\{blogger\}/g, preview.value.blogger || '签名')
}
function previewParts(slot: Slot): { prefix: string; rest: string } {
  const text = previewText(slot)
  const match = slot.key === 'to_cn' ? /^(To(?=\s|[:：]|$)(?:[:：])?)\s*/i.exec(text) : null
  return match ? { prefix: match[1] ?? '', rest: text.slice(match[0].length) } : { prefix: '', rest: text }
}
function previewGap(): number {
  return 10 * displayCanvasWidth.value / (assetMeta.value?.width || 1200)
}
function previewPx(slot: Slot): number {
  const width = displayCanvasWidth.value
  const ratio = assetMeta.value ? assetMeta.value.height / assetMeta.value.width : 4 / 3
  const height = slot.region.h_norm * width * ratio
  const parts = previewParts(slot)
  const toSize = parts.prefix ? slot.text_style?.to_ratio ?? 2 : 1
  const units = Array.from(parts.rest).reduce((sum, ch) => sum + ((ch.codePointAt(0) ?? 0) > 255 ? 1 : 0.65), 0)
    + parts.prefix.length * 0.7 * toSize
  const requested = height * 0.6 * (slot.text_style?.ordinary_scale ?? 1.3)
  // Browser font preview fits its slot; exported pixels use the backend ink bounding boxes.
  return Math.max(1, Math.min(requested, Math.max(1, height - 8) / Math.max(1, toSize),
    Math.max(1, slot.region.w_norm * width - 12 - (parts.prefix ? previewGap() : 0)) / Math.max(1, units)))
}

const normalizedJson = computed(() => JSON.stringify(slots.value, null, 2))
const allInRange = computed(() =>
  slots.value.every(
    (s) =>
      [s.region.x_norm, s.region.y_norm, s.region.w_norm, s.region.h_norm].every(
        (v) => v >= 0 && v <= 1,
      ),
  ),
)

const closeButton = ref<HTMLButtonElement | null>(null)
watch(showSaveSheet, async (open) => {
  if (typeof document !== 'undefined') document.body.style.overflow = open ? 'hidden' : ''
  if (open) {
    await nextTick()
    closeButton.value?.focus()
  }
})
function onKeydown(e: KeyboardEvent): void {
  if (e.key === 'Escape') showSaveSheet.value = false
}
function closeSheet(): void {
  if (saving.value) return
  showSaveSheet.value = false
}
async function confirmSave(): Promise<void> {
  if (saving.value || restoringTemplate.value || Boolean(sizeError.value) || saveUncertain.value || !allInRange.value || !assetId.value || assetError.value || !auth.me) return
  saving.value = true
  saveError.value = ''
  const generation = assetGeneration
  const sourceId = assetId.value
  const sourceRoute = route.fullPath
  const key = saveKey()
  let submitted = false
  try {
    sessionStorage.setItem(key, JSON.stringify({ version: 1, started_at: new Date().toISOString() }))
    submitted = true
    const created = await request<{ template_id: string }>('/api/v1/layout-templates', {
      method: 'POST',
      body: {
        name: `布局模板 ${new Date().toISOString().slice(0, 10)}`,
        base_asset_id: sourceId,
        slots: slots.value,
        canvas_width: assetMeta.value?.width ?? 0,
        canvas_height: assetMeta.value?.height ?? 0,
      },
    })
    sessionStorage.removeItem(key)
    if (disposed || generation !== assetGeneration) return
    saveUncertain.value = false
    if (route.fullPath !== sourceRoute) return
    commit()
    showSaveSheet.value = false
    await router.replace({ path: route.path, query: { ...route.query, template_id: created.template_id } })
    savedToast.value = true
    window.setTimeout(() => {
      savedToast.value = false
    }, 1800)
  } catch (e) {
    if (disposed || generation !== assetGeneration) return
    saveUncertain.value = submitted && (!(e instanceof ApiRequestError) || e.status === 0 || e.status >= 500)
    if (!saveUncertain.value) { try { sessionStorage.removeItem(key) } catch { saveUncertain.value = true } }
    saveError.value = saveUncertain.value ? pendingSaveMessage : !submitted ? '无法保存恢复状态，尚未发送保存请求，请检查浏览器会话存储'
      : e instanceof ApiRequestError ? userError(e) : e instanceof Error && e.message ? e.message : '布局模板保存失败'
  } finally {
    if (!disposed) saving.value = false
  }
}
onMounted(() => window.addEventListener('keydown', onKeydown))
onBeforeUnmount(() => {
  canvasObserver?.disconnect()
  disposed = true; assetGeneration += 1
  window.removeEventListener('keydown', onKeydown)
  if (typeof document !== 'undefined') document.body.style.overflow = ''
})

const assetId = computed(() => String(route.params.id ?? ''))
// 真资产元数据：P3 只消费 width/height（比例与方向），加载失败降级 3:4 占位
interface AssetMeta { asset_id: string; width: number; height: number }
const assetMeta = ref<AssetMeta | null>(null)
const assetError = ref(false)
const assetImageUrl = computed(() =>
  assetId.value ? `/api/v1/assets/${assetId.value}/file` : '',
)
const canvasAspect = computed(() =>
  assetMeta.value
    ? aspectRatioValue(assetMeta.value.width, assetMeta.value.height)
    : '3 / 4',
)
const canvasIsLandscape = computed(
  () =>
    assetMeta.value != null &&
    orientationOf(assetMeta.value.width, assetMeta.value.height) === 'landscape',
)

watch(() => [assetId.value, auth.me?.user_id], async () => {
  const generation = ++assetGeneration
  assetMeta.value = null; assetError.value = false; saveError.value = ''; verifiedTemplateId.value = null
  saveReconciled.value = false; showSaveSheet.value = false
  try { saveUncertain.value = sessionStorage.getItem(saveKey()) != null; if (saveUncertain.value) saveError.value = pendingSaveMessage }
  catch { saveUncertain.value = true; saveError.value = '无法读取保存恢复状态，请检查浏览器会话存储' }
  if (!assetId.value) {
    assetError.value = true
    return
  }
  try {
    const meta = await request<AssetMeta>(`/api/v1/assets/${assetId.value}`)
    if (!disposed && generation === assetGeneration) assetMeta.value = meta
    if (!disposed && generation === assetGeneration && !hasDraft()) {
      restoringTemplate.value = true
      try {
        const templateId = typeof route.query.template_id === 'string' ? route.query.template_id : ''
        const previous = templateId
          ? await request<{ base_asset_id: string; slots: Slot[] }>(`/api/v1/layout-templates/${encodeURIComponent(templateId)}`)
          : (await request<{ items: { base_asset_id: string; slots: Slot[] }[] }>('/api/v1/layout-templates?page_size=100')).items.find(t => t.base_asset_id === assetId.value)
        if (previous && previous.base_asset_id !== assetId.value) throw new Error('Template belongs to a different asset')
        if (previous && !disposed && generation === assetGeneration && !hasDraft()) {
          slots.value = previous.slots.map(s => ({ ...s, region: { ...s.region }, text_style: { ...DEFAULT_TEXT_STYLE, ...s.text_style } }))
        }
      } catch { if (!disposed && generation === assetGeneration) sizeError.value = '读取已保存字号失败，请刷新后重试' }
      finally { if (!disposed && generation === assetGeneration) restoringTemplate.value = false }
    }
  } catch {
    if (!disposed && generation === assetGeneration) assetError.value = true
  }
}, { immediate: true })
</script>

<template>
  <main
    class="bg-blueprint flex h-[100dvh] flex-col overflow-hidden text-ink select-none"
    @pointermove="onPointerMove"
    @pointerup="endDrag"
    @pointercancel="endDrag"
  >
    <h1 class="sr-only">落点标注工作台</h1>

    <header
      class="safe-t z-20 flex h-14 shrink-0 items-center justify-between gap-2 border-b border-ink/15 px-3"
    >
      <button
        type="button"
        aria-label="返回资产台"
        class="btn-hard btn-hard--compact !rounded-full !px-3"
        @click="router.push('/assets')"
      >
        <svg aria-hidden="true" class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">
          <path d="M19 12H5"></path>
          <path d="m12 19-7-7 7-7"></path>
        </svg>
      </button>
      <div class="flex min-w-0 flex-col items-center">
        <p class="truncate text-sm font-black">落点标注工作台</p>
        <span class="mt-0.5 inline-flex items-center gap-1 rounded-full border-2 border-ink bg-mint px-2 py-px text-[10px] font-black">
          <span aria-hidden="true" class="h-1.5 w-1.5 rounded-full bg-ink"></span>
          全程人工 · 零 AI 参与
        </span>
      </div>
      <button type="button" class="btn-hard btn-hard--primary btn-hard--compact" @click="showSaveSheet = true">
        保存模板
      </button>
    </header>

    <LayoutTemplateAdmission :asset-id="assetId" @verified="verifiedTemplateId = $event" />
    <div v-if="saveError || saveUncertain" class="shrink-0 space-y-2 border-b border-coral bg-blush p-3 text-xs">
      <p v-if="saveError" role="alert" class="font-bold text-danger">{{ saveError }}</p>
      <template v-if="saveUncertain">
        <button v-if="verifiedTemplateId" type="button" class="btn-hard btn-hard--compact" :disabled="saving" @click="clearPendingSave">使用当前已保存模板结束未确认状态</button>
        <template v-else><label class="flex gap-2"><input v-model="saveReconciled" type="checkbox" aria-label="已检查模板并决定另存" />我已检查本图模板，决定允许另存新模板；原请求若已保存，可能产生重复模板。</label><button type="button" class="btn-hard btn-hard--compact" :disabled="saving || !saveReconciled" @click="clearPendingSave">清除未确认状态，允许另存</button></template>
      </template>
    </div>

    <!-- 标注主画布 -->
    <section class="flex flex-1 flex-col gap-3 overflow-hidden p-4">
      <div class="flex items-center justify-between">
        <p class="font-mono text-[10px] font-black uppercase tracking-[0.25em] text-ink-muted">
          Manual Layout / Normalized
        </p>
        <span v-if="hasDraft()" class="tag-code !border-amber !bg-amber/20">draft recovered</span>
      </div>

      <div
        class="relative flex flex-1 touch-none items-center justify-center overflow-hidden rounded-2xl border-2 border-ink bg-canvas p-3"
        style="background-image: radial-gradient(circle, rgba(26,26,26,0.10) 1px, transparent 1px); background-size: 18px 18px;"
      >
        <!-- 原图白卡：比例严格等于 Asset.width/height（9:16/3:4/1:1/横图均不拉伸），即归一化坐标系 -->
        <div
          ref="canvasRef"
          :style="{ aspectRatio: canvasAspect }"
          :class="[
            'relative max-h-full max-w-full border-2 border-ink bg-surface shadow-[3px_3px_0_var(--color-ink)]',
            canvasIsLandscape ? 'w-full' : 'h-full',
          ]"
        >
          <!-- 底图：真资产原图（P3 接线 T58 Phase 2）；加载失败退回线描占位 -->
          <img
            v-if="assetImageUrl && !assetError"
            :src="assetImageUrl"
            alt=""
            class="absolute inset-0 h-full w-full rounded-xl object-cover"
            draggable="false"
          >
          <!-- 底图线描占位（无资产/加载失败时） -->
          <svg aria-hidden="true" class="absolute inset-0 m-auto max-h-2/5 max-w-2/5 text-ink/15" viewBox="0 0 100 100" fill="none" stroke="currentColor" stroke-width="3">
            <circle cx="50" cy="38" r="17"></circle>
            <path d="M18 92c4-20 17-30 32-30s28 10 32 30" stroke-linecap="round"></path>
          </svg>

          <!-- 三个槽位框（可拖拽） -->
          <div
            v-for="slot in slots"
            :key="slot.key"
            :class="[
              'absolute cursor-move rounded-lg border-2 border-dashed p-1 transition-shadow duration-150',
              SLOT_META[slot.key].border,
              SLOT_META[slot.key].bg,
              activeTool === slot.key
                ? 'ring-2 ring-ink ring-offset-1 shadow-[2px_2px_0_var(--color-ink)]'
                : 'hover:shadow-[2px_2px_0_var(--color-ink)]',
              drag?.key === slot.key ? 'touch-none' : '',
            ]"
            :style="{
              left: `${slot.region.x_norm * 100}%`,
              top: `${slot.region.y_norm * 100}%`,
              width: `${slot.region.w_norm * 100}%`,
              height: `${slot.region.h_norm * 100}%`,
            }"
            role="button"
            tabindex="0"
            :aria-label="`${SLOT_META[slot.key].label}，可拖动调整位置`"
            :aria-pressed="activeTool === slot.key"
            @pointerdown="onSlotPointerDown($event, slot)"
            @focus="activeTool = slot.key"
          >
            <span
              :class="[
                'pointer-events-none absolute -top-2.5 left-1 rounded-md border border-ink px-1 py-px text-[9px] font-black',
                SLOT_META[slot.key].tag,
              ]"
            >{{ SLOT_META[slot.key].label }}</span>
            <div class="pointer-events-none flex h-full w-full items-center overflow-hidden whitespace-nowrap leading-none" data-testid="slot-text-preview" :style="{ fontSize: `${previewPx(slot)}px`, gap: `${previewParts(slot).prefix ? previewGap() : 0}px` }">
              <span v-if="previewParts(slot).prefix" :style="{ fontSize: `${slot.text_style?.to_ratio ?? 2}em` }">{{ previewParts(slot).prefix }}</span>
              <span>{{ previewParts(slot).rest }}</span>
            </div>
            <span class="sr-only">
              [{{ slot.region.x_norm.toFixed(2) }}, {{ slot.region.y_norm.toFixed(2) }},
              {{ slot.region.w_norm.toFixed(2) }}, {{ slot.region.h_norm.toFixed(2) }}]
            </span>
          </div>

          <span class="pointer-events-none absolute bottom-1.5 right-2 font-mono text-[9px] font-black tracking-widest text-ink/40">
            {{ assetId }} · 3:4 CANVAS
          </span>
        </div>
      </div>
    </section>

    <section class="shrink-0 space-y-2 border-t border-ink/15 bg-surface px-3 py-2" aria-label="文字字号设置">
      <div class="grid grid-cols-2 gap-3">
        <label class="text-xs font-bold">普通文字比例
          <input type="range" min="0.5" max="3" step="0.1" :value="ordinaryScale" aria-label="普通文字比例滑块" class="w-full" @input="changeSize('ordinary_scale', $event)">
          <input type="number" min="0.5" max="3" step="0.1" :value="ordinaryScale" aria-label="普通文字比例" class="w-full rounded border border-ink/30 px-2 py-1" @input="changeSize('ordinary_scale', $event)">
        </label>
        <label class="text-xs font-bold">To 相对字号
          <input type="range" min="0.5" max="3" step="0.1" :value="toRatio" aria-label="To 相对字号滑块" class="w-full" @input="changeSize('to_ratio', $event)">
          <input type="number" min="0.5" max="3" step="0.1" :value="toRatio" aria-label="To 相对字号" class="w-full rounded border border-ink/30 px-2 py-1" @input="changeSize('to_ratio', $event)">
        </label>
      </div>
      <div class="flex gap-2">
        <input v-model="preview.cn" aria-label="预览昵称" placeholder="预览昵称" class="min-w-0 flex-1 rounded border px-2 py-1 text-xs">
        <input v-model="preview.blessing" aria-label="预览祝福语" placeholder="预览祝福语" class="min-w-0 flex-1 rounded border px-2 py-1 text-xs">
      </div>
      <p class="text-[10px] text-ink-muted">实时尺寸示意；导出使用已保存字号和真实字形，长文本会适配区域。To 比例只影响 To 前缀。</p>
      <p v-if="sizeError" role="alert" class="text-xs text-danger">{{ sizeError }}</p>
    </section>

    <!-- 底部工具栏：两行结构，避免工具与撤销/重置挤压重叠 -->
    <footer class="safe-b z-30 shrink-0 space-y-2 border-t-2 border-ink bg-surface px-3 py-2.5">
      <div class="flex gap-2">
        <button
          v-for="key in ['to_cn', 'blessing', 'signature'] as SlotKey[]"
          :key="key"
          type="button"
          class="pill-hard min-h-[44px] flex-1 !shadow-none text-xs"
          :class="[
            activeTool === key
              ? '!bg-ink !text-white'
              : key === 'signature' && !hasSignature
                ? 'border-dashed !bg-surface'
                : '!bg-surface',
          ]"
          :aria-pressed="activeTool === key"
          @click="selectTool(key)"
        >
          <span v-if="key === 'signature' && !hasSignature" aria-hidden="true" class="mr-0.5">+</span>
          {{ SLOT_META[key].tool }}
          <span v-if="key === 'signature'" class="ml-1 font-mono text-[9px] opacity-60">{{ hasSignature ? '可选' : '添加' }}</span>
        </button>
      </div>
      <div class="flex items-center justify-between gap-2">
        <p class="font-mono text-[10px] font-bold text-ink-muted">
          当前：{{ SLOT_META[activeTool].label }}
          <span v-if="activeTool === 'signature'" class="text-ink-muted">（可选槽位）</span>
        </p>
        <div class="flex gap-2">
          <button
            v-if="activeTool === 'signature' && hasSignature"
            type="button"
            class="btn-hard btn-hard--compact !min-h-[44px] !rounded-full !px-3 !text-danger"
            aria-label="移除个人签名框"
            @click="removeSignature"
          >
            <svg aria-hidden="true" class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M3 6h18"></path>
              <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
            </svg>
            移除签名
          </button>
          <button
            type="button"
            class="btn-hard btn-hard--compact !rounded-full !px-3"
            aria-label="撤销上一步"
            :disabled="history.length === 0"
            @click="undo"
          >
            <svg aria-hidden="true" class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M9 14 4 9l5-5"></path>
              <path d="M4 9h10.5a5.5 5.5 0 0 1 5.5 5.5 5.5 5.5 0 0 1-5.5 5.5H11"></path>
            </svg>
            撤销
          </button>
          <button
            type="button"
            class="btn-hard btn-hard--compact !rounded-full !px-3"
            aria-label="重置全部槽位"
            @click="resetSlots"
          >
            <svg aria-hidden="true" class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M3 12a9 9 0 1 0 3-6.7L3 8"></path>
              <path d="M3 3v5h5"></path>
            </svg>
            重置
          </button>
        </div>
      </div>
    </footer>

    <!-- 保存确认抽屉 -->
    <div
      v-if="showSaveSheet"
      class="motion-fade fixed inset-0 z-40 bg-ink/50"
      @click.self="closeSheet"
    >
      <div
        class="sheet-panel safe-b fixed inset-x-0 bottom-0 z-50 max-h-[80vh] overflow-y-auto p-5"
        role="dialog"
        aria-modal="true"
        aria-label="保存布局模板确认"
      >
        <div class="flex items-start justify-between">
          <div>
            <h2 class="flex items-center gap-2 text-lg font-black tracking-tight">
              保存布局模板
              <span class="tag-code">&lt;template&gt;</span>
            </h2>
            <p class="mt-0.5 text-[11px] font-bold text-ink-muted">
              归一化坐标将随模板入库，严格校验闭区间 [0.00, 1.00]
            </p>
          </div>
          <button
            ref="closeButton"
            type="button"
            aria-label="关闭"
            class="flex h-11 w-11 items-center justify-center rounded-full border-2 border-ink bg-surface transition-all hover:bg-ink hover:text-white active:scale-95"
            @click="closeSheet"
          >
            <svg aria-hidden="true" class="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round">
              <path d="M18 6 6 18M6 6l12 12"></path>
            </svg>
          </button>
        </div>

        <pre
          class="mt-4 max-h-48 overflow-x-auto rounded-xl border-2 border-ink bg-ink-soft p-3 font-mono text-[11px] leading-relaxed text-sky"
        >{{ normalizedJson }}</pre>

        <div
          class="mt-3 flex items-center gap-2 rounded-xl border-2 px-3 py-2 text-xs font-black"
          :class="allInRange ? 'border-mint bg-mint/30 text-emerald-800' : 'border-coral bg-blush text-danger'"
          role="status"
        >
          <svg v-if="allInRange" aria-hidden="true" class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round">
            <path d="M20 6 9 17l-5-5"></path>
          </svg>
          <svg v-else aria-hidden="true" class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round">
            <circle cx="12" cy="12" r="10"></circle>
            <line x1="12" y1="8" x2="12" y2="12"></line>
            <line x1="12" y1="16" x2="12.01" y2="16"></line>
          </svg>
          {{ allInRange ? '校验通过：全部坐标位于 [0.00, 1.00]' : '校验失败：存在越界坐标' }}
        </div>

        <p v-if="saveError" role="alert" class="mt-3 text-xs font-bold text-danger">{{ saveError }}</p>
        <div class="mt-5 flex items-center justify-between gap-3">
          <button type="button" class="btn-hard w-[45%]" :disabled="saving" @click="closeSheet">
            取消
          </button>
          <button
            type="button"
            class="btn-hard btn-hard--primary w-[45%]"
            :disabled="saving || restoringTemplate || Boolean(sizeError) || saveUncertain || !allInRange"
            @click="confirmSave"
          >
            <svg
              v-if="saving"
              aria-hidden="true"
              class="h-4 w-4 motion-safe:animate-spin"
              viewBox="0 0 24 24"
              fill="none"
            >
              <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
              <path class="opacity-90" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z"></path>
            </svg>
            {{ saving ? '保存中…' : '确认保存' }}
          </button>
        </div>
      </div>
    </div>

    <!-- 成功 Toast -->
    <div
      v-if="savedToast"
      role="status"
      class="pointer-events-none fixed inset-x-0 z-[60] flex justify-center px-4"
      style="bottom: calc(6rem + env(safe-area-inset-bottom));"
    >
      <span class="motion-fade flex items-center gap-2 rounded-full border-2 border-ink bg-ink px-4 py-2.5 font-mono text-[11px] font-black text-white shadow-hard">
        <span aria-hidden="true" class="flex h-4 w-4 items-center justify-center rounded-full bg-mint text-ink">
          <svg class="h-2.5 w-2.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="4" stroke-linecap="round" stroke-linejoin="round">
            <path d="M20 6 9 17l-5-5"></path>
          </svg>
        </span>
        布局模板保存成功
      </span>
    </div>
  </main>
</template>

<!-- 验收: 工具栏safe-area; 保存弹窗[0,1]归一化JSON; 全程零AI; 可点工具≥44px; useDraft草稿持久化; 波普圆润双层风 -->
