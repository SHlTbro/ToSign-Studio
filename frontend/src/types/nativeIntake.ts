import type { Region } from '@/types/domain'
export interface IntakeAsset { asset_id: string; sha256: string; type: string; status: string; width: number; height: number }
export interface IntakeStyle { style_id: number; name: string; status: string; source_type: string }
export interface NativeDraftInput { candidate_id: string; source_asset_id: string; source_bbox_norm: Region; draft_alpha_path: string }
export interface SourceDeclaration { style_id: number; source_sha256: string; origin: 'user_supplied_attachment'; history_status: 'UNKNOWN'; allow_unknown_history: true; reason: string; actor_kind: 'delegated_ai' }
export interface SourceAdmission { schema: string; user_id: number; style_id: number; asset_id: string; source_sha256: string; source_kind: string; history_status: string; actor_kind: string; actor: string; receipt_sha256: string; does_not_approve_glyphs: boolean }
export interface NativeCandidate extends NativeDraftInput { image_sha256: string; source_image_sha256: string; source_context_sha256: string; candidate_version_sha256: string; source_admission_sha256: string }
export interface NativeJobPackage { schema: 'native-candidate-review-job/1'; job_id: string; user_id: number; style_id: number; nonce: string; created_at: string; policy_sha256: string; items: NativeCandidate[]; human_confirmation: false; canonical_samples_created: false }
export interface NativeDecision { candidate_id: string; review_status: string; recommendation: string; reviewed_label: string | null; reason: string; isolation_reasons: string[]; canonical_sample_id: string | null; owner_confirmed: false; reviewer?: { provider?: string; model_id?: string; reviewer_kind?: string }; source_review_sha256?: string; touchup_decision: string }
export interface NativeApplyResult { job_id: string; status: 'APPLIED'; bundle_sha256: string; accepted: number; isolated: number; items: NativeDecision[]; model_trained: false; owner_confirmations_created: false }
export interface NativeReviewCapabilities { request_export: true; http_raw_review_import: false; trusted_import: 'operator_authenticated_hash_pinned_cli'; online_provider_status: 'NOT_CONFIGURED'; export_is_review_acceptance: false }
export interface NativeReviewJob { code_version_matches?: boolean; capabilities?: NativeReviewCapabilities; job_id: string; status: string; package_sha256: string; package: NativeJobPackage; imported_bundle_sha256?: string | null; result?: NativeApplyResult | null }
export interface NativeReviewExport { blob: Blob; filename: string; archive_sha256: string; package_sha256: string; code_version_matches: boolean }
export interface NativeJobSummary { code_version_matches: boolean; reason?: string; job_id: string; style_id: number; status: string; created_at: string; package_sha256: string; imported_bundle_sha256: string | null; result_summary: { accepted?: number; isolated?: number } | null }
export interface NativeJobList { items: NativeJobSummary[]; next_after_job_id: string | null }
export interface NativeRef { job_id: string; candidate_id: string }
export interface NativeVersion { ref: NativeRef; sample_id: string; source_asset_id: string; parent_source_hash: string; source_bbox_norm: Region; label: string; image_sha256: string; candidate_version_sha256: string; review_receipt_sha256: string; canonical_provenance_sha256: string; source_admission_sha256: string }
export interface OccurrenceInventory { native_intake_sha256: string | null; versions: NativeVersion[]; selection_required: boolean }
export interface OccurrenceGroup { origin: NativeRef; revisions: NativeRef[]; selected: NativeRef }
export interface OccurrenceRequest { native_intake_sha256: string; groups: OccurrenceGroup[] }
export interface OccurrenceReceipt { schema: 'native-occurrence-selection/1'; user_id: number; style_id: number; native_intake_sha256: string; plan_sha256: string; selected_sample_ids: string[]; excluded_sample_ids: string[]; preserves_all_review_rows: true; authority: { actor_kind: string; human_review: false; changes_review_decisions: false } }

export interface NativeReviewIngress {
  job_id: string
  package_sha256: string
  request: { request_id: string; request_sha256: string; nonce: string; created_at: string } | null
  status: 'not_requested' | 'requested' | 'staged' | 'imported' | 'cancelled' | 'blocked'
  reason: { code: string; message: string } | null
  stage: { bundle_sha256: string; reviewer: { provider: string; model_id: string; reviewer_kind: string }; source_review_sha256: string; reviewed_at: string; staged_at: string } | null
  capabilities: { visible_request: true; http_raw_review_import: false; trusted_import: 'operator_staged_hash_pinned_selection'; online_provider_status: 'NOT_CONFIGURED'; automatic_review_dispatch: false }
  canonical_samples_created: false
}
