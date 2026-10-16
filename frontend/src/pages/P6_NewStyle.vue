<!-- src/pages/P6_NewStyle.vue -->
<script setup lang="ts">
import { nextTick, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { request } from '@/api/http'
import { loadPresetFont } from '@/utils/presetFont'
import HandwritingOnboarding from '@/components/HandwritingOnboarding.vue'

const router = useRouter()

// 真数据源：GET /style-presets（T65 字体下发；font_url 为票据签发入口 D-T65-1）
interface PresetRow {
  preset_id: string
  name: string | null
  description: string | null
  font_url: string
  file_size_bytes: number
  version: string | null
  activated_style_id: number | null
  available?: boolean
}

const presets = ref<PresetRow[]>([])
const loading = ref(true)
const loadError = ref<string | null>(null)
const catalogMessage = ref('')
const systemFontAvailable = ref(false)
const downloaded = ref<Set<string>>(new Set())
const downloading = ref<string | null>(null)
const presetGrid = ref<HTMLElement | null>(null)
const fontStates = ref<Record<string, 'loading' | 'ready' | 'error'>>({})
const fontFamilies = ref<Record<string, string>>({})
let previewObserver: IntersectionObserver | null = null

function loadPreviewFont(preset: PresetRow): void {
  if (preset.available === false) return
  const id = preset.preset_id
  if (fontStates.value[id] === 'loading' || fontStates.value[id] === 'ready') return
  fontStates.value[id] = 'loading'
  void loadPresetFont(preset.font_url).then(
    (family) => {
      fontFamilies.value[id] = family
      fontStates.value[id] = 'ready'
    },
    () => { fontStates.value[id] = 'error' },
  )
}

function observePresetCards(): void {
  previewObserver?.disconnect()
  if (!presetGrid.value) return
  if (typeof IntersectionObserver === 'undefined') {
    presets.value.forEach(loadPreviewFont)
    return
  }
  previewObserver = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue
      const id = (entry.target as HTMLElement).dataset.presetId
      const preset = presets.value.find((item) => item.preset_id === id)
      if (preset) loadPreviewFont(preset)
      previewObserver?.unobserve(entry.target)
    }
  }, { root: presetGrid.value.parentElement, rootMargin: '180px' })
  for (const card of presetGrid.value.querySelectorAll<HTMLElement>('[data-preset-id]')) {
    previewObserver.observe(card)
  }
}

async function loadPresets(): Promise<void> {
  loading.value = true
  loadError.value = null
  try {
    const d = await request<{ items: PresetRow[]; catalog?: { message: string }; fallback?: { available: boolean } }>('/api/v1/style-presets')
    presets.value = d.items
    catalogMessage.value = d.catalog?.message ?? ''
    systemFontAvailable.value = d.fallback?.available === true
    downloaded.value = new Set(
      d.items.filter((preset) => preset.activated_style_id != null).map((preset) => preset.preset_id),
    )
    await nextTick()
    observePresetCards()
  } catch (e) {
    loadError.value = e instanceof Error && e.message ? e.message : '预设字库加载失败'
  } finally {
    loading.value = false
  }
}
onMounted(() => {
  void loadPresets()
})
onUnmounted(() => previewObserver?.disconnect())

const notice = ref<string | null>(null)
let noticeTimer: number | null = null
function flash(message: string): void {
  notice.value = message
  if (noticeTimer !== null) window.clearTimeout(noticeTimer)
  noticeTimer = window.setTimeout(() => {
    notice.value = null
    noticeTimer = null
  }, 2400)
}

