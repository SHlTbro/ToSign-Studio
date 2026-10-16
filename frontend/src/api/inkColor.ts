import type { InkColorBinding, InkColorCapabilities, RenderCapabilities } from '@/types/renderControls'

const PALETTES = ['train-magenta-v1', 'train-violet-v1']
const HASHES = ['artifact_manifest_sha256', 'adapter_sha256', 'snapshot_sha256', 'runtime_binding_sha256',
  'source_image_sha256', 'measurement_sha256', 'profile_sha256', 'implementation_sha256', 'catalog_sha256'] as const
const record = (value: unknown): value is Record<string, unknown> => Boolean(value && typeof value === 'object' && !Array.isArray(value))
const positiveId = (value: unknown): value is number => typeof value === 'number' && Number.isSafeInteger(value) && value > 0
const hash = (value: unknown) => typeof value === 'string' && /^[0-9a-f]{64}$/.test(value)
export const isInkPaletteId = (value: unknown) => typeof value === 'string' && PALETTES.includes(value)
export const validInkRgb = (value: unknown): value is [number, number, number] => Array.isArray(value) && value.length === 3
  && value.every((channel) => typeof channel === 'number' && Number.isInteger(channel) && channel >= 0 && channel <= 255)
export function rgbFromHex(value: unknown): [number, number, number] | null {
  if (typeof value !== 'string' || !/^#?[0-9a-fA-F]{6}$/.test(value)) return null
  const hex = value.replace(/^#/, '')
  return [0, 2, 4].map(offset => parseInt(hex.slice(offset, offset + 2), 16)) as [number, number, number]
}
export function validInkBinding(value: unknown): value is InkColorBinding {
  return record(value) && value.schema === 'author-ink-color-binding/1' && positiveId(value.owner_user_id)
    && positiveId(value.profile_id) && positiveId(value.artifact_id) && isInkPaletteId(value.palette_id)
    && validInkRgb(value.rgb) && HASHES.every((field) => hash(value[field]))
}
// Canonicalize the complete JSON authority, including nested ownership/admission provenance.
// This local comparison is not a cryptographic proof and is never sent as server authority.
function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`
  if (record(value)) return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(',')}}`
  return JSON.stringify(value)
}
export function inkBindingIdentity(value: InkColorBinding): string {
  return canonicalJson(value)
}
export function validInkCapabilities(value: unknown, render: RenderCapabilities, ownerId?: string): value is InkColorCapabilities {
  if (!record(value) || value.default_mode !== 'auto-contrast-v1' || value.automatic_recoloring !== false
    || !Array.isArray(value.supported_layouts) || value.supported_layouts.length !== 1 || value.supported_layouts[0] !== 'natural-layout-v1'
    || !Array.isArray(value.supported_source_policies) || value.supported_source_policies.length !== 1 || value.supported_source_policies[0] !== 'm2_required'
    || !record(value.source_solid)) return false
  const source = value.source_solid
  if (typeof source.available !== 'boolean' || !(source.reason === null || typeof source.reason === 'string') || !Array.isArray(source.palettes)
    || (source.available && source.palettes.length === 0)) return false
  const ids = new Set<string>()
  return source.palettes.every((palette: unknown) => {
    if (!record(palette) || !isInkPaletteId(palette.id) || typeof palette.label !== 'string' || !palette.label.trim()
      || palette.role !== 'train' || !validInkRgb(palette.rgb) || !validInkBinding(palette.binding) || ids.has(palette.id as string)) return false
    const binding = palette.binding
    ids.add(palette.id as string)
    return binding.palette_id === palette.id && binding.rgb.every((channel, index) => channel === (palette.rgb as number[])[index])
      && render.style_mode === 'profile' && render.source_type === 'M2_ADAPTER' && binding.profile_id === render.style_id
      && binding.artifact_id === render.artifact?.artifact_id && binding.artifact_manifest_sha256 === render.artifact?.manifest_sha256
      && (!ownerId || String(binding.owner_user_id) === ownerId)
  })
}
export function validPendingInkSelection(body: Record<string, unknown>): boolean {
  // Server-owned binding must never be replayed as a caller-provided field.
  if (Object.prototype.hasOwnProperty.call(body, 'ink_color_binding') || Object.prototype.hasOwnProperty.call(body, 'ink_color_binding_sha256')) return false
  if (body.ink_color_mode === 'custom-solid-srgb-v1') return body.ink_palette_id === undefined && validInkRgb(body.ink_color_rgb)
  if (Object.prototype.hasOwnProperty.call(body, 'ink_color_rgb')) return false
  if (body.ink_color_mode === undefined || body.ink_color_mode === 'auto-contrast-v1') return body.ink_palette_id === undefined
  return body.ink_color_mode === 'source-solid-srgb-v1' && isInkPaletteId(body.ink_palette_id)
    && body.style_mode === 'profile' && positiveId(body.style_id) && body.glyph_source_policy === 'm2_required'
    && body.layout_profile === 'natural-layout-v1' && typeof body.layout_seed === 'number' && Number.isInteger(body.layout_seed)
    && body.layout_seed >= 0 && body.layout_seed <= 4294967295
}
export const inkHex = (rgb: [number, number, number]) => `#${rgb.map((channel) => channel.toString(16).padStart(2, '0')).join('').toUpperCase()}`
