<script setup lang="ts">
import { computed } from 'vue'
import type { FrozenRenderSettings } from '@/types/renderControls'
import { inkHex, validInkBinding, validInkRgb } from '@/api/inkColor'
const props = defineProps<{ settings: FrozenRenderSettings }>()
const bindingDigest = computed(() => typeof props.settings.ink_color_binding_sha256 === 'string' && /^[0-9a-f]{64}$/.test(props.settings.ink_color_binding_sha256) ? props.settings.ink_color_binding_sha256 : null)
const binding = computed(() => {
  const value = props.settings.ink_color_binding
  return props.settings.ink_color_mode === 'source-solid-srgb-v1' && bindingDigest.value && validInkBinding(value)
    && value.palette_id === props.settings.ink_palette_id && value.profile_id === props.settings.style_id
    && props.settings.style_mode === 'profile' && props.settings.layout_profile === 'natural-layout-v1'
    && props.settings.glyph_source_policy === 'm2_required' ? value : null
})
const inconsistent = computed(() => {
  const value = props.settings
  if (value.ink_color_mode === 'source-solid-srgb-v1') return !binding.value
  const sourceMetadata = value.ink_palette_id != null || value.ink_color_binding != null || value.ink_color_binding_sha256 != null
  if (value.ink_color_mode === 'custom-solid-srgb-v1') return sourceMetadata || !validInkRgb(value.ink_color_rgb)
  if (value.ink_color_rgb != null) return true
  if (value.ink_color_mode === 'auto-contrast-v1') return sourceMetadata
  return ['ink_color_mode', 'ink_palette_id', 'ink_color_binding', 'ink_color_binding_sha256'].some((key) => Object.prototype.hasOwnProperty.call(value, key))
})
</script>

<template>
  <div class="mt-2 text-[11px]" data-testid="frozen-ink-settings">
    <p v-if="settings.ink_color_mode === 'custom-solid-srgb-v1' && validInkRgb(settings.ink_color_rgb)" class="flex items-center gap-2"><span aria-hidden="true" class="h-5 w-5 shrink-0 rounded border border-ink" :style="{ backgroundColor: inkHex(settings.ink_color_rgb) }"></span>冻结字体颜色：{{ inkHex(settings.ink_color_rgb) }} · RGB {{ settings.ink_color_rgb.join(', ') }} · 自选单色</p>
    <p v-if="settings.text_style">本次字号：普通字 {{ settings.text_style.ordinary_scale }} 倍 · To {{ settings.text_style.to_ratio }} 倍 · 姓名间距 {{ settings.text_style.header_gap }} 像素</p>
    <p v-if="settings.v3_layout_binding" class="break-all">V3 原模型与排版 · 配置 {{ settings.v3_layout_binding.profile_sha256 }}</p>
    <p v-if="settings.handwriting">本次逐字变化：大小 ±{{ settings.handwriting.size_variation_pct }}% · 倾斜 ±{{ settings.handwriting.tilt_variation_deg }}° · 种子 {{ settings.handwriting.seed }}</p>
    <p>冻结墨色：{{ inconsistent ? '颜色记录不完整或不一致，尚不能确认' : settings.ink_color_mode === 'source-solid-srgb-v1' ? '训练原图取色 · source-solid-srgb-v1' : settings.ink_color_mode === 'custom-solid-srgb-v1' ? '自选单色 · custom-solid-srgb-v1' : settings.ink_color_mode === 'auto-contrast-v1' ? '原有自动对比单色 · auto-contrast-v1' : '历史任务未记录墨色方式' }}</p>
    <p v-if="inconsistent" class="font-bold text-danger" role="alert">服务端冻结墨色身份缺失或不匹配，不能确认本次墨色来源；完整绑定校验缺失、异常或颜色记录互相矛盾。请勿据此通过质检。</p>
    <template v-if="settings.ink_color_mode === 'source-solid-srgb-v1'">
      <p>冻结调色板：{{ settings.ink_palette_id ?? '未记录' }}</p>
      <template v-if="binding">
        <p class="flex items-center gap-2"><span aria-hidden="true" class="inline-block h-5 w-5 shrink-0 rounded border border-ink" :style="{ backgroundColor: inkHex(binding.rgb) }"></span>RGB {{ binding.rgb.join(', ') }} · {{ inkHex(binding.rgb) }}</p>
        <p>绑定档案 #{{ binding.profile_id }} · 模型 #{{ binding.artifact_id }} · 所有者 #{{ binding.owner_user_id }}</p>
        <p class="break-all">模型清单校验：{{ binding.artifact_manifest_sha256 }}</p>
        <p class="break-all">原图校验：{{ binding.source_image_sha256 }}</p>
        <p class="break-all">测量校验：{{ binding.measurement_sha256 }}</p>
        <p class="break-all">墨色配置校验：{{ binding.profile_sha256 }}</p>
        <p v-if="bindingDigest" class="break-all">服务端完整绑定校验：{{ bindingDigest }}</p>
        <details class="mt-1">
          <summary class="min-h-8 cursor-pointer font-bold">完整冻结墨色身份</summary>
          <p class="break-all">适配器校验：{{ binding.adapter_sha256 }}</p>
          <p class="break-all">训练快照校验：{{ binding.snapshot_sha256 }}</p>
          <p class="break-all">运行时绑定校验：{{ binding.runtime_binding_sha256 }}</p>
          <p class="break-all">实现校验：{{ binding.implementation_sha256 }}</p>
          <p class="break-all">目录校验：{{ binding.catalog_sha256 }}</p>
        </details>
      </template>
      <p class="mt-1 font-bold text-amber">来自训练原图照片的表观墨色，按 sRGB 假设处理，不代表真实颜料或透明度。只改变 RGB，保留字形透明度；底图局部可见度须放大检查实际照片预览，没有自动可读性通过结论。</p>
      <p class="mt-1 text-ink-muted">这里显示服务端保存的本次任务墨色。重试保留原绑定；如需更换墨色，请创建新任务。</p>
    </template>
  </div>
</template>
