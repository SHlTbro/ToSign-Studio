/**
 * usePolling —— 退避轮询统一件（白皮书 §1.5.3；v1.1 R14 代际防竞态）。
 *
 * R14 问题：retry/双启动后旧 poll timer 未销毁，双循环交替写 state、请求翻倍。
 * 修正：
 * - 代际号 generation：每次 start()/stop() 自增；每次 fn 发起前捕获 myGen，
 *   await 返回后若 myGen !== 当前代际，响应直接丢弃——不调 onData、不续 timer，
 *   保证旧代际响应永不覆盖新 state；
 * - 成功未终止 → 延迟复位 base；失败 → 延迟翻倍封顶 cap；
 * - onUnmounted 自动 stop，组件卸载零残留。
 *
 * 写 state 的唯一通道是 onData 回调（仅当前代际触发）；store 持有时可在
 * onData 内比对自身 pollGen（白皮书 R14 store 形态）。
 */
import { onScopeDispose } from 'vue'

export interface PollOutcome<T> {
  done: boolean
  data?: T
}

export interface UsePollingOptions<T> {
  /** 初始/成功后基础间隔 ms */
  base?: number
  /** 失败退避上限 ms */
  cap?: number
  /** 当前代际数据回调（过期代际不会触发） */
  onData?: (data: T) => void
  /** 是否创建即启动，默认 true */
  immediate?: boolean
}

export interface UsePollingReturn {
  /** 启动/重启轮询；重复调用使在途旧代际失效（R14） */
  start: () => void
  stop: () => void
  /** 当前代际号（测试/与 store pollGen 对账用） */
  generation: () => number
  isRunning: () => boolean
}

export function usePolling<T>(
  fn: () => Promise<PollOutcome<T>>,
  baseOrOptions?: number | UsePollingOptions<T>,
  capArg?: number,
): UsePollingReturn {
  const opts: UsePollingOptions<T> =
    typeof baseOrOptions === 'object' ? baseOrOptions : {}
  const base =
    (typeof baseOrOptions === 'number' ? baseOrOptions : opts.base) ?? 2000
  const cap = capArg ?? opts.cap ?? 10_000
  const onData = opts.onData
  const immediate = opts.immediate ?? true

  let timer: number | null = null
  let stopped = true
  let delay = base
  let gen = 0

  function clearTimer(): void {
    if (timer !== null) {
      window.clearTimeout(timer)
      timer = null
    }
  }

  function schedule(myGen: number): void {
    clearTimer()
    timer = window.setTimeout(() => {
      void run(myGen)
    }, delay)
  }

  async function run(myGen: number): Promise<void> {
    if (stopped || myGen !== gen) return // 旧代际调度到期直接丢弃
    try {
      const r = await fn()
      // await 期间可能已被 start()/stop() 换代——过期响应丢弃，不写 state
      if (stopped || myGen !== gen) return
      // 终态也携带最终状态（例如 completed/error）；先提交最后一份数据，再停轮询。
      if (r.data !== undefined && onData) onData(r.data)
      if (r.done) {
        stopped = true
        clearTimer()
        return
      }
      delay = base // 成功复位
      schedule(myGen)
    } catch {
      if (stopped || myGen !== gen) return
      delay = Math.min(delay * 2, cap) // 失败指数退避
      schedule(myGen)
    }
  }

  function start(): void {
    gen += 1 // 使任何在途旧代际失效
    stopped = false
    delay = base
    clearTimer()
    void run(gen)
  }

  function stop(): void {
    gen += 1
    stopped = true
    clearTimer()
  }

  // onScopeDispose 在组件 setup 作用域中于组件 unmount 时触发（等价 onUnmounted），
  // 同时覆盖 effectScope() 手动停止场景，保证轮询零残留。
  onScopeDispose(stop)

  if (immediate) start()

  return {
    start,
    stop,
    generation: () => gen,
    isRunning: () => !stopped,
  }
}
