/**
 * 硬验收⑤ / R14：轮询双启动竞态——旧代际响应丢弃，不写 state（onData 不触发）；
 * 失败退避翻倍、成功复位；stop/onUnmounted 停止。
 */
import { effectScope } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { usePolling, type PollOutcome } from '@/composables/usePolling'

interface Deferred<T> {
  promise: Promise<T>
  resolve: (v: T) => void
  reject: (e?: unknown) => void
}
function deferred<T>(): Deferred<T> {
  let resolve!: (v: T) => void
  let reject!: (e?: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}
const flush = () => new Promise((r) => setTimeout(r, 0))

describe('R14 usePolling 代际号防竞态', () => {
  beforeEach(() => vi.useRealTimers())

  it('双启动：旧代际迟到响应被丢弃，只有新代际 onData 写 state', async () => {
    const inflight: Deferred<PollOutcome<string>>[] = []
    const fn = vi.fn(() => {
      const d = deferred<PollOutcome<string>>()
      inflight.push(d)
      return d.promise
    })
    const writes: string[] = []

    const scope = effectScope()
    const poller = scope.run(() =>
      usePolling(fn, { base: 10_000, immediate: false, onData: (d) => writes.push(d) }),
    )!

    poller.start() // 代际 1，请求 1 在途
    poller.start() // 双启动 → 代际 2，请求 2 在途；代际 1 立即过期
    expect(fn).toHaveBeenCalledTimes(2)
    const genAfterRestart = poller.generation()

    // 新代际先返回
    inflight[1].resolve({ done: false, data: 'new' })
    await flush()
    // 旧代际后返回（迟到响应）
    inflight[0].resolve({ done: false, data: 'old' })
    await flush()

    expect(writes).toEqual(['new']) // old 被丢弃，不写 state
    expect(poller.generation()).toBe(genAfterRestart)

    poller.stop()
    scope.stop()
  })

  it('stop 后在途响应同样丢弃', async () => {
    const d = deferred<PollOutcome<number>>()
    const fn = vi.fn(() => d.promise)
    const writes: number[] = []
    const scope = effectScope()
    const poller = scope.run(() =>
      usePolling(fn, { immediate: false, onData: (v) => writes.push(v) }),
    )!
    poller.start()
    poller.stop()
    d.resolve({ done: false, data: 99 })
    await flush()
    expect(writes).toEqual([])
    expect(poller.isRunning()).toBe(false)
    scope.stop()
  })

  it('失败退避翻倍（base→2base→4base），成功后复位 base', async () => {
    vi.useFakeTimers()
    const seq: Array<'err' | 'ok' | 'done'> = ['err', 'err', 'ok', 'done']
    const fn = vi.fn(async () => {
      const s = seq.shift()!
      if (s === 'err') throw new Error('boom')
      if (s === 'done') return { done: true }
      return { done: false, data: 'tick' }
    })
    const writes: string[] = []
    const scope = effectScope()
    scope.run(() => usePolling(fn, { base: 100, cap: 800, onData: (d) => writes.push(d) }))

    expect(fn).toHaveBeenCalledTimes(1) // 立即首发；首次失败 delay=base×2=200
    await vi.advanceTimersByTimeAsync(200)
    expect(fn).toHaveBeenCalledTimes(2)
    await vi.advanceTimersByTimeAsync(400) // 再次失败翻倍 400
    expect(fn).toHaveBeenCalledTimes(3)
    await vi.advanceTimersByTimeAsync(100) // 成功复位 base=100
    expect(fn).toHaveBeenCalledTimes(4) // done，终止
    expect(writes).toEqual(['tick'])
    await vi.advanceTimersByTimeAsync(5000)
    expect(fn).toHaveBeenCalledTimes(4) // 终止后不再轮询
    scope.stop()
  })

  it('终态仍提交最后一份数据，然后停止且不再调度', async () => {
    vi.useFakeTimers()
    const fn = vi.fn(async () => ({ done: true, data: 'completed' }))
    const writes: string[] = []
    const scope = effectScope()
    const poller = scope.run(() => usePolling(fn, { base: 100, onData: (d) => writes.push(d) }))!

    await Promise.resolve()
    await Promise.resolve()
    expect(writes).toEqual(['completed'])
    expect(poller.isRunning()).toBe(false)
    expect(vi.getTimerCount()).toBe(0)
    expect(fn).toHaveBeenCalledTimes(1)
    scope.stop()
  })

  it('组件卸载（onUnmounted）自动停止轮询', async () => {
    vi.useFakeTimers()
    const fn = vi.fn(async () => ({ done: false }))
    const scope = effectScope()
    const poller = scope.run(() => usePolling(fn, { base: 100 }))!
    expect(poller.isRunning()).toBe(true)
    scope.stop() // 模拟组件 unmount
    expect(poller.isRunning()).toBe(false)
    await vi.advanceTimersByTimeAsync(1000)
    expect(fn).toHaveBeenCalledTimes(1)
  })
})