function formatSize(bytes: number): string {
  if (!bytes) return '—'
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

// 下载启用：GET /style-presets/{id}/download 签发票据，再以 <a> 免鉴权下载 TTF
async function onDownloadTap(preset: PresetRow): Promise<void> {
  if (downloading.value || preset.available === false) return
  downloading.value = preset.preset_id
  let activated = false
  try {
    await request(`/api/v1/style-presets/${encodeURIComponent(preset.preset_id)}/activate`, {
      method: 'POST',
      body: {},
    })
    activated = true
    downloaded.value = new Set([...downloaded.value, preset.preset_id])
    const d = await request<{ url?: string; ttl_seconds?: number }>(preset.font_url)
    if (typeof d.url === 'string' && d.url) {
      const a = document.createElement('a')
      a.href = d.url
      a.download = `${preset.name ?? preset.preset_id}.ttf`
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      flash(`「${preset.name ?? preset.preset_id}」已同步到风格库，并开始下载`)
    } else {
      flash('已同步到风格库，但下载地址缺失，请重试下载')
    }
  } catch (e) {
    const reason = e instanceof Error && e.message ? e.message : '请重试'
    flash(activated ? `已同步到风格库，本地下载失败：${reason}` : `启用失败：${reason}`)
  } finally {
    downloading.value = null
  }
}
</script>

<template>
  <main class="bg-blueprint flex h-[100dvh] flex-col overflow-hidden text-ink select-none">
    <h1 class="sr-only">新建手写资产</h1>

    <header class="safe-t z-20 flex h-14 shrink-0 items-center justify-between gap-2 border-b border-ink/15 px-3">
      <button
        type="button"
        aria-label="返回风格资产库"
        class="btn-hard btn-hard--compact !rounded-full !px-3"
        @click="router.push('/styles')"
      >
        <svg aria-hidden="true" class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">
          <path d="M19 12H5"></path>
          <path d="m12 19-7-7 7-7"></path>
        </svg>
      </button>
      <p class="text-sm font-black">新建手写资产</p>
    </header>

    <section class="flex-1 space-y-4 overflow-y-auto overflow-x-hidden px-4 pb-10 pt-4">
      <HandwritingOnboarding />
      <!-- 个人风格字体训练入口（跳模型训练控制台） -->
      <div class="motion-rise relative">
        <div aria-hidden="true" class="absolute inset-0 translate-x-1.5 translate-y-1.5 rounded-2xl bg-ink"></div>
        <RouterLink
          to="/training"
          class="group relative flex items-center justify-between rounded-2xl border-2 border-ink bg-sky p-4 transition-all duration-150 ease-[cubic-bezier(0.22,1,0.36,1)] hover:-translate-y-0.5 hover:shadow-[5px_5px_0_var(--color-ink)] active:translate-y-0 active:shadow-none"
        >
          <div class="flex items-center gap-3">
            <span aria-hidden="true" class="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl border-2 border-ink bg-primary text-white transition-transform duration-150 group-hover:rotate-[-6deg] group-hover:scale-105">
              <svg class="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M22 12h-4l-3 9L9 3l-3 9H2"></path>
              </svg>
            </span>
            <div>
              <p class="text-sm font-black">个人风格字体训练</p>
              <p class="mt-0.5 text-[11px] font-bold leading-snug text-ink-muted">已有档案可查看样本、训练快照与运行环境；质量门通过前不能发布</p>
            </div>
          </div>
          <span aria-hidden="true" class="flex h-9 w-9 shrink-0 items-center justify-center rounded-full border-2 border-ink bg-surface transition-transform duration-150 group-hover:translate-x-0.5">
            <svg class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="m9 18 6-6-6-6"></path></svg>
          </span>
        </RouterLink>
      </div>

      <!-- 预设基底字库（编排者预设，可下载） -->
      <div class="motion-rise flex items-center justify-between pt-1" style="animation-delay:60ms">
        <p class="text-sm font-black tracking-tight">预设基底字库</p>
        <span class="font-mono text-[10px] font-bold text-ink-muted">{{ presets.length }} 款 · 官方预设</span>
      </div>
      <p class="text-[11px] font-bold text-ink-muted" style="animation-delay:60ms">
        字体名称来自授权预设清单，缺失文件会单独标记；预设字体与个人手写训练模型不同
      </p>

      <div ref="presetGrid" class="grid grid-cols-1 gap-3">
        <!-- 加载态 -->
        <div
          v-if="loading"
          class="flex items-center justify-center gap-2 rounded-2xl border-2 border-dashed border-ink/30 bg-surface/70 px-6 py-10 text-center"
          role="status"
        >
          <svg aria-hidden="true" class="h-4 w-4 motion-safe:animate-spin" viewBox="0 0 24 24" fill="none">
            <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
            <path class="opacity-90" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z"></path>
          </svg>
          <p class="text-sm font-black text-ink-muted">预设字库加载中…</p>
        </div>

        <div v-else-if="!loadError && !presets.length" role="status" class="space-y-2 rounded-2xl border-2 border-dashed border-ink/30 bg-surface/70 px-4 py-4">
          <p class="text-xs font-bold">{{ catalogMessage || '尚未安装可选预设字体，仍可创建个人手写档案' }}</p>
          <p v-if="systemFontAvailable" class="text-xs text-ink-muted">本机楷体（simkai）可用于普通系统字体生成，无需安装 ML 插件；它不是个人手写模型，也不提供字体下载。</p>
          <p v-else class="text-xs text-ink-muted">未检测到本机楷体（simkai）。普通渲染需可用的授权字体；已验证的运行时内容字体仅作为渲染回退，不冒充预设或个人手写。</p>
        </div>

        <!-- 错误态 -->
        <div
          v-else-if="loadError"
          class="flex items-center justify-between gap-3 rounded-2xl border-2 border-coral bg-blush px-4 py-3"
          role="alert"
        >
          <p class="text-xs font-black text-danger">预设字库加载失败：{{ loadError }}</p>
          <button type="button" class="btn-hard btn-hard--compact shrink-0" @click="loadPresets">重试</button>
        </div>

        <article
          v-for="(preset, i) in presets"
          :key="preset.preset_id"
          :data-preset-id="preset.preset_id"
          class="motion-rise relative flex flex-col gap-2.5 rounded-2xl border-2 border-ink bg-surface p-4 shadow-[3px_3px_0_var(--color-ink)]"
          :style="{ animationDelay: `${80 + i * 40}ms` }"
        >
          <div class="flex items-start justify-between gap-2">
            <p class="text-2xl leading-tight" :style="fontStates[preset.preset_id] === 'ready' ? { fontFamily: fontFamilies[preset.preset_id] } : undefined">{{ preset.name }}</p>
          </div>
          <p
            v-if="fontStates[preset.preset_id] === 'ready'"
            class="truncate border-y border-ink/10 py-2 text-lg leading-snug"
            :style="{ fontFamily: fontFamilies[preset.preset_id] }"
          >
            星河手写 To 0123
          </p>
          <div v-else class="flex min-h-10 items-center justify-between border-y border-ink/10 py-2 text-xs font-bold text-ink-muted" role="status">
            <span>{{ preset.available === false ? '字体文件未安装或不可用' : fontStates[preset.preset_id] === 'error' ? '实际字体预览加载失败' : '正在加载实际字体…' }}</span>
            <button v-if="fontStates[preset.preset_id] === 'error'" type="button" class="underline" @click="loadPreviewFont(preset)">重试</button>
          </div>
          <p class="text-[11px] font-bold text-ink-muted">{{ preset.description }}</p>

          <div class="flex items-center justify-between border-t border-ink/10 pt-2.5">
            <span class="font-mono text-[10px] font-bold text-ink-muted">{{ formatSize(preset.file_size_bytes) }} · TTF</span>
            <button
              type="button"
              class="btn-hard btn-hard--compact"
              :class="downloaded.has(preset.preset_id) ? '!bg-mint' : ''"
              :disabled="downloading !== null || preset.available === false"
              :aria-label="`${downloaded.has(preset.preset_id) ? '重新下载' : '下载并启用'}${preset.name}`"
              @click="onDownloadTap(preset)"
            >
              <svg
                v-if="downloading === preset.preset_id"
                aria-hidden="true"
                class="h-3.5 w-3.5 motion-safe:animate-spin"
                viewBox="0 0 24 24"
                fill="none"
              >
                <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                <path class="opacity-90" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z"></path>
              </svg>
              {{ downloaded.has(preset.preset_id) ? '已加入资产' : downloading === preset.preset_id ? '签发票据…' : '下载启用' }}
            </button>
          </div>
        </article>
      </div>

      <p class="pb-2 pt-1 text-center font-mono text-[10px] uppercase tracking-[0.3em] text-ink-muted">
        来源档案 → 字符提取与确认 → 训练快照 → 评估与发布
      </p>
    </section>

    <!-- Toast -->
    <div
      v-if="notice"
      role="status"
      class="pointer-events-none fixed inset-x-0 z-[60] flex justify-center px-4"
      style="bottom: calc(1.5rem + env(safe-area-inset-bottom));"
    >
      <span class="motion-fade max-w-full truncate rounded-full border-2 border-ink bg-ink px-4 py-2.5 font-mono text-[11px] font-black text-white shadow-hard">{{ notice }}</span>
    </div>
  </main>
</template>

<!-- 验收: 真端点GET /style-presets + 签发票据下载; 字库名与字样使用同一真实TTF; 「个人风格字体训练」跳/training; 加载/错误态; 动效可降级; 无hex无Emoji -->
