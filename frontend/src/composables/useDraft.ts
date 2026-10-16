/**
 * useDraft —— 草稿本地持久化（白皮书 §1.2.8；v1.1 R2 替代版）。
 *
 * R2：deep watch 每次 keystroke 全量序列化会阻塞主线程。修正：
 * - 500ms trailing debounce：输入只重置定时器，序列化移出关键路径；
 * - pagehide 强制 flush（切后台/关页前同步落盘）；
 * - onUnmounted 清理定时器与事件监听；
 * - commit() = 落盘后移除草稿（正式提交成功时调用）。
 *
 * 体积纪律：行数上限 200（enqueue/粘贴入口强制），序列化上界 ~40KB。
 */
import { computed, onScopeDispose, ref, unref, watch, type Ref } from 'vue'

const DEBOUNCE_MS = 500
const PREFIX = 'draft:'

function load<T>(storageKey: string): T | null {
  const raw = localStorage.getItem(storageKey)
  if (raw === null) return null
  try {
    return JSON.parse(raw) as T
  } catch {
    return null
  }
}

export interface UseDraftReturn<T> {
  data: Ref<T>
  /** 立即把当前值写入 localStorage（忽略 debounce） */
  flush: () => void
  /** 正式提交：先 flush 再移除草稿键 */
  commit: () => void
  /** Resume persistence only after an explicit user edit after submission. */
  resume: () => void
  hasDraft: () => boolean
  /** 测试/换键清理用：取消挂起定时器 */
  cancelPending: () => void
}

export function useDraft<T>(key: string | Ref<string>, initial: T): UseDraftReturn<T> {
  const storageKey = computed(() => PREFIX + unref(key))
  const data = ref<T>(load<T>(storageKey.value) ?? structuredClone(initial)) as Ref<T>
  let timer: number | null = null
  let committed = false

  const persist = (value: T): void => {
    if (committed) return
    localStorage.setItem(storageKey.value, JSON.stringify(value))
  }

  const cancelPending = (): void => {
    if (timer !== null) {
      window.clearTimeout(timer)
      timer = null
    }
  }

  // Route reuse must flush the old asset and load the next asset independently.
  watch(storageKey, (nextKey, previousKey) => {
    cancelPending()
    if (!committed) localStorage.setItem(previousKey, JSON.stringify(data.value))
    committed = false
    data.value = load<T>(nextKey) ?? structuredClone(initial)
  }, { flush: 'sync' })

  // 500ms trailing debounce
  watch(
    data,
    (v) => {
      cancelPending()
      timer = window.setTimeout(() => {
        timer = null
        persist(v)
      }, DEBOUNCE_MS)
    },
    { deep: true },
  )

  const flush = (): void => {
    cancelPending()
    persist(data.value)
  }

  // pagehide：切后台/关闭前同步落盘
  const onPageHide = (): void => flush()
  window.addEventListener('pagehide', onPageHide)

  // onScopeDispose：组件 unmount 时 setup 作用域销毁即触发（等价 onUnmounted 清理）
  onScopeDispose(() => {
    if (!committed) flush()
    cancelPending()
    window.removeEventListener('pagehide', onPageHide)
  })

  const commit = (): void => {
    // Successful submission must remove the draft. A pending debounce, pagehide,
    // or scope disposal must not recreate it after this point.
    committed = true
    cancelPending()
    localStorage.removeItem(storageKey.value)
  }

  const resume = (): void => { committed = false; flush() }

  return {
    data,
    flush,
    commit,
    resume,
    hasDraft: () => localStorage.getItem(storageKey.value) !== null,
    cancelPending,
  }
}
