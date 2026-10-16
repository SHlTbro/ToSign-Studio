<!-- src/pages/P8_Blessings.vue -->
<script setup lang="ts">
import { computed, nextTick, onMounted, ref } from 'vue'
import type { Blessing } from '@/types/domain'
import { request } from '@/api/http'

const MAX_ITEMS = 10

// 真数据源：GET/POST/PUT /phrase-templates + enable/disable（v0.2 §1.3 六端点，
// v0.4 E-6.5：旧别名路径已移除，本页统一走新路径）。
// 契约无 DELETE 端点（D-T68-5），删除按钮已移除，启停由 Switch 承担。
const blessingsList = ref<Blessing[]>([])
const savedBlessingText = new Map<string, string>()
const loading = ref(true)
const loadError = ref<string | null>(null)

async function loadBlessings(): Promise<void> {
  loading.value = true
  loadError.value = null
  try {
    const d = await request<{ items: Blessing[] }>('/api/v1/phrase-templates')
    blessingsList.value = d.items
    savedBlessingText.clear()
    for (const item of d.items) savedBlessingText.set(item.id, item.text)
  } catch (e) {
    loadError.value = e instanceof Error && e.message ? e.message : '祝福语库加载失败'
  } finally {
    loading.value = false
  }
}
onMounted(() => {
  void loadBlessings()
  void loadPresets()
})

const count = computed(() => blessingsList.value.length)
const atLimit = computed(() => count.value >= MAX_ITEMS)

// 自适应撑高：无内部滚动条，容器随内容自然伸展
function autoResize(e: Event): void {
  const el = e.target as HTMLTextAreaElement
  el.style.height = 'auto'
  el.style.height = `${el.scrollHeight}px`
}

const savingIds = ref<Set<string>>(new Set())
function markSaving(id: string, on: boolean): void {
  const next = new Set(savingIds.value)
  if (on) next.add(id)
  else next.delete(id)
  savingIds.value = next
}

async function addBlessing(): Promise<void> {
  if (atLimit.value) return
  // Keep the blank row local until the user enters a valid phrase. The API
  // rejects empty text, so creating on button click would always fail.
  const localId = `blessing-local-${Date.now()}`
  blessingsList.value.push({ id: localId, text: '', enabled: true, created_by: '', updated_at: '' })
  // 聚焦新行并按内容撑高
  void nextTick(() => {
    const areas = document.querySelectorAll<HTMLTextAreaElement>('[data-blessing-input]')
    const last = areas[areas.length - 1]
    last?.focus()
    if (last) autoResize({ target: last } as unknown as Event)
  })
}

function toggle(item: Blessing): void {
  const target = !item.enabled
  markSaving(item.id, true)
  // 启停走专用端点；失败回滚开关
  request<Blessing>(`/api/v1/phrase-templates/${item.id}/${target ? 'enable' : 'disable'}`, { method: 'POST' })
    .then((updated) => {
      item.enabled = updated.enabled ?? target
      item.updated_at = updated.updated_at ?? item.updated_at
    })
    .catch((e) => {
      item.enabled = !target
      flash(e instanceof Error && e.message ? `启停失败：${e.message}` : '启停失败，请重试')
    })
    .finally(() => markSaving(item.id, false))
}

// 失焦保存文本编辑（PUT 局部更新；空文本被契约拒绝时恢复原值）
async function saveText(item: Blessing): Promise<void> {
  const text = item.text.trim()
  if (!text) {
    if (item.id.startsWith('blessing-local-')) {
      flash('先输入祝福语再保存')
    } else {
      item.text = savedBlessingText.get(item.id) ?? item.text
      flash('祝福语不能为空，已恢复上次保存内容')
    }
    return
  }
  markSaving(item.id, true)
  try {
    if (item.id.startsWith('blessing-local-')) {
      const created = await request<Blessing>('/api/v1/phrase-templates', {
        method: 'POST',
        body: { text, enabled: item.enabled },
      })
      const index = blessingsList.value.findIndex((row) => row.id === item.id)
      if (index >= 0) blessingsList.value[index] = created
      savedBlessingText.set(created.id, created.text)
    } else {
      const updated = await request<Blessing>(`/api/v1/phrase-templates/${item.id}`, {
        method: 'PUT',
        body: { text },
      })
      item.text = updated.text
      item.updated_at = updated.updated_at
      savedBlessingText.set(item.id, updated.text)
    }
  } catch (e) {
    if (!item.id.startsWith('blessing-local-')) item.text = savedBlessingText.get(item.id) ?? item.text
    flash(e instanceof Error && e.message ? `保存失败：${e.message}` : '保存失败，请重试')
  } finally {
    markSaving(item.id, false)
  }
}

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

