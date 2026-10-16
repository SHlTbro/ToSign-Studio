import { describe, expect, it } from 'vitest'
import { inkBindingIdentity, inkHex, validInkBinding, validInkCapabilities, validPendingInkSelection } from '@/api/inkColor'
import { INK_BINDING, INK_CAPABILITIES, INK_RENDER_CAPABILITIES } from './pages/ink-capability-fixtures'
const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value))

describe('strict server-owned ink identity', () => {
  it('accepts only the matching owner, model, palette and train capability without treating extra provenance as client authority', () => {
    const caps = clone(INK_CAPABILITIES); caps.source_solid.palettes[0]!.binding.intake_style_id = 444
    expect(validInkCapabilities(caps, INK_RENDER_CAPABILITIES, '2')).toBe(true)
    expect(validInkCapabilities(caps, INK_RENDER_CAPABILITIES, '3')).toBe(false)
    expect(validInkCapabilities(caps, { ...INK_RENDER_CAPABILITIES, style_id: 8 }, '2')).toBe(false)
    expect(validInkCapabilities(caps, { ...INK_RENDER_CAPABILITIES, source_type: 'M0_SYSTEM' }, '2')).toBe(false)
  })
  it.each([
    ['schema', 'unknown'], ['owner_user_id', '2'], ['profile_id', true], ['artifact_id', 0], ['artifact_id', '12'],
    ['palette_id', 'held-out-green'], ['rgb', [196, 26]], ['rgb', [256, 26, 180]], ['rgb', [196, -1, 180]],
    ['rgb', [196, 1.5, 180]], ['rgb', [196, true, 180]], ['rgb', [196, '26', 180]],
    ...['artifact_manifest_sha256', 'adapter_sha256', 'snapshot_sha256', 'runtime_binding_sha256', 'source_image_sha256',
      'measurement_sha256', 'profile_sha256', 'implementation_sha256', 'catalog_sha256'].map((key) => [key, 'not-a-hash']),
  ] as [string, unknown][])('rejects malformed binding %s=%j', (key, value) => {
    expect(validInkBinding({ ...INK_BINDING, [key as string]: value })).toBe(false)
  })
  it.each([
    { default_mode: 'source-solid-srgb-v1' }, { automatic_recoloring: true }, { supported_layouts: ['baseline-v1'] },
    { supported_source_policies: ['fallback_allowed'] }, { source_solid: { available: true, reason: null, palettes: [] } },
  ])('rejects changed discovery semantics %j', (change) => {
    expect(validInkCapabilities({ ...INK_CAPABILITIES, ...change }, INK_RENDER_CAPABILITIES)).toBe(false)
  })
  it.each(['id', 'rgb', 'role', 'binding.profile_id', 'binding.artifact_id', 'binding.artifact_manifest_sha256', 'binding.palette_id', 'binding.rgb'])('rejects palette mismatch %s', (key) => {
    const caps = clone(INK_CAPABILITIES); const palette = caps.source_solid.palettes[0]!
    const changes: Record<string, unknown> = { id: 'unknown', rgb: [1, 2, 3], role: 'test' }
    if (key.startsWith('binding.')) Object.assign(palette.binding, { [key.split('.')[1]!]: key.endsWith('rgb') ? [1, 2, 3] : key.endsWith('palette_id') ? 'train-violet-v1' : key.endsWith('sha256') ? 'f'.repeat(64) : 999 })
    else Object.assign(palette, { [key]: changes[key] })
    expect(validInkCapabilities(caps, INK_RENDER_CAPABILITIES)).toBe(false)
  })
  it('canonicalizes the complete nested binding, with stable object key order and meaningful array order', () => {
    const a = { ...INK_BINDING, current_owned_source_bindings: [{ source_admission_sha256: 'a'.repeat(64), evidence: { second: 2, first: 1 } }], roles: ['train', 'owned'] }
    const same = { ...INK_BINDING, roles: ['train', 'owned'], current_owned_source_bindings: [{ evidence: { first: 1, second: 2 }, source_admission_sha256: 'a'.repeat(64) }] }
    expect(inkBindingIdentity(a)).toBe(inkBindingIdentity(same))
    expect(inkBindingIdentity({ ...a, roles: ['owned', 'train'] })).not.toBe(inkBindingIdentity(a))
    expect(inkBindingIdentity({ ...a, current_owned_source_bindings: [{ source_admission_sha256: 'b'.repeat(64), evidence: { second: 2, first: 1 } }] })).not.toBe(inkBindingIdentity(a))
  })
  it('rejects duplicate palette IDs and accepts an explicitly unavailable empty catalog', () => {
    const caps = clone(INK_CAPABILITIES); caps.source_solid.palettes.push(caps.source_solid.palettes[0]!)
    expect(validInkCapabilities(caps, INK_RENDER_CAPABILITIES)).toBe(false)
    expect(validInkCapabilities({ ...caps, source_solid: { available: false, reason: 'QA incomplete', palettes: [] } }, INK_RENDER_CAPABILITIES)).toBe(true)
  })
  it('detects changed core identity independent of property insertion order', () => {
    expect(inkBindingIdentity(INK_BINDING)).toBe(inkBindingIdentity(Object.fromEntries(Object.entries(INK_BINDING).reverse()) as typeof INK_BINDING))
    expect(inkBindingIdentity({ ...INK_BINDING, catalog_sha256: '9'.repeat(64) })).not.toBe(inkBindingIdentity(INK_BINDING))
    expect(inkHex(INK_BINDING.rgb)).toBe('#C41AB4')
  })
})

describe('immutable pending request color recovery', () => {
  const body = { style_mode: 'profile', style_id: 7, glyph_source_policy: 'm2_required', layout_profile: 'natural-layout-v1', layout_seed: 123,
    ink_color_mode: 'source-solid-srgb-v1', ink_palette_id: 'train-magenta-v1' }
  it('retains old omitted defaults and valid explicit source fields', () => {
    expect(validPendingInkSelection({})).toBe(true)
    expect(validPendingInkSelection({ ink_color_mode: 'auto-contrast-v1' })).toBe(true)
    expect(validPendingInkSelection(body)).toBe(true)
  })
  it.each([
    { ink_color_binding: INK_BINDING }, { ink_color_binding: null }, { ink_color_binding_sha256: '8'.repeat(64) }, { ink_color_binding_sha256: null }, { ink_palette_id: undefined }, { ink_palette_id: 'test-green' },
    { ink_color_mode: 'unknown' }, { ink_color_mode: undefined }, { ink_color_mode: 'auto-contrast-v1' },
    { layout_profile: 'baseline-v1' }, { glyph_source_policy: 'fallback_allowed' }, { style_mode: 'system' }, { style_id: '7' }, { layout_seed: -1 },
  ])('blocks malformed recovery without normalizing the saved body %j', (change) => {
    expect(validPendingInkSelection({ ...body, ...change })).toBe(false)
  })
})
