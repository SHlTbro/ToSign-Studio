import { record } from '@/api/m2Runtime'
export { USER_SCR_RECIPE, WINDOWS_SCR_RECIPE, isUserScrRecipe } from '@/api/userScrRecipe'
export function validBudget(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value > 0 && value <= 86400
}
export function preparationPath(styleId: number, jobId?: string): string {
  if (!Number.isSafeInteger(styleId) || styleId <= 0) throw new Error('风格标识无效')
  const path = `/api/v1/styles/${styleId}/scr-preparations`
  return jobId === undefined ? path : `${path}/${encodeURIComponent(jobId)}`
}
export interface ScrPreparation {
  status: 'PREPARING' | 'PREPARED'
  job_id?: string
  preparation_id?: string
  snapshot_sha256?: string
  style_id?: number
  cache_prepared?: boolean
}
/** A job pointer is progress only. Only a bound server result can mean prepared. */
export function assertPreparation(value: unknown, styleId: number, snapshot: string): asserts value is ScrPreparation {
  const row = record(value)
  if (row?.status === 'PREPARING' && typeof row.job_id === 'string' && row.job_id.length > 0 && row.job_id.length <= 200) return
  if (row?.status === 'PREPARED' && row.cache_prepared === true && row.style_id === styleId
    && row.snapshot_sha256 === snapshot && typeof row.preparation_id === 'string' && row.preparation_id.length > 0) return
  throw new Error('SCR 准备结果不完整或与当前风格/快照不匹配；未放行创建')
}
