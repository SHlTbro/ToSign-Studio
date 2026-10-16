import { ApiRequestError, handleResponseAuth, type ApiEnvelope } from './http'
export interface OutputEvidenceContext {
  status: 'READY' | 'BLOCKED'; package_ok: boolean; target_binding: { ok: boolean }; candidate_id?: string
  detail?: string; model_sha256?: string; profile_id?: string; item_id?: string; render_version?: number
  acceptance?: { status: string }; images: { url: string; label: string }[]; may_publish: false
}
export async function importOutputEvidence(path: string, file: File, signal: AbortSignal): Promise<{ package_id: string; package_ok: boolean }> {
  if (!file.size || file.size > 16 * 1024 * 1024) throw new Error('证据包应小于 16MB')
  const bytes = await file.arrayBuffer()
  const hash = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))).map(v => v.toString(16).padStart(2, '0')).join('')
  if (signal.aborted) throw new DOMException('已取消', 'AbortError')
  const response = await fetch(path, { method: 'POST', credentials: 'include', signal,
    headers: { 'Content-Type': 'application/zip', 'X-Requested-With': 'XMLHttpRequest', 'X-Evidence-SHA256': hash }, body: bytes })
  const result = await response.json()
  if (!response.ok) {
    handleResponseAuth(response.status, result.code)
    throw new ApiRequestError(response.status, result.code ?? 'OWNER_EVIDENCE_PACKAGE_INVALID', false, result.detail || result.title || '证据导入失败')
  }
  const value = (result as ApiEnvelope<{ package_id: string; package_ok: boolean }>).data
  if (!value.package_ok || value.package_id !== hash) throw new Error('证据包校验未完成')
  return value
}
