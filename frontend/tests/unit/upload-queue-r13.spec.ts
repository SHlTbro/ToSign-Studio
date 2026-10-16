/**
 * 硬验收④ / R13：串行队列任务级 try/catch——单任务最终 reject 只标记自身 error，
 * 不中断后续任务；PAUSED 保留队列位，resume 从暂停位续跑。
 */
import { describe, expect, it, vi } from 'vitest'
import { ApiRequestError } from '@/api/http'
import {
  drainUploadQueue,
  resumeUploadQueue,
  type UploadQueueTask,
} from '@/api/uploadQueue'

function makeTask(localId: string): UploadQueueTask {
  return { localId, status: 'pending', progress: 0 }
}

describe('R13 上传队列任务级异常隔离', () => {
  it('任务1最终失败 → 标记 error，任务2/3 照常完成（迭代器不中断）', async () => {
    const tasks = [makeTask('t1'), makeTask('t2'), makeTask('t3')]
    const order: string[] = []
    const runOne = vi.fn(async (t: UploadQueueTask) => {
      order.push(t.localId)
      if (t.localId === 't1') {
        // 模拟重试用尽后的最终 reject（非 PAUSED）
        throw new ApiRequestError(0, 'NETWORK', true, '网络错误')
      }
    })

    const result = await drainUploadQueue(tasks, runOne)

    expect(result.outcome).toBe('completed')
    expect(order).toEqual(['t1', 't2', 't3']) // 后续任务未被堵死
    expect(tasks[0].status).toBe('error')
    expect(tasks[0].error).toBe('网络错误')
    expect(tasks[1].status).toBe('done')
    expect(tasks[2].status).toBe('done')
    expect(runOne).toHaveBeenCalledTimes(3)
  })

  it('连续两个任务失败也不影响后续（铁律：任何单任务失败不得中断迭代器）', async () => {
    const tasks = [makeTask('a'), makeTask('b'), makeTask('c')]
    const runOne = vi.fn(async (t: UploadQueueTask) => {
      if (t.localId !== 'c') throw new ApiRequestError(500, 'EXPORT_FAILED', true, '500')
    })
    await drainUploadQueue(tasks, runOne)
    expect(tasks.map((t) => t.status)).toEqual(['error', 'error', 'done'])
  })

  it('PAUSED：中止本轮 drain 并保留队列位；resume 从暂停位续跑', async () => {
    const tasks = [makeTask('t1'), makeTask('t2'), makeTask('t3')]
    const firstRound = vi.fn(async (t: UploadQueueTask) => {
      if (t.localId === 't1') throw new ApiRequestError(0, 'PAUSED', false, '暂停')
    })

    const r1 = await drainUploadQueue(tasks, firstRound)
    expect(r1).toEqual({ outcome: 'paused', pausedAtIndex: 0 })
    expect(tasks[0].status).toBe('paused')
    expect(tasks[1].status).toBe('pending') // t2/t3 未被消费
    expect(tasks[2].status).toBe('pending')
    expect(firstRound).toHaveBeenCalledTimes(1)

    // 回前台：从暂停位续跑，全部成功
    const secondRound = vi.fn(async () => {})
    const r2 = await resumeUploadQueue(tasks, secondRound, r1.pausedAtIndex ?? 0)
    expect(r2.outcome).toBe('completed')
    expect(tasks.every((t) => t.status === 'done')).toBe(true)
    expect(secondRound).toHaveBeenCalledTimes(3)
  })
})
