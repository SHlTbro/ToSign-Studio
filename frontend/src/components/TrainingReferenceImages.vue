<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { request, type ApiEnvelope } from '@/api/http'
import { useAuthStore } from '@/stores/auth'
import type { Asset } from '@/types/domain'

const props = defineProps<{ styleId?: number }>()
const auth = useAuthStore()
const items = ref<Asset[]>([])
const total = ref(0)
const page = ref(1)
const loading = ref(false)
const error = ref('')
let generation = 0

async function load(nextPage = 1): Promise<void> {
  const current = ++generation
  const account = auth.me?.user_id
  items.value = []
  total.value = 0
  page.value = nextPage
  error.value = ''
  if (!account) { loading.value = false; return }
  loading.value = true
  try {
    const response = await request<ApiEnvelope<{ items: Asset[] }>>(
      `/api/v1/assets?type=sample_source&page=${nextPage}&page_size=20`,
      { preserveEnvelope: true, retries: 0 },
    )
    const count = Number(response.meta?.total)
    if (!Array.isArray(response.data.items) || response.data.items.some(row => row.type !== 'sample_source')
      || !Number.isSafeInteger(count) || count < response.data.items.length) {
      throw new Error('训练参考图列表用途或数量不匹配，请重新读取')
    }
    if (current === generation && auth.me?.user_id === account) {
      const lastPage = Math.max(1, Math.ceil(count / 20))
      if (nextPage > lastPage) { void load(lastPage); return }
      items.value = response.data.items
      total.value = count
    }
  } catch (reason) {
    if (current === generation && auth.me?.user_id === account) {
      error.value = reason instanceof Error ? reason.message : '读取训练参考图失败'
    }
  } finally { if (current === generation) loading.value = false }
}

function extractPath(assetId: string): string {
  const path = `/extract/${encodeURIComponent(assetId)}`
  return props.styleId ? `${path}?style_id=${props.styleId}` : path
}
watch(() => auth.me?.user_id, () => { void load() }, { immediate: true })
onBeforeUnmount(() => { generation += 1 })
</script>

<template>
  <section aria-label="训练参考图" data-testid="training-reference-images" class="rounded-xl border border-ink/20 p-3">
    <div class="flex items-center justify-between gap-2">
      <h3 class="text-sm font-bold">训练参考图 · {{ total }} 张</h3>
      <button type="button" class="min-h-11 px-2 text-xs underline" :disabled="loading || !auth.me" @click="load(page)">刷新训练参考图</button>
    </div>
    <p class="mt-1 text-xs text-ink-muted">这些原图可含 To签。由项目从原图文字提取字形，再确认字样和生成训练集。原文件保留，不作为生成底图。</p>
    <p v-if="loading" role="status" class="mt-2 text-xs">正在读取训练参考图…</p>
    <p v-else-if="error" role="alert" class="mt-2 text-xs text-danger">{{ error }}</p>
    <p v-else-if="!items.length" role="status" class="mt-2 text-xs">本账户暂无训练参考图，请在提取页上传。</p>
    <ul v-else class="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-4">
      <li v-for="(asset, index) in items" :key="asset.asset_id" class="min-w-0 rounded-lg border border-ink/20 p-2">
        <RouterLink :to="extractPath(asset.asset_id)" class="block">
          <img :src="`/api/v1/assets/${encodeURIComponent(asset.asset_id)}/thumbnail`" :alt="`参考图 ${(page - 1) * 20 + index + 1}`" class="h-36 w-full object-contain" loading="lazy">
          <p class="mt-1 text-xs font-bold">参考图 {{ (page - 1) * 20 + index + 1 }}</p>
          <p class="mt-1 text-xs font-bold">查看原图并提取字形</p>
        </RouterLink>
        <details class="mt-1 text-[10px]"><summary class="min-h-8 cursor-pointer">高级：原图校验</summary><p class="break-all font-mono">{{ asset.asset_id }}</p><p class="break-all font-mono">{{ asset.sha256 }}</p></details>
      </li>
    </ul>
    <div v-if="total > 20" class="mt-2 flex items-center justify-between text-xs">
      <button type="button" class="min-h-11 px-2 underline" :disabled="loading || page <= 1" @click="load(page - 1)">上一页参考图</button>
      <span>{{ page }} / {{ Math.ceil(total / 20) }}</span>
      <button type="button" class="min-h-11 px-2 underline" :disabled="loading || page * 20 >= total" @click="load(page + 1)">下一页参考图</button>
    </div>
  </section>
</template>
