import { validPendingInkSelection } from '@/api/inkColor'

export interface PendingRenderOperation { key: string; body: Record<string, unknown> }
export const pendingRenderKey = (userId: string) => `yy:p5:pending-render:${userId}`
export const pendingOperationKey = (userId: string, key: string) => `${pendingRenderKey(userId)}:operation:${encodeURIComponent(key)}`
const invalid = () => new Error('未确认提交的记录不完整，请保留记录并检查，不能自动创建新请求')
export function decodePending(raw: string): PendingRenderOperation {
  const saved = JSON.parse(raw)
  if (!saved || typeof saved.key !== 'string' || !saved.key || !saved.body || typeof saved.body !== 'object' || Array.isArray(saved.body)
    || typeof saved.body.template_id !== 'string' || !Array.isArray(saved.body.items) || !saved.body.items.length
    || !['system', 'profile'].includes(saved.body.style_mode) || !['fallback_allowed', 'm2_required', 'm2_cjk_required'].includes(saved.body.glyph_source_policy)
    || !['baseline-v1', 'natural-layout-v1'].includes(saved.body.layout_profile) || !validPendingInkSelection(saved.body)) throw invalid()
  if (saved.body.glyph_source_policy === 'm2_cjk_required' && (saved.body.style_mode !== 'profile'
    || !Number.isSafeInteger(saved.body.style_id) || saved.body.style_id < 1 || saved.body.layout_profile !== 'baseline-v1'
    || saved.body.layout_seed !== undefined)) throw invalid()
  return { key: saved.key, body: saved.body }
}
export const samePending = (left: PendingRenderOperation, right: PendingRenderOperation) => left.key === right.key && JSON.stringify(left.body) === JSON.stringify(right.body)
export function listPendingRenders(userId: string): PendingRenderOperation[] {
  const found = new Map<string, PendingRenderOperation>()
  const prefix = `${pendingRenderKey(userId)}:operation:`
  const keys = Array.from({ length: localStorage.length }, (_, index) => localStorage.key(index)).filter((key): key is string => key !== null && (key === pendingRenderKey(userId) || key.startsWith(prefix)))
  for (const key of keys) {
    const raw = localStorage.getItem(key)
    if (raw === null) continue
    const value = decodePending(raw)
    if (key !== pendingRenderKey(userId) && key !== pendingOperationKey(userId, value.key)) throw invalid()
    const prior = found.get(value.key)
    if (prior && !samePending(prior, value)) throw invalid()
    found.set(value.key, value)
  }
  return [...found.values()]
}
export function persistPendingRender(userId: string, operation: PendingRenderOperation): void {
  // A unique immutable journal entry is authoritative. Concurrent operations never share it.
  const key = pendingOperationKey(userId, operation.key)
  const raw = JSON.stringify(operation)
  const existing = localStorage.getItem(key)
  if (existing !== null && !samePending(decodePending(existing), operation)) throw invalid()
  if (existing === null) localStorage.setItem(key, raw)
  if (localStorage.getItem(key) !== (existing ?? raw)) throw new Error('未确认提交保存失败，已阻止发送')
  // Legacy single-operation slot remains compatible, but is never overwritten by a different operation.
  if (localStorage.getItem(pendingRenderKey(userId)) === null) localStorage.setItem(pendingRenderKey(userId), raw)
}
export function completePendingRender(userId: string, operation: PendingRenderOperation): void {
  for (const key of [pendingOperationKey(userId, operation.key), pendingRenderKey(userId)]) {
    const raw = localStorage.getItem(key)
    if (raw === null) continue
    try { if (samePending(decodePending(raw), operation)) localStorage.removeItem(key) } catch { /* Keep an unrelated malformed record for explicit recovery. */ }
  }
}