// ---------- 博主署名预设（真 CRUD：/blogger-name-presets，CC-14；上限 5 条 409） ----------
interface PresetRow {
  preset_id: string
  name: string
  is_default: boolean
  updated_at?: string
}
const MAX_NAMES = 5
const bloggerNames = ref<PresetRow[]>([])
const presetsError = ref<string | null>(null)

async function loadPresets(): Promise<void> {
  presetsError.value = null
  try {
    const d = await request<{ items: PresetRow[] }>('/api/v1/blogger-name-presets')
    bloggerNames.value = d.items
  } catch (e) {
    presetsError.value = e instanceof Error && e.message ? e.message : '署名预设加载失败'
  }
}

async function savePresetName(preset: PresetRow): Promise<void> {
  const name = preset.name.trim()
  if (!name) {
    flash('署名不能为空')
    return
  }
  try {
    if (preset.preset_id.startsWith('bn-local-')) {
      // 首次落库：POST（is_default 立即提交，避免二次 PATCH）
      const created = await request<PresetRow>('/api/v1/blogger-name-presets', {
        method: 'POST',
        body: { name, is_default: preset.is_default },
      })
      const idx = bloggerNames.value.findIndex((p) => p.preset_id === preset.preset_id)
      if (idx >= 0) bloggerNames.value[idx] = { ...created }
    } else {
      const updated = await request<PresetRow>(`/api/v1/blogger-name-presets/${preset.preset_id}`, {
        method: 'PATCH',
        body: { name },
      })
      preset.name = updated.name
      preset.updated_at = updated.updated_at
    }
  } catch (e) {
    flash(e instanceof Error && e.message ? `署名保存失败：${e.message}` : '署名保存失败，请重试')
  }
}

async function setDefaultName(preset: PresetRow): Promise<void> {
  const prev = bloggerNames.value.find((p) => p.is_default)
  if (prev) prev.is_default = false
  preset.is_default = true
  if (preset.preset_id.startsWith('bn-local-')) return
  try {
    const updated = await request<PresetRow>(`/api/v1/blogger-name-presets/${preset.preset_id}`, {
      method: 'PATCH',
      body: { is_default: true },
    })
    preset.is_default = updated.is_default
  } catch (e) {
    if (prev) prev.is_default = true
    preset.is_default = false
    flash(e instanceof Error && e.message ? `设默认失败：${e.message}` : '设默认失败，请重试')
  }
}

function addName(): void {
  if (bloggerNames.value.length >= MAX_NAMES) return
  bloggerNames.value.push({
    preset_id: `bn-local-${Date.now()}`,
    name: '',
    is_default: bloggerNames.value.length === 0,
  })
}

async function removeNameAt(index: number): Promise<void> {
  const preset = bloggerNames.value[index]
  if (!preset) return
  if (!preset.preset_id.startsWith('bn-local-')) {
    try {
      await request(`/api/v1/blogger-name-presets/${preset.preset_id}`, { method: 'DELETE' })
    } catch (e) {
      flash(e instanceof Error && e.message ? `删除失败：${e.message}` : '删除失败，请重试')
      return
    }
  }
  bloggerNames.value.splice(index, 1)
  // Owner contract: deleting the default leaves no default; do not silently promote.
}
</script>

