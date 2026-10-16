import { isHash, isJobId } from '@/api/nativeIntake'
import type { NativeReviewIngress, NativeReviewJob } from '@/types/nativeIntake'

const textValue = (value: unknown): value is string => typeof value === 'string' && Boolean(value.trim())
const dateValue = (value: unknown): boolean => textValue(value) && /(?:Z|\+00:00)$/.test(value) && Number.isFinite(Date.parse(value))
function validReviewer(stage: NativeReviewIngress['stage'], job: NativeReviewJob): boolean {
  const reviewer = stage?.reviewer
  // Compatibility of the server-owned summary only. The server rechecks the
  // configured user and exact reviewer_task_id; this DTO exposes no task ID.
  return reviewer?.reviewer_kind === 'vision_model' && (
    reviewer.provider === 'OpenAI' && reviewer.model_id === 'gpt-6-astra'
    || job.package.user_id === 1 && reviewer.provider === 'CodexLocal' && reviewer.model_id === 'current-session'
  )
}
/** A browser selects an opaque server record; it never constructs review authority. */
export function assertReviewIngress(value: NativeReviewIngress, job: NativeReviewJob): void {
  const caps = value?.capabilities
  const record = value?.request
  const stage = value?.stage
  if (!value || value.job_id !== job.job_id || value.package_sha256 !== job.package_sha256 || !isHash(value.package_sha256)
    || !['not_requested', 'requested', 'staged', 'imported', 'cancelled', 'blocked'].includes(value.status)
    || value.canonical_samples_created !== false || !caps || caps.visible_request !== true || caps.http_raw_review_import !== false
    || caps.trusted_import !== 'operator_staged_hash_pinned_selection' || caps.online_provider_status !== 'NOT_CONFIGURED' || caps.automatic_review_dispatch !== false
    || (value.reason !== null && (!value.reason || !textValue(value.reason.code) || !textValue(value.reason.message)))
    || (record !== null && (!record || !isJobId(record.request_id) || !isHash(record.request_sha256) || !isJobId(record.nonce) || !dateValue(record.created_at)))
    || (stage !== null && (!stage || !record || !isHash(stage.bundle_sha256) || !isHash(stage.source_review_sha256)
      || !validReviewer(stage, job)
      || !dateValue(stage.reviewed_at) || !dateValue(stage.staged_at)))
    || (value.status === 'not_requested' && (record !== null || stage !== null))
    || (value.status === 'requested' && (!record || stage !== null))
    || (['staged', 'imported'].includes(value.status) && (!record || !stage))
    || (value.status === 'blocked' && !value.reason)) throw new Error('审核请求身份、状态或受信证据不符合服务端契约；已阻止操作')
}
export function ingressVersion(value: NativeReviewIngress): string {
  return JSON.stringify([value.job_id, value.package_sha256, value.status, value.request, value.stage, value.reason])
}
