import type { FrozenRenderSettings, InkColorBinding, InkColorCapabilities, RenderCapabilities } from '@/types/renderControls'
import { M2_CAPABILITIES } from './render-capability-fixtures'
// Synthetic unit-test identities only; these never enable or qualify the distributed catalog.
export const INK_BINDING: InkColorBinding = {
  schema: 'author-ink-color-binding/1', owner_user_id: 2, profile_id: 7, artifact_id: 12,
  artifact_manifest_sha256: 'b'.repeat(64), adapter_sha256: 'c'.repeat(64), snapshot_sha256: 'd'.repeat(64),
  runtime_binding_sha256: 'e'.repeat(64), palette_id: 'train-magenta-v1', rgb: [196, 26, 180],
  source_image_sha256: '1'.repeat(64), measurement_sha256: '2'.repeat(64), profile_sha256: '3'.repeat(64),
  implementation_sha256: '4'.repeat(64), catalog_sha256: '5'.repeat(64),
}
export const INK_CAPABILITIES: InkColorCapabilities = {
  default_mode: 'auto-contrast-v1', automatic_recoloring: false,
  supported_layouts: ['natural-layout-v1'], supported_source_policies: ['m2_required'],
  source_solid: { available: true, reason: null, palettes: [
    { id: 'train-magenta-v1', label: '训练原图品红', rgb: [196, 26, 180], role: 'train', binding: INK_BINDING },
    { id: 'train-violet-v1', label: '训练原图蓝紫', rgb: [47, 12, 221], role: 'train', binding: { ...INK_BINDING, palette_id: 'train-violet-v1', rgb: [47, 12, 221], source_image_sha256: '6'.repeat(64), profile_sha256: '7'.repeat(64) } },
  ] },
}
export const INK_RENDER_CAPABILITIES: RenderCapabilities = { ...M2_CAPABILITIES,
  artifact: { artifact_id: 12, engine_version: 'fixture', manifest_sha256: 'b'.repeat(64) }, ink_colors: INK_CAPABILITIES }
export const FROZEN_INK: FrozenRenderSettings = { style_mode: 'profile', style_id: 7, glyph_source_policy: 'm2_required',
  layout_profile: 'natural-layout-v1', layout_seed: 123, layout_profile_sha256: 'a'.repeat(64),
  ink_color_mode: 'source-solid-srgb-v1', ink_palette_id: 'train-magenta-v1', ink_color_binding: INK_BINDING, ink_color_binding_sha256: '8'.repeat(64) }
