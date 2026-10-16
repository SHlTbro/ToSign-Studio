import type { RenderCapabilities } from '@/types/renderControls'
// Synthetic capability fixtures exercise UI state only, never qualify a real model.
export const SYSTEM_CAPABILITIES: RenderCapabilities = {
  schema: 'render-style-capabilities/1', style_mode: 'system', style_id: null, source_type: 'M0_SYSTEM', renderable: true, font_ready: true,
  reason: null, source_policies: ['fallback_allowed'], layouts: [{ id: 'baseline-v1', available: true, reason: null, profile_sha256: null },
    { id: 'natural-layout-v1', available: false, reason: 'Natural profile not qualified', profile_sha256: null }], artifact: null,
}
export const M2_CAPABILITIES: RenderCapabilities = {
  ...SYSTEM_CAPABILITIES, style_mode: 'profile', style_id: 7, source_type: 'M2_ADAPTER', source_policies: ['fallback_allowed', 'm2_required'],
  layouts: [{ id: 'baseline-v1', available: true, reason: null, profile_sha256: null }, { id: 'natural-layout-v1', available: true, reason: null, profile_sha256: 'a'.repeat(64) }],
  artifact: { artifact_id: 'synthetic-adapter', engine_version: 'fixture', manifest_sha256: 'b'.repeat(64) },
}
