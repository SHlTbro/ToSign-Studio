/**
 * 串行上传队列（白皮书 §1.2.3；v1.1 R13）。
 *
 * R13 铁律：
 * - 每个任务独立 try/catch，单任务最终 reject 只标记自身 error，绝不中断迭代器；
 * - PAUSED（R7 切后台）保留队列位并中止本轮 drain，resume 时从暂停位续跑；
 * - 不做并发上传（弱网串行，白皮书 §1.2.3，YAGNI）。
 *
 * 本件是纯队列编排件，不含 Pinia；assetStore（T36）组合它实现 enqueue/uploadOne。
 */
import { ApiRequestError } from './http'

export type QueueTaskStatus = 'pending' | 'uploading' | 'done' | 'error' | 'paused'

export interface UploadQueueTask {
  localId: string
  status: QueueTaskStatus
  progress: number
  error?: string
}

export interface DrainResult {
  outcome: 'completed' | 'paused'
  /** PAUSED 时暂停在的任务下标（resume 从这里续跑） */
  pausedAtIndex?: number
}

/**
 * 从 startIndex 起串行消费任务。
 * @param tasks  任务数组（原地改 status/progress/error）
 * @param runOne 单任务执行器（通常封装 xhrUpload UploadSession.start）
 */
export async function drainUploadQueue<T extends UploadQueueTask>(
  tasks: T[],
  runOne: (task: T) => Promise<void>,
  startIndex = 0,
): Promise<DrainResult> {
  for (let i = startIndex; i < tasks.length; i++) {
    const t = tasks[i]
    if (t.status === 'done') continue
    t.status = 'uploading'
    t.error = undefined
    try {
      await runOne(t)
      t.status = 'done'
      t.progress = 100
    } catch (e) {
      if (e instanceof ApiRequestError && e.code === 'PAUSED') {
        // 切后台：保留队列位，等待 resume
        t.status = 'paused'
        return { outcome: 'paused', pausedAtIndex: i }
      }
      // 普通失败：标记后继续下一个（R13：不得中断迭代器）
      t.status = 'error'
      t.error = e instanceof Error ? e.message : String(e)
    }
  }
  return { outcome: 'completed' }
}

/** 回前台从暂停位续跑（暂停任务本身按原 attempt 重发） */
export function resumeUploadQueue<T extends UploadQueueTask>(
  tasks: T[],
  runOne: (task: T) => Promise<void>,
  pausedAtIndex: number,
): Promise<DrainResult> {
  return drainUploadQueue(tasks, runOne, pausedAtIndex)
}
