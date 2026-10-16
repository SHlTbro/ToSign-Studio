import { ENDPOINT_NATIVE_REVIEW_ALPHA, ENDPOINT_NATIVE_REVIEW_CONTEXT, ENDPOINT_NATIVE_REVIEW_EXPORT, fill } from '@/api/endpoints'
import { ApiRequestError, handleResponseAuth } from '@/api/http'
import type { Region } from '@/types/domain'
import type { NativeDraftInput, NativeReviewJob, NativeReviewExport, NativeRef, OccurrenceGroup, OccurrenceInventory } from '@/types/nativeIntake'
export const isHash = (value: unknown): value is string => typeof value === 'string' && /^[0-9a-f]{64}$/.test(value)
export const isJobId = (value: unknown): value is string => typeof value === 'string' && /^[0-9a-f]{32}$/.test(value)
export const refKey = (ref: NativeRef): string => `${ref.job_id}:${ref.candidate_id}`
export const sameRef = (a: NativeRef, b: NativeRef): boolean => a.job_id === b.job_id && a.candidate_id === b.candidate_id
export function isBox(box: Region): boolean {
  return Boolean(box && [box.x_norm, box.y_norm, box.w_norm, box.h_norm].every(Number.isFinite)
    && box.x_norm >= 0 && box.y_norm >= 0 && box.w_norm > 0 && box.h_norm > 0
    && box.x_norm + box.w_norm <= 1 && box.y_norm + box.h_norm <= 1)
}
export function boxesOverlap(a: Region, b: Region): boolean {
  return isBox(a) && isBox(b) && Math.min(a.x_norm + a.w_norm, b.x_norm + b.w_norm) > Math.max(a.x_norm, b.x_norm)
    && Math.min(a.y_norm + a.h_norm, b.y_norm + b.h_norm) > Math.max(a.y_norm, b.y_norm)
}
export function imagePath(ref: NativeRef, kind: 'alpha' | 'source-context'): string {
  return fill((kind === 'alpha' ? ENDPOINT_NATIVE_REVIEW_ALPHA : ENDPOINT_NATIVE_REVIEW_CONTEXT).path,
    { id: encodeURIComponent(ref.job_id), candidateId: encodeURIComponent(ref.candidate_id) })
}
export function assertJob(job: NativeReviewJob, userId: string, styleId: number, requestedId?: string): void {
  if (!job || !isJobId(job.job_id) || (requestedId && requestedId !== job.job_id) || !isHash(job.package_sha256)
    || job.package?.schema !== 'native-candidate-review-job/1' || job.package.job_id !== job.job_id
    || String(job.package.user_id) !== userId || job.package.style_id !== styleId || !isJobId(job.package.nonce)
    || !isHash(job.package.policy_sha256) || job.package.human_confirmation !== false || job.package.canonical_samples_created !== false
    || !Array.isArray(job.package.items) || !job.package.items.length || job.package.items.length > 100
    || new Set(job.package.items.map((item) => item.candidate_id)).size !== job.package.items.length
    || job.package.items.some((item) => !/^[A-Za-z0-9_-]{1,80}$/.test(item.candidate_id) || !item.source_asset_id || !isBox(item.source_bbox_norm)
      || !isHash(item.image_sha256) || !isHash(item.source_image_sha256) || !isHash(item.source_context_sha256) || !isHash(item.candidate_version_sha256)
      || !new RegExp(`^users/${userId}/styles/drafts/[0-9a-f]{16}\\.png$`).test(item.draft_alpha_path))
    || (job.imported_bundle_sha256 != null && !isHash(job.imported_bundle_sha256))) throw new Error('审核任务身份、来源或版本不匹配；已阻止使用')
  if (job.capabilities && (job.capabilities.request_export !== true || job.capabilities.http_raw_review_import !== false
    || job.capabilities.trusted_import !== 'operator_authenticated_hash_pinned_cli' || job.capabilities.online_provider_status !== 'NOT_CONFIGURED'
    || job.capabilities.export_is_review_acceptance !== false)) throw new Error('审核服务能力与当前受信渠道契约不匹配；已阻止使用')
  if (job.result && (job.result.job_id !== job.job_id || job.result.status !== 'APPLIED'
    || job.result.bundle_sha256 !== job.imported_bundle_sha256 || job.result.owner_confirmations_created !== false || job.result.model_trained !== false
    || !Array.isArray(job.result.items) || job.result.items.length !== job.package.items.length
    || !Number.isSafeInteger(job.result.accepted) || !Number.isSafeInteger(job.result.isolated)
    || job.result.accepted !== job.result.items.filter((item) => item.canonical_sample_id != null).length
    || job.result.isolated !== job.result.items.filter((item) => item.canonical_sample_id == null).length
    || new Set(job.result.items.map((item) => item.candidate_id)).size !== job.package.items.length
    || job.result.items.some((item) => !job.package.items.some((candidate) => candidate.candidate_id === item.candidate_id)
      || item.owner_confirmed !== false || (item.canonical_sample_id != null && (item.review_status !== 'ai_confirmed_authorized' || item.recommendation !== 'AI_ACCEPT' || item.touchup_decision !== 'NO_CHANGE_REQUIRED'))))) throw new Error('审核结果不符合当前任务或 AI 来源约束；已阻止使用')
}
export async function exportNativeReviewRequest(job: NativeReviewJob, signal: AbortSignal): Promise<NativeReviewExport> {
  if (job.capabilities?.request_export !== true || !isJobId(job.job_id) || !isHash(job.package_sha256)
    || typeof job.code_version_matches !== 'boolean') throw new Error('服务端尚未确认当前任务的审核请求导出能力')
  const response = await fetch(fill(ENDPOINT_NATIVE_REVIEW_EXPORT.path, { id: encodeURIComponent(job.job_id) }), {
    method: ENDPOINT_NATIVE_REVIEW_EXPORT.method, credentials: 'same-origin', headers: { Accept: 'application/zip, application/problem+json' }, signal,
  })
  if (signal.aborted) throw new DOMException('Export was cancelled', 'AbortError')
  if (!response.ok) {
    const problem = await response.json().catch(() => null) as { code?: string; detail?: string; title?: string } | null
    if (signal.aborted) throw new DOMException('Export was cancelled', 'AbortError')
    handleResponseAuth(response.status, problem?.code)
    throw new ApiRequestError(response.status, problem?.code ?? 'REVIEW_EXPORT_FAILED', false,
      problem?.detail || problem?.title || (response.status === 401 ? '登录已过期，请重新登录' : `审核请求导出失败（HTTP ${response.status}）`))
  }
  const archiveHash = response.headers.get('X-Review-Archive-SHA256')
  if (response.headers.get('Content-Type')?.split(';')[0]?.trim().toLowerCase() !== 'application/zip'
    || response.headers.get('X-Review-Job-Id') !== job.job_id || response.headers.get('X-Review-Package-SHA256') !== job.package_sha256
    || response.headers.get('X-Review-Code-Version-Matches') !== String(job.code_version_matches) || !isHash(archiveHash)) {
    throw new Error('导出响应的任务、版本或文件类型不匹配；已阻止下载')
  }
  const bytes = await response.arrayBuffer()
  if (signal.aborted) throw new DOMException('Export was cancelled', 'AbortError')
  if (!globalThis.crypto?.subtle) throw new Error('浏览器无法校验导出文件 SHA256；请在支持安全校验的浏览器中重试')
  const digest = await crypto.subtle.digest('SHA-256', bytes)
  const actualHash = Array.from(new Uint8Array(digest), (value) => value.toString(16).padStart(2, '0')).join('')
  if (actualHash !== archiveHash) throw new Error('导出文件 SHA256 与服务端记录不一致；已阻止下载')
  if (signal.aborted) throw new DOMException('Export was cancelled', 'AbortError')
  return { blob: new Blob([bytes], { type: 'application/zip' }), filename: `candidate-review-${job.job_id}.zip`, archive_sha256: archiveHash,
    package_sha256: job.package_sha256, code_version_matches: job.code_version_matches }
}
export function sameNativeItems(items: NativeDraftInput[], expected: NativeDraftInput[]): boolean {
  const compact = (rows: NativeDraftInput[]) => rows.map((row) => ({ candidate_id: row.candidate_id, source_asset_id: row.source_asset_id, draft_alpha_path: row.draft_alpha_path,
    source_bbox_norm: [row.source_bbox_norm.x_norm, row.source_bbox_norm.y_norm, row.source_bbox_norm.w_norm, row.source_bbox_norm.h_norm] })).sort((a, b) => a.candidate_id.localeCompare(b.candidate_id))
  return JSON.stringify(compact(items)) === JSON.stringify(compact(expected))
}
export function occurrenceGroups(inventory: OccurrenceInventory, assignment: Record<string, string>, selected: Record<string, string>, origins: Map<string, NativeRef>): OccurrenceGroup[] | null {
  if (!isHash(inventory.native_intake_sha256) || !inventory.versions.length) return null
  const grouped = new Map<string, NativeRef[]>()
  for (const version of inventory.versions) {
    const key = assignment[refKey(version.ref)]
    if (!key || !origins.has(key)) return null
    grouped.set(key, [...(grouped.get(key) ?? []), version.ref])
  }
  const groups: OccurrenceGroup[] = []
  for (const [key, revisions] of grouped) {
    const chosen = revisions.find((ref) => refKey(ref) === selected[key])
    if (!chosen) return null
    groups.push({ origin: origins.get(key)!, revisions, selected: chosen })
  }
  return groups
}