<template>
  <main class="bg-blueprint flex h-[100dvh] flex-col overflow-hidden text-ink select-none">
    <h1 class="sr-only">模版语库</h1>

    <header class="safe-t z-20 flex h-14 shrink-0 items-center justify-between gap-2 border-b border-ink/15 px-4">
      <h2 class="text-xl font-black tracking-tight">模版语库</h2>
      <span
        class="flex items-center gap-1 rounded-full border-2 border-ink bg-surface px-3 py-1 font-mono text-xs font-black shadow-[2px_2px_0_var(--color-ink)]"
        role="status"
      >
        容量 {{ count }} / {{ MAX_ITEMS }}
      </span>
    </header>

    <section class="flex-1 space-y-3 overflow-y-auto overflow-x-hidden px-4 pb-32 pt-4">
      <!-- 博主署名预设（P5 逐行「图上署名」引用） -->
      <div class="motion-rise rounded-2xl border-2 border-ink bg-surface p-3.5 shadow-[2px_2px_0_var(--color-ink)]">
        <div class="flex items-center justify-between">
          <p class="text-sm font-black">博主署名预设</p>
          <span class="font-mono text-[10px] font-bold text-ink-muted">{{ bloggerNames.length }} / {{ MAX_NAMES }}</span>
        </div>
        <p class="mt-0.5 text-[11px] font-bold text-ink-muted">批量生成时可逐行选择，是否把博主名写上 cos 图</p>
        <div
          v-if="presetsError"
          class="mt-2 flex items-center justify-between gap-2 rounded-xl border-2 border-coral bg-blush px-3 py-2"
          role="alert"
        >
          <p class="text-xs font-black text-danger">{{ presetsError }}</p>
          <button type="button" class="btn-hard btn-hard--compact shrink-0" @click="loadPresets">重试</button>
        </div>
        <div class="mt-2.5 space-y-1.5">
          <div v-for="(p, i) in bloggerNames" :key="p.preset_id" class="flex items-center gap-1">
            <button
              type="button"
              role="radio"
              :aria-checked="p.is_default"
              :aria-label="`将 ${p.name || '未命名'} 设为默认署名`"
              title="设为默认署名"
              class="flex h-11 w-9 shrink-0 items-center justify-center"
              @click="setDefaultName(p)"
            >
              <span
                class="flex h-5 w-5 items-center justify-center rounded-full border-2 border-ink transition-colors duration-150"
                :class="p.is_default ? 'bg-ink' : 'bg-surface'"
              >
                <span v-if="p.is_default" class="h-1.5 w-1.5 rounded-full bg-white"></span>
              </span>
            </button>
            <input
              v-model="p.name"
              type="text"
              maxlength="20"
              class="input-hard min-h-[44px] flex-1 !py-2 text-sm"
              :aria-label="`署名预设 ${i + 1}`"
              placeholder="输入博主名（≤20 字，失焦保存）"
              @blur="savePresetName(p)"
            >
            <button
              type="button"
              class="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl border-2 border-coral bg-surface text-coral transition-all hover:bg-coral hover:text-white active:scale-95"
              :aria-label="`删除署名预设 ${i + 1}`"
              @click="removeNameAt(i)"
            >
              <svg aria-hidden="true" class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M3 6h18"></path>
                <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
              </svg>
            </button>
          </div>
          <button
            v-if="bloggerNames.length < MAX_NAMES"
            type="button"
            class="flex min-h-[44px] w-full items-center justify-center gap-1.5 rounded-xl border-2 border-dashed border-ink/40 text-xs font-black text-ink-muted transition-all hover:border-ink hover:bg-sky/50 hover:text-ink active:scale-[0.99]"
            @click="addName"
          >
            <svg aria-hidden="true" class="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round"><path d="M12 5v14M5 12h14"></path></svg>
            添加署名预设
          </button>
        </div>
      </div>

      <p class="pt-1 text-sm font-black tracking-tight">祝福语模板</p>

      <!-- 空态引导 -->
      <div
        v-if="!loading && loadError === null && count === 0"
        class="motion-rise space-y-2 rounded-2xl border-2 border-dashed border-ink/30 bg-surface/70 p-8 text-center"
      >
        <span aria-hidden="true" class="mx-auto flex h-12 w-12 items-center justify-center rounded-2xl border-2 border-ink bg-lilac">
          <svg class="h-6 w-6" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M7 11v8a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1v-7a1 1 0 0 1 1-1h3Zm0 0V5a2 2 0 0 1 4 0v6m0 0h9a2 2 0 0 1 2 2l-1 5a2 2 0 0 1-2 2h-8"></path>
          </svg>
        </span>
        <p class="text-sm font-black">还没有添加任何专属祝福语</p>
        <p class="font-mono text-[11px] font-bold text-ink-muted">点击下方按钮，开始创建第一条模板</p>
      </div>

      <!-- 错误态 -->
      <div
        v-if="loadError"
        class="flex items-center justify-between gap-3 rounded-2xl border-2 border-coral bg-blush px-4 py-3"
        role="alert"
      >
        <p class="text-xs font-black text-danger">祝福语库加载失败：{{ loadError }}</p>
        <button type="button" class="btn-hard btn-hard--compact shrink-0" @click="loadBlessings">重试</button>
      </div>

      <!-- 模板行 -->
      <div
        v-for="(item, index) in blessingsList"
        :key="item.id"
        class="motion-fade flex items-start gap-3 rounded-2xl border-2 border-ink bg-surface p-3 shadow-[2px_2px_0_var(--color-ink)] transition-all duration-150"
        :class="item.enabled ? '' : 'opacity-60'"
      >
        <textarea
          v-model="item.text"
          data-blessing-input
          rows="1"
          class="input-hard min-h-[44px] flex-1 resize-none overflow-hidden !py-2.5 text-sm"
          :aria-label="`祝福语第 ${index + 1} 条`"
          placeholder="输入祝福语（如：天天开心），失焦自动保存"
          @input="autoResize"
          @blur="saveText(item)"
        ></textarea>

        <div class="flex shrink-0 items-center gap-1">
          <!-- 启停 Switch（真端点 enable/disable） -->
          <button
            type="button"
            role="switch"
            :aria-checked="item.enabled"
            :aria-label="`第 ${index + 1} 条启停`"
            :disabled="savingIds.has(item.id)"
            class="flex h-11 w-11 items-center justify-center disabled:opacity-50"
            @click="toggle(item)"
          >
            <span
              class="relative inline-flex h-6 w-10 rounded-full border-2 border-ink transition-colors duration-150"
              :class="item.enabled ? 'bg-mint' : 'bg-gray-200'"
            >
              <span
                class="absolute top-0.5 h-4 w-4 rounded-full border border-ink bg-white transition-all duration-150"
                :style="{ left: item.enabled ? '1.125rem' : '2px' }"
              ></span>
            </span>
          </button>
        </div>
      </div>

      <!-- 新增（上限拦截） -->
      <button
        type="button"
        class="flex min-h-[52px] w-full items-center justify-center gap-2 rounded-2xl border-2 border-dashed bg-surface/70 font-black transition-all duration-150"
        :class="atLimit
          ? 'cursor-not-allowed border-ink/25 text-ink-muted'
          : 'border-ink text-ink hover:bg-sky/50 active:scale-[0.99]'"
        :disabled="atLimit"
        @click="addBlessing"
      >
        <svg v-if="!atLimit" aria-hidden="true" class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round">
          <path d="M12 5v14M5 12h14"></path>
        </svg>
        {{ atLimit ? `已达系统上限 ${MAX_ITEMS} 条（请删除现有项后追加）` : '新增一条祝福语' }}
      </button>
    </section>
  </main>
</template>

<!-- 验收: 真端点phrase-templates(CRUD无DELETE,启停enable/disable)与blogger-name-presets(CRUD含DELETE); textarea自高失焦保存; 10条硬拦截; 空态引导; 错误态回滚 -->
