/**
 * 硬验收⑥ / R2：useDraft 500ms trailing debounce 后才写 localStorage；
 * pagehide 立即 flush；commit 清键；onUnmounted 清理并落盘。
 */
import { effectScope, nextTick } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useDraft } from '@/composables/useDraft'

const KEY = 'draft:t35-draft'

describe('R2 useDraft 防抖持久化', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    localStorage.clear()
  })
  afterEach(() => {
    vi.useRealTimers()
    localStorage.clear()
  })

  it('连续输入只在 500ms 静默后写一次 localStorage', async () => {
    const scope = effectScope()
    const draft = scope.run(() => useDraft('t35-draft', { text: '' }))!

    draft.data.value.text = 'a'
    await nextTick()
    draft.data.value.text = 'ab'
    await nextTick()
    draft.data.value.text = 'abc'
    await nextTick()

    // 500ms 内不落盘
    await vi.advanceTimersByTimeAsync(499)
    expect(localStorage.getItem(KEY)).toBeNull()

    // 满 500ms trailing 才写
    await vi.advanceTimersByTimeAsync(1)
    expect(JSON.parse(localStorage.getItem(KEY) ?? 'null')).toEqual({ text: 'abc' })

    // 后续静默不再重复写
    const setSpy = vi.spyOn(Storage.prototype, 'setItem')
    await vi.advanceTimersByTimeAsync(2000)
    expect(setSpy).not.toHaveBeenCalled()
    setSpy.mockRestore()

    scope.stop()
  })

  it('pagehide 无视 debounce 立即 flush 最新值', async () => {
    const scope = effectScope()
    const draft = scope.run(() => useDraft('t35-draft', { n: 0 }))!
    draft.data.value.n = 42
    await nextTick()
    expect(localStorage.getItem(KEY)).toBeNull() // 仍在 debounce 窗口

    window.dispatchEvent(new Event('pagehide'))
    expect(JSON.parse(localStorage.getItem(KEY) ?? 'null')).toEqual({ n: 42 })

    scope.stop()
  })

  it('hasDraft / commit：提交后移除草稿键', async () => {
    const scope = effectScope()
    const draft = scope.run(() => useDraft('t35-draft', { v: 1 }))!
    expect(draft.hasDraft()).toBe(false)
    draft.data.value.v = 2
    draft.flush()
    expect(draft.hasDraft()).toBe(true)
    draft.commit()
    expect(draft.hasDraft()).toBe(false)
    window.dispatchEvent(new Event('pagehide'))
    scope.stop()
    expect(draft.hasDraft()).toBe(false)
  })

  it('已有草稿在初始化时恢复；onUnmounted flush 后内容不丢', async () => {
    localStorage.setItem(KEY, JSON.stringify({ text: '恢复我' }))
    const scope = effectScope()
    const draft = scope.run(() => useDraft('t35-draft', { text: '' }))!
    expect(draft.data.value.text).toBe('恢复我')

    draft.data.value.text = '卸载前最后值'
    scope.stop() // 触发 onUnmounted → flush
    expect(JSON.parse(localStorage.getItem(KEY) ?? 'null')).toEqual({ text: '卸载前最后值' })
  })
})
