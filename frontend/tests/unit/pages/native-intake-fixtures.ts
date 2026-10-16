import type { IntakeAsset, IntakeStyle, NativeDraftInput, NativeReviewJob, NativeApplyResult, NativeVersion, NativeReviewIngress } from '@/types/nativeIntake'
import { envelope, type FetchPlan } from './_harness'
export const JOB = '1'.repeat(32), OTHER_JOB = '2'.repeat(32), HASH = 'a'.repeat(64), IMPORT = 'b'.repeat(64), INTAKE = 'c'.repeat(64)
export const ASSET: IntakeAsset = { asset_id: 'source-a', sha256: HASH, type: 'sample_source', status: 'pending', width: 500, height: 400 }
export const PROFILE: IntakeStyle = { style_id: 9, name: '自己的手写', source_type: 'HANDWRITING_ASSET', status: 'approved' }
export const DRAFT: NativeDraftInput = { candidate_id: 'p4-0123456789abcdef', source_asset_id: ASSET.asset_id, source_bbox_norm: { x_norm: .1, y_norm: .2, w_norm: .3, h_norm: .4 }, draft_alpha_path: 'users/2/styles/drafts/0123456789abcdef.png' }
export function review(id = JOB, overrides: Partial<NativeReviewJob> = {}): NativeReviewJob {
  return { job_id: id, status: 'pending', code_version_matches: true, package_sha256: 'd'.repeat(64), imported_bundle_sha256: null, result: null,
    package: { schema: 'native-candidate-review-job/1', job_id: id, user_id: 2, style_id: 9, nonce: 'e'.repeat(32), created_at: '2026-10-02T09:00:00Z', policy_sha256: 'f'.repeat(64), human_confirmation: false, canonical_samples_created: false,
      items: [{ ...DRAFT, image_sha256: '3'.repeat(64), source_image_sha256: HASH, source_context_sha256: '4'.repeat(64), candidate_version_sha256: '5'.repeat(64), source_admission_sha256: '6'.repeat(64) }] }, ...overrides }
}
export function result(id = JOB): NativeApplyResult {
  return { job_id: id, status: 'APPLIED', bundle_sha256: IMPORT, accepted: 1, isolated: 0, model_trained: false, owner_confirmations_created: false, items: [{ candidate_id: DRAFT.candidate_id, review_status: 'ai_confirmed_authorized', recommendation: 'AI_ACCEPT', reviewed_label: '春', reason: 'Actual test-fixture acceptance', isolation_reasons: [], canonical_sample_id: `sample-${id}`, owner_confirmed: false, reviewer: { provider: 'OpenAI', model_id: 'gpt-6-astra', reviewer_kind: 'vision_model' }, source_review_sha256: '7'.repeat(64), touchup_decision: 'NO_CHANGE_REQUIRED' }] }
}
export function applied(id = JOB): NativeReviewJob { return review(id, { status: 'applied', imported_bundle_sha256: IMPORT, result: result(id) }) }
export function version(id = JOB): NativeVersion {
  return { ref: { job_id: id, candidate_id: DRAFT.candidate_id }, sample_id: `sample-${id}`, source_asset_id: ASSET.asset_id, parent_source_hash: HASH, source_bbox_norm: DRAFT.source_bbox_norm, label: '春', image_sha256: '3'.repeat(64), candidate_version_sha256: '5'.repeat(64), review_receipt_sha256: '8'.repeat(64), canonical_provenance_sha256: '9'.repeat(64), source_admission_sha256: '6'.repeat(64) }
}
function legacyIngress(id: string): NativeReviewIngress {
  return { job_id: id, package_sha256: review(id).package_sha256, request: null, stage: null, reason: null, status: 'not_requested',
    capabilities: { visible_request: true, http_raw_review_import: false, trusted_import: 'operator_staged_hash_pinned_selection', online_provider_status: 'NOT_CONFIGURED', automatic_review_dispatch: false }, canonical_samples_created: false }
}
export function plan(extra: FetchPlan = {}): FetchPlan {
  return { '/api/v1/styles/9/occurrence-selection': envelope({ native_intake_sha256: null, versions: [], selection_required: false }),
    '/api/v1/candidate-review-jobs': envelope(review()),
    '/api/v1/candidate-review-jobs?style_id=9&limit=50': envelope({ items: [], next_after_job_id: null }),
    [`/api/v1/candidate-review-jobs/${JOB}`]: envelope(review()), [`/api/v1/candidate-review-jobs/${OTHER_JOB}`]: envelope(review(OTHER_JOB)),
    [`/api/v1/candidate-review-jobs/${JOB}/review-ingress`]: envelope(legacyIngress(JOB)),
    [`/api/v1/candidate-review-jobs/${OTHER_JOB}/review-ingress`]: envelope(legacyIngress(OTHER_JOB)),
    '/api/v1/training-sources/source-a/admit': envelope({ schema: 'personal-source-admission/1', user_id: 2, style_id: 9, asset_id: ASSET.asset_id, source_sha256: HASH, source_kind: 'sample_source', history_status: 'UNKNOWN', actor_kind: 'delegated_ai', actor: 'ai:delegated:test', receipt_sha256: '0'.repeat(64), does_not_approve_glyphs: true }), ...extra }
}
