<script setup lang="ts">
import { userError } from '@/utils/userLanguage'
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { ApiRequestError } from '@/api/http'
import { readUnpublishedModels, type UnpublishedModelList } from '@/api/unpublishedModels'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const data = ref<UnpublishedModelList | null>(null)
const loading = ref(false)
const error = ref('')
const page = ref(1)
const pageCount = computed(() => Math.max(1, Math.ceil((data.value?.total ?? 0) / 20)))
let generation = 0
async function load(nextPage = page.value): Promise<void> {
  const current = ++generation
  const account = auth.me?.user_id
  data.value = null
  error.value = ''
  page.value = nextPage
  if (!account) { loading.value = false; error.value = '请先登录后查看已导入的模型'; return }
  loading.value = true
  try {
    const result = await readUnpublishedModels(nextPage)
    if (current === generation && auth.me?.user_id === account) {
      const lastPage = Math.max(1, Math.ceil(result.total / 20))
      if (nextPage > lastPage) { void load(lastPage); return }
      data.value = result
    }
  } catch (reason) {
    if (current === generation && auth.me?.user_id === account) {
      error.value = reason instanceof ApiRequestError ? userError(reason)
        : reason instanceof Error ? reason.message : '读取未发布模型失败，请重试'
    }
  } finally { if (current === generation) loading.value = false }
}
watch(() => auth.me?.user_id, () => { void load(1) }, { immediate: true })
onBeforeUnmount(() => { generation += 1 })
</script>

<template>
  <section class="rounded-2xl border-2 border-ink bg-surface p-4" aria-label="已导入的旧模型" data-testid="unpublished-model-list">
    <div class="flex flex-wrap items-center justify-between gap-2">
      <h2 class="text-sm font-black">已导入的旧模型</h2>
      <button type="button" class="min-h-11 px-2 text-xs font-bold underline" :disabled="loading || !auth.me" @click="load()">刷新模型列表</button>
    </div>
    <p class="mt-1 text-xs leading-relaxed text-ink-muted">只显示本账户的导入记录。已有模型无需重新训练；通过独立推理资格后可生成图片。模型发布与质量验收分别记录。</p>
    <p v-if="loading" class="mt-3 text-xs" role="status">正在读取已导入的模型…</p>
    <p v-else-if="error" class="mt-3 break-words text-xs font-bold text-danger" role="alert">{{ error }}</p>
    <p v-else-if="data && !data.items.length" class="mt-3 text-xs" role="status">本账户还没有已导入的旧模型。完成导入后刷新即可看到。</p>
    <ul v-else-if="data" class="mt-3 space-y-3">
      <li v-for="model in data.items" :key="model.import_id" class="min-w-0 rounded-xl border border-ink/20 p-3" data-testid="unpublished-model">
        <h3 class="text-xs font-bold">{{ model.adapter_sha256 === '017881442b7f8b909f7d8d8c4c5e7ad1893d0caa3c3267ae3c793a9a96175f81' ? '经典手写 V3' : `导入手写字体 · ${model.runtime_status !== 'NOT_REGISTERED' ? '字体 ' + model.inference_selection_id : '记录 ' + ((page - 1) * 20 + data.items.indexOf(model) + 1)}` }}</h3>
        <div class="mt-2 flex flex-wrap gap-2 text-[11px] font-bold">
          <span class="rounded border border-amber px-2 py-1">未发布</span>
          <span class="rounded border border-ink/30 px-2 py-1" data-testid="model-runtime-status">
            <template v-if="model.runtime_status === 'INFERENCE_READY'">推理资格已通过</template>
            <template v-else-if="model.runtime_status === 'SELECTED_BLOCKED'">当前推理资格未通过</template>
            <template v-else>未注册推理资格</template>
          </span>
        </div>
        <p v-if="model.inference_ready" class="mt-2 text-xs font-bold">可生成：已认可的 V3 组合。选择这份字体后生成；发布与模型整体质量批准尚未完成。</p>
        <p v-else-if="model.runtime_status === 'SELECTED_BLOCKED'" class="mt-2 text-xs font-bold text-danger">不可生成：已关联字体，但当前推理资格复验未通过，请联系维护人员检查。</p>
        <p v-else class="mt-2 text-xs font-bold text-danger">不可生成：尚未完成这份模型的独立推理资格验证。</p>
        <p class="mt-2 text-xs">来源：{{ model.source_summary }}</p>
        
        <details class="mt-2 text-xs"><summary class="min-h-11 cursor-pointer font-bold">高级：导入来源与文件校验</summary>
          <p class="mt-2 text-xs font-bold">模型文件校验码</p><p class="select-text break-all font-mono text-[11px]">{{ model.adapter_sha256 }}</p>
          <p class="select-text break-all">导入时间：{{ model.created_at }}</p>
          <p class="mt-1 select-text break-all">清单文件校验码：{{ model.manifest_sha256 }}</p>
          <p class="mt-1 select-text break-all">来源记录校验码：{{ model.input_fingerprint_sha256 }}</p>
          <p v-if="model.runtime_status !== 'NOT_REGISTERED'" class="mt-2">生成字体编号：{{ model.inference_selection_id }}</p>
          <p v-if="model.inference_ready" class="mt-2">已认可的 V3 文案：{{ model.acceptance_scope.text_lines.join(' / ') }}。质量范围：仅限本人认可的经典手写组合。</p>
          <p class="mt-2">导入、推理资格和发布分别核验。推理资格通过仅允许上述限定使用，不授予训练、发布或模型整体质量批准。</p>
        </details>
      </li>
    </ul>
    <div v-if="data && pageCount > 1" class="mt-3 flex items-center justify-between gap-2 text-xs">
      <button type="button" class="min-h-11 px-2 underline" :disabled="loading || page <= 1" @click="load(page - 1)">上一页</button>
      <span>{{ page }} / {{ pageCount }} · {{ data.total }} 个模型</span>
      <button type="button" class="min-h-11 px-2 underline" :disabled="loading || page >= pageCount" @click="load(page + 1)">下一页</button>
    </div>
  </section>
</template>
