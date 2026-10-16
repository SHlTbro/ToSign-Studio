import { request } from './http'
import { ENDPOINT_STYLE_RUN_RECOVERY, ENDPOINT_STYLE_RUN_RETRY, ENDPOINT_STYLE_RUN_RESUME, fill } from './endpoints'
import type { StyleRun } from '@/types/domain'

export interface RunCheckpoint {
  checkpoint_id: number
  position: { epoch: number; global_step: number; batch_ptr: { epoch: number; batch_idx: number } }
  verified: boolean
  failure_code?: string
}
export interface RunRecovery {
  run_id: number
  status: string
  execution_state: string
  checkpoints: RunCheckpoint[]
  continuation_run_id: number | null
  retry_allowed: boolean
  resume_allowed: boolean
  resume_gate: string
  claims_quality_pass: false
}
export interface PendingContinuation { version: 1; parent_run_id: number; key: string; mode: 'retry' | 'resume'; checkpoint_id?: number }
export function recoveryStorageKey(userId: string | number, runId: number): string { return `yy:p7:continuation:${encodeURIComponent(String(userId))}:${runId}` }
export function parsePendingContinuation(raw: string | null, runId: number): PendingContinuation | null {
  if (raw === null) return null
  const value: unknown = JSON.parse(raw)
  if (!value || typeof value !== 'object') throw new Error('恢复请求记录无效，请保留记录并重新读取服务端状态')
  const row = value as Record<string, unknown>
  const keys = row.mode === 'resume' ? ['version', 'parent_run_id', 'key', 'mode', 'checkpoint_id'] : ['version', 'parent_run_id', 'key', 'mode']
  if (row.version !== 1 || row.parent_run_id !== runId || typeof row.key !== 'string' || !/^[A-Za-z0-9_.:-]{8,128}$/.test(row.key)
    || !['retry', 'resume'].includes(String(row.mode)) || Object.keys(row).some(key => !keys.includes(key))
    || (row.mode === 'resume' && (typeof row.checkpoint_id !== 'number' || !Number.isSafeInteger(row.checkpoint_id) || row.checkpoint_id < 1))) {
    throw new Error('恢复请求记录与当前运行不一致，已阻止重新提交')
  }
  return row as unknown as PendingContinuation
}
export function readRecovery(runId: number): Promise<RunRecovery> {
  return request<RunRecovery>(fill(ENDPOINT_STYLE_RUN_RECOVERY.path, { id: String(runId) }), { retries: 0, timeoutMs: 60000 })
}
export function continueOwnedRun(pending: PendingContinuation): Promise<StyleRun> {
  const endpoint = pending.mode === 'resume' ? ENDPOINT_STYLE_RUN_RESUME : ENDPOINT_STYLE_RUN_RETRY
  return request<StyleRun>(fill(endpoint.path, { id: String(pending.parent_run_id) }), {
    method: 'POST', body: pending.mode === 'resume' ? { checkpoint_id: pending.checkpoint_id } : {},
    idempotencyKey: pending.key, retries: 0, timeoutMs: 60000,
  })
}
