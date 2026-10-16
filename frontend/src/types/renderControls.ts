export type GlyphSourcePolicy = 'fallback_allowed' | 'm2_required' | 'm2_cjk_required'
export type LayoutProfile = 'baseline-v1' | 'natural-layout-v1'
export type InkColorMode = 'auto-contrast-v1' | 'source-solid-srgb-v1' | 'custom-solid-srgb-v1'
export type InkPaletteId = 'train-magenta-v1' | 'train-violet-v1'
export interface InkColorBinding {
  schema: 'author-ink-color-binding/1'
  owner_user_id: number
  profile_id: number
  artifact_id: number
  artifact_manifest_sha256: string
  adapter_sha256: string
  snapshot_sha256: string
  runtime_binding_sha256: string
  palette_id: InkPaletteId
  rgb: [number, number, number]
  source_image_sha256: string
  measurement_sha256: string
  profile_sha256: string
  implementation_sha256: string
  catalog_sha256: string
  [provenance: string]: unknown
}
export interface InkPalette { id: InkPaletteId; label: string; rgb: [number, number, number]; role: 'train'; binding: InkColorBinding }
export interface InkColorCapabilities {
  custom_solid?: { available: boolean }
  default_mode: 'auto-contrast-v1'
  source_solid: { available: boolean; reason: string | null; palettes: InkPalette[] }
  supported_layouts: ['natural-layout-v1']
  supported_source_policies: ['m2_required']
  automatic_recoloring: false
}
export interface NonCjkFontCatalog {
  schema: 'non-cjk-font-catalog/1'
  fonts: { id: string; name: string; roles: ('latin' | 'other')[]; available: boolean }[]
  missing_requested_fonts: string[]
}
export interface NonCjkFontBinding {
  schema: 'non-cjk-font-binding/1'
  fonts: Record<'latin' | 'other', { id: string; name: string; sha256: string; license: string; face_index?: number; variation_axes?: Record<string, number> }>
}
export interface RenderCapabilities {
  schema: 'render-style-capabilities/1'
  style_mode: 'system' | 'profile'
  style_id: number | null
  source_type: string
  renderable: boolean
  font_ready: boolean
  reason: string | null
  source_policies: string[]
  layouts: { id: LayoutProfile; available: boolean; reason: string | null; profile_sha256: string | null }[]
  artifact: { artifact_id: number | string; engine_version: string; manifest_sha256: string } | null
  ink_colors?: InkColorCapabilities
  non_cjk_fonts?: NonCjkFontCatalog
  accepted_v3?: { available: boolean; fonts: { latin: string; other: string }; seed: number; rgb: number[];
    binding: { schema: 'accepted-v3-layout-binding/1'; profile_sha256: string; implementation_sha256: string; adapter_sha256: string } }
}
export interface RenderPreferences {
  styleId: string; sourcePolicy: GlyphSourcePolicy; layout: LayoutProfile; seed: number | string; needsExplicitReset?: boolean
  inkMode?: InkColorMode; inkPaletteId?: string; inkPaletteIdentity?: string; inkHex?: string
  mixedFontsEnabled?: boolean; latinFontId?: string; otherFontId?: string
}
export interface RenderSelection {
  style_mode: 'system' | 'profile'
  style_id?: number
  glyph_source_policy: GlyphSourcePolicy
  layout_profile: LayoutProfile
  layout_seed?: number
  ink_color_mode?: InkColorMode
  ink_color_rgb?: [number, number, number]
  ink_palette_id?: InkPaletteId
  non_cjk_fonts?: { latin: string; other: string }
}
export interface FrozenRenderSettings {
  v3_layout_binding?: { schema: string; profile_sha256: string; implementation_sha256: string; adapter_sha256: string }
  text_style?: { ordinary_scale: number; to_ratio: number; header_gap: number }
  handwriting?: { size_variation_pct: number; tilt_variation_deg: number; seed: number }
  style_mode?: 'system' | 'profile' | null
  style_id?: number | null
  glyph_source_policy?: GlyphSourcePolicy | null
  layout_profile?: LayoutProfile | null
  layout_seed?: number | null
  layout_profile_sha256?: string | null
  ink_color_mode?: InkColorMode | null
  ink_color_rgb?: [number, number, number] | null
  ink_palette_id?: InkPaletteId | null
  ink_color_binding?: InkColorBinding | null
  ink_color_binding_sha256?: string | null
  non_cjk_fonts?: { latin: string; other: string }
  non_cjk_font_binding?: NonCjkFontBinding
}
