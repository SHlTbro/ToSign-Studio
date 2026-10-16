import { beforeEach, describe, expect, it, vi } from 'vitest'
import { completePendingRender, listPendingRenders, pendingOperationKey, pendingRenderKey, persistPendingRender, type PendingRenderOperation } from '@/api/pendingRender'
const first: PendingRenderOperation = { key: 'first', body: { template_id: 't', items: [{ cn: '  first\n ' }], style_mode: 'profile', style_id: 7,
  glyph_source_policy: 'm2_required', layout_profile: 'natural-layout-v1', layout_seed: 12, ink_color_mode: 'source-solid-srgb-v1', ink_palette_id: 'train-magenta-v1' } }
const second: PendingRenderOperation = { key: 'second', body: { ...first.body, items: [{ cn: ' second\n ' }], ink_palette_id: 'train-violet-v1' } }
beforeEach(() => { localStorage.clear(); vi.restoreAllMocks() })

describe('independently keyed immutable pending render journal', () => {
  it('keeps both simultaneous first-operation snapshots independently durable even if the legacy pointer races', () => {
    persistPendingRender('2', first); persistPendingRender('2', second)
    localStorage.setItem(pendingRenderKey('2'), JSON.stringify(second)) // A cross-process legacy-slot write race cannot erase either journal.
    expect(listPendingRenders('2')).toEqual(expect.arrayContaining([first, second]))
    expect(listPendingRenders('2')).toHaveLength(2)
    expect(localStorage.getItem(pendingOperationKey('2', 'first'))).toBe(JSON.stringify(first))
  })
  it('an old successful response clears only its exact operation, preserving a newer pending operation', () => {
    persistPendingRender('2', first); persistPendingRender('2', second)
    localStorage.setItem(pendingRenderKey('2'), JSON.stringify(second))
    completePendingRender('2', first)
    expect(listPendingRenders('2')).toEqual([second])
    expect(localStorage.getItem(pendingRenderKey('2'))).toBe(JSON.stringify(second))
  })
  it('never overwrites or clears changed content under the same operation key', () => {
    persistPendingRender('2', first)
    const changed = { ...first, body: second.body }
    expect(() => persistPendingRender('2', changed)).toThrow()
    completePendingRender('2', changed)
    expect(listPendingRenders('2')).toEqual([first])
  })
  it('keeps different owners separate even when operation keys match', () => {
    persistPendingRender('2', first); persistPendingRender('3', first)
    completePendingRender('2', first)
    expect(listPendingRenders('2')).toEqual([]); expect(listPendingRenders('3')).toEqual([first])
  })
  it('deduplicates exact legacy and journal copies without dropping another operation', () => {
    localStorage.setItem(pendingRenderKey('2'), JSON.stringify(first))
    persistPendingRender('2', first); persistPendingRender('2', second)
    expect(listPendingRenders('2')).toHaveLength(2)
    expect(localStorage.getItem(pendingRenderKey('2'))).toBe(JSON.stringify(first))
  })
  it('rejects conflicting key/body copies and forged journal-path identities', () => {
    persistPendingRender('2', first)
    localStorage.setItem(pendingRenderKey('2'), JSON.stringify({ ...first, body: second.body }))
    expect(() => listPendingRenders('2')).toThrow()
    localStorage.clear(); localStorage.setItem(pendingOperationKey('2', 'first'), JSON.stringify(second))
    expect(() => listPendingRenders('2')).toThrow()
  })
  it('retains a durable journal before any legacy slot write and fails closed on journal write failure', () => {
    const original = Storage.prototype.setItem
    const writes: string[] = []
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key, value) { writes.push(key); return original.call(this, key, value) })
    persistPendingRender('2', first)
    expect(writes).toEqual([pendingOperationKey('2', 'first'), pendingRenderKey('2')])
    vi.restoreAllMocks(); localStorage.clear()
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('Storage unavailable') })
    expect(() => persistPendingRender('2', first)).toThrow('Storage unavailable')
    vi.restoreAllMocks()
  })
})
