/**
 * 领域类型 —— 单一事实来源（白皮书 §1.1；v1.1 修订后保持单源）。
 *
 * 纪律：
 * - 所有 store / api / 组件只允许从本文件 import 领域类型，禁止在别处重新声明。
 * - store state 只存领域类型，不存响应信封；分页在 api 层拆包（Paged<T>）。
 * - 错误码联合类型 ApiErrorCode 对齐：contract-v0.2（26 行）+ v0.2.1 新增
 *   + v0.3-patch-A（OCR/DETECT/EXTRACT）+ v0.3-patch-B（RUN_*）+ 前端合成码
 *   （TIMEOUT/NETWORK/UNKNOWN/PAUSED）。前端不得发明契约外业务错误码。
 */

// ---------- §1.1 白皮书领域类型（全量） ----------

export type Role = 'admin' | 'user'

export interface User {
  user_id: string
  username: string
  display_name: string
  role: Role
  must_change_password: boolean
}

export type AssetType = 'cos' | 'sample_source'

export type AssetStatus = 'pending' | 'draft' | 'pending_review' | 'approved' | 'rejected'

export interface Asset {
  asset_id: string
  type: AssetType
  status: AssetStatus
  sha256: string
  bytes: number
  width: number
  height: number
  thumbnail_path: string
  created_at: string
  approved_at?: string
}

/** 归一化落点区域，值域 [0,1]，相对底图宽高 */
export interface Region {
  x_norm: number
  y_norm: number
  w_norm: number
  h_norm: number
}

export type SlotKey = 'to_cn' | 'blessing' | 'signature'

export interface TextStyle {
  ordinary_scale: number
  to_ratio: number
  header_gap?: number
}

export interface Slot {
  key: SlotKey
  text_template: string
  text_style?: TextStyle
  region: Region
}

export type TemplateStatus = 'draft' | 'pending_review' | 'approved' | 'rejected'

export interface LayoutTemplate {
  template_id: string
  name: string
  version: number
  status: TemplateStatus
  base_asset_id: string
  slots: Slot[]
  canvas_width: number
  canvas_height: number
}

export type StyleSourceType =
  | 'M0_SYSTEM'
  | 'M1_FROZEN'
  | 'HANDWRITING_ASSET'
  | 'M2_ADAPTER'

export type StyleStatus = 'draft' | 'pending_review' | 'approved' | 'rejected'

export interface StyleProfile {
  style_id: number
  name: string
  source_type: StyleSourceType
  status: StyleStatus
  enabled: boolean
  quality_status?: string | null
  coverage: number | null
  model_version: string | null
  receipt_id: string | null
  font_url?: string | null
  reject_reason?: string | null
  created_at?: string
}

export type StyleRunStatus =
  | 'created'
  | 'queued'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'cancelled'

export interface StyleRunMetrics {
  run_id: number
  samples_seen: number | null
  epoch: number | null
  total_epochs: number | null
  loss: number | null
  eta_s: number | null
  engine_mode?: string | null
  engine_id?: string | null
}

export interface StyleRun {
  run_id: number
  status: StyleRunStatus
  sample_ids: number[]
  config: Record<string, unknown>
  created_at: string
  updated_at: string
}

export interface RenderItem {
  retry_regions?: Record<string, { x_norm: number; y_norm: number; w_norm: number; h_norm: number }>
  image_size?: [number, number]
  item_id: string
  status: string
  cn?: string
  blessing?: string | null
  output_relative_path?: string | null
  fallback_used: boolean
  fallback_map: Record<string, string>
  warnings: string[]
  last_error?: string
  retryable: boolean
  reviewed_by?: string
  reviewed_at?: string
  comment?: string
  render_version: number
}

/** 渲染任务状态；终态集合见 RENDER_JOB_TERMINAL（api 层轮询终止判定） */
export type RenderJobStatus =
  | 'consumed'
  | 'rendering'
  | 'pending_review'
  | 'completed'
  | 'failed'
  | 'cancelled'
  | 'timeout'

export interface RenderJob {
  job_id: string
  status: RenderJobStatus | string
  draft: boolean
  item_count: number
  created_at: string
  ttl_expires_at?: string
}

export interface Blessing {
  id: string
  text: string
  enabled: boolean
  created_by: string
  updated_at: string
}

// ---------- 统一错误对象 / 错误码联合 ----------

/**
 * ApiErrorCode —— 契约错误码全量联合 + 前端合成码。
 * 分组注释标注来源契约，禁止在业务代码里写联合之外的裸字符串。
 */
export type ApiErrorCode =
  // v0.2 §3 错误码表（26 行）
  | 'INVALID_REQUEST'
  | 'PATH_OUTSIDE_DATA_ROOT'
  | 'ASSET_NOT_FOUND'
  | 'ASSET_NOT_APPROVED'
  | 'ASSET_DISABLED'
  | 'FORCE_DELETE_REQUIRED'
  | 'TEMPLATE_NOT_APPROVED'
  | 'STYLE_NOT_FOUND'
  | 'STYLE_NOT_APPROVED'
  | 'STYLE_DISABLED'
  | 'VERSION_CONFLICT'
  | 'ITEM_NOT_APPROVED'
  | 'JOB_NOT_FOUND'
  | 'JOB_NOT_CANCELLABLE'
  | 'GLYPH_MISSING'
  | 'RENDER_ITEM_FAILED'
  | 'EXPORT_TOO_LARGE'
  | 'EXPORT_FAILED'
  | 'BACKUP_FAILED'
  | 'BACKUP_CHECKSUM_MISMATCH'
  | 'BACKUP_NOT_FOUND'
  | 'RESTORE_FAILED'
  | 'RESTORE_CONFIRMATION_REQUIRED'
  | 'RESTORE_TARGET_NOT_EMPTY'
  | 'WORKER_RESTARTED'
  | 'WORKER_UNAVAILABLE'
  // v0.2.1 §3 新增（含 PASSWORD_CHANGE_REQUIRED 门禁码、PRD §6 USER_NOT_FOUND）
  | 'UNAUTHORIZED'
  | 'FORBIDDEN'
  | 'USER_DISABLED'
  | 'USER_NOT_FOUND'
  | 'FILE_TOO_LARGE'
  | 'QUOTA_EXCEEDED'
  | 'QUEUE_FULL'
  | 'PASSWORD_CHANGE_REQUIRED'
  // v0.3-patch-A §4（CC-4/CC-8）
  | 'OCR_FAILED'
  | 'DETECT_FAILED'
  | 'EXTRACT_FAILED'
  // v0.3-patch-B §3（CC-2/CC-3）
  | 'RUN_NOT_FOUND'
  | 'RUN_NOT_MUTABLE'
  | 'RUN_BLIND_REVIEW_REQUIRED'
  | 'SNAPSHOT_MISMATCH'
  | 'EVAL_NOT_PRODUCED'
  | 'EVAL_RECEIPT_INVALID'
  | 'ARTIFACT_INVALID'
  | 'M2_RUNTIME_UNAVAILABLE'
  | 'AI_PROVIDER_UNAVAILABLE'
  | 'AI_AUTHORIZATION_REQUIRED'
  | 'RUN_READINESS_BLOCKED'
  // 前端合成码（不来自后端；status=0 或客户端门卫）
  | 'TIMEOUT'
  | 'NETWORK'
  | 'UNKNOWN'
  /** R7：切后台 abort，reject PAUSED 且不消耗重试次数 */
  | 'PAUSED'

/** 统一错误对象（api 层抛；组件 toast 消费；http.ApiRequestError implements 本接口） */
export interface ApiError {
  status: number
  code: ApiErrorCode | (string & {})
  message: string
  retryable: boolean
}

// ---------- 响应信封（契约 v0.2 §2 / patch-A §0；store 不存信封，仅供 api 拆包） ----------

/** 成功响应壳：{ data, meta, warnings } */
export interface ApiEnvelope<T> {
  data: T
  meta: { api_version: string; schema_version: '0.2' | '0.4' | string }
  warnings: string[]
}

/** 失败响应壳：RFC 9457 application/problem+json 八字段 */
export interface ProblemDetail {
  type: string
  title: string
  status: number
  detail: string
  instance: string
  code: ApiErrorCode | (string & {})
  retryable: boolean
  trace_id: string
}

/** 分页信封（api 层拆包后传给 store；白皮书 §1.1 约定） */
export interface Paged<T> {
  items: T[]
  page: number
  page_size: number
  total: number
}

// ---------- 业务辅助领域类型 ----------

/** 覆盖预检单行（P5，POST /coverage/check） */
export interface CoverageRow {
  text: string
  missing: string[]
  fallback: Record<string, string>
  risk: 'low' | 'high'
}

// ---------- T33-A（CC-4/CC-8，patch-A 冻结） ----------

/** CC-4 POST /ocr/extract 单条文本候选 */
export interface OcrTextCandidate {
  field: 'cn' | 'blessing'
  text: string
  confidence: number
}

export interface OcrExtractResult {
  asset_id: string
  texts: OcrTextCandidate[]
}

/** CC-8 POST /detect-text 候选框（text 允许 null，Owner 裁定 #2） */
export interface DetectCandidate {
  id: string
  box_norm: Region
  text?: string | null
  confidence: number
}

export interface DetectTextResult {
  asset_id: string
  candidates: DetectCandidate[]
}

/** CC-8 detect 来源框（检测器候选） */
export interface DetectBoxRef {
  source: 'detect'
  detect_id: string
}

/** CC-8 manual 来源框（人工自由框选，永备路径） */
export interface ManualBoxRef {
  source: 'manual'
  box_norm: Region
}

export type ExtractBoxInput = DetectBoxRef | ManualBoxRef

export interface StrokeResult {
  ref: string
  stroke_path: string
  width: number
  height: number
  quality: 'ok' | 'suspect'
}

export interface ExtractResult {
  asset_id: string
  strokes: StrokeResult[]
}

// ---------- T33-B（CC-2/CC-3，patch-B 冻结；M2 Beta 训练扩展） ----------

/** GET /styles/{id}/samples / GET /style-runs/{id}/samples 列表项 */
export interface StyleSample {
  sample_id: number
  style_id?: number
  text_label: string | null
  source_training_image_id: number | null
  uploaded_at: string
}

/** GET /style-runs/{id}/progress 轻量轮询载荷 */
export interface InferenceDeploymentState {
  run_id: number
  state: 'registered' | 'not_registered' | 'blocked' | 'not_required'
  supports_preserved_training: boolean
  deployment_binding?: Record<string, unknown>
  training_runtime_binding?: Record<string, unknown>
  runtime_binding?: Record<string, unknown>
  reason?: string
  available: { registration_id: string; variant_id: string; device: string; ready: boolean }[]
}

export interface RunProgress {
  run_id: number
  status: StyleRunStatus
  stage: string | null
  engine_mode: string | null
  epoch: number | null
  total_epochs: number | null
  loss: number | null
  eta_s: number | null
  percent: number | null
  samples_seen: number | null
  units_done: number | null
  units_total: number | null
  unit: string | null
  failure?: { code?: string; message?: string; [key: string]: unknown } | null
  admission?: { status: 'PASSED'; receipt_sha256: string; model_sha256: string }
  historical_execution?: { stage: string; failure?: { code?: string; message?: string; [key: string]: unknown } | null }
  log_tail: string[]
  updated_at: string
}

/** GET /style-runs/{id}/metrics receipt-bound/pending/unavailable response */
export interface RunMetricsResponse {
  run_id: number
  state: 'pending' | 'unavailable' | 'available'
  reason: string | null
  metrics: StyleRunMetrics | null
  receipt: { relative_path: string; sha256: string; engine_mode?: string; engine_id?: string; engine_version?: string } | null
  detail?: { code?: string; message?: string; [key: string]: unknown }
}

/** GET /style-runs/{id}/evaluate receipt-bound/pending/unavailable response */
export interface EvaluateResponse {
  run_id: number
  state: 'pending' | 'unavailable' | 'available'
  reason: string | null
  report: EvaluateReport | null
  receipt: { relative_path: string; sha256: string } | null
}

/** GET /style-runs/{id}/evaluate 评估报告 */
export interface EvaluateReport {
  report_id: string
  glyph_accuracy: number
  style_similarity: number
  coverage: number
  per_char_sample: { char: string; ok: boolean }[]
  generated_samples: string[]
}

/** POST /style-runs/{id}/blind-review 请求体 */
export interface BlindReviewSubmission {
  items: { sample_key: string; score: 0 | 1 | 2 | 3 }[]
  verdict: 'pass' | 'fail'
}

export interface BlindReviewResult {
  review_id: string
}


/** Immutable registration identity supplied by the server, never edited by P7. */
export interface M2RuntimeBinding {
  schema: 'm2-runtime-binding/1'
  registration_id: string
  variant_id: string
  device: 'cpu' | 'cuda'
  asset_identity_sha256: string
  dependency_lock_sha256: string
  execution_profile_sha256: string
  environment_identity_sha256: string
  capability_receipt_sha256: string | null
  binding_sha256: string
}

/** Hardware/dependency readiness alone is not permission to run the full model. */
export interface M2RuntimeOption {
  registration_id?: string
  variant_id?: string | null
  device?: 'cpu' | 'cuda' | null
  status: string
  ready: boolean
  environment_ready: boolean
  full_model_ready: boolean
  code?: string
  reason?: string
}

/** M2 config is discovered and verified on the server. Never reconstructed from user-entered hashes. */
export interface PersonalRuntimeDependency {
  key: string
  status: string
  verified: boolean
  file?: string
  expected_bytes?: number
  expected_sha256?: string
}

export interface PersonalRuntimeInstallation {
  schema: 'windows-personal-style-dependencies/1'
  status: 'BLOCKED'
  configuration_status: string
  interpreter: PersonalRuntimeDependency
  assets: PersonalRuntimeDependency[]
  full_model: { status: string; configured: boolean }
  execution_policy: { status: string; code: string }
  qualification: string
  verification_mode: string
  may_prepare: false
  may_train: false
  may_publish: false
  recovery: { check_command: string; steps: string[]; qualification_entrypoint: string; qualification_requires: string[]; receipt_import_supported: false }
}

export interface M2Runtime {
  installation?: PersonalRuntimeInstallation
  snapshot?: { snapshot_sha256: string } | null
  runtime_options?: M2RuntimeOption[]
  selected_runtime_binding?: M2RuntimeBinding | null
  snapshot_preparation_dependencies?: {
    status: 'READY' | 'BLOCKED_DEPENDENCY'
    missing_metadata: { path: string; expected_sha256: string }[]
    remediation: string
  }
  status: 'READY' | 'BLOCKED'
  ready: boolean
  config: Record<string, unknown> | null
  sample_ids?: number[]
  blockers: { code: string; detail: string }[]
  capabilities: {
    create_m2_run: boolean
    run_evaluation: boolean
    ai_visual_review: boolean
    ai_candidate_confirmation: boolean
    offline_ai_review_import: boolean
  }
  acceptance_policy?: {
    mode: string
    human_blind_review: string
    candidate_confirmation: string
    policy_sha256?: string
  }
}

export interface PublishedStyleProfile extends StyleProfile {
  artifact?: {
    artifact_id: string
    engine_mode: string
    engine_id: string
    engine_version: string
    relative_path: string
    sha256: string
    quality_status: string
  }
}

export interface RunReadiness {
  run_id: number
  status: string
  AI_VISUAL_REVIEW: string
  AI_BLIND_REVIEW?: string
  OWNER_BLIND_REVIEW: string
  HUMAN_BLIND_REVIEW?: string
  OWNER_DIRECT_VISUAL_ACCEPTANCE?: string
  visual_acceptance_mode?: 'owner_direct_visual_acceptance'
  blockers: Record<string, string>
  details?: string[]
  allowed_actions: { evaluate: boolean; ai_review: boolean; publish: boolean }
  evaluation_state: 'not_started' | 'running' | 'complete' | 'blocked' | 'failed'
  ai_review_state?: 'not_started' | 'running' | 'complete' | 'blocked' | 'failed'
  publication: PublishedStyleProfile | null
}

export interface M2WorkflowJob {
  run_id: number
  state: string
}

export interface CandidateAiConfirmation {
  confirmed: number
  isolated: number
  items: { sample_id: string; review_status: 'ai_confirmed_authorized' | 'candidate'; reason?: string }[]
}


export interface SnapshotPreparation {
  target_user_id?: number
  target_style_id?: number
  base_intake_sha256?: string
  status: 'READY' | 'BLOCKED' | 'BLOCKED_DEPENDENCY'
  snapshot_ready: boolean
  snapshot_created: boolean
  snapshot_reused: boolean
  blockers: string[]
  dataset_snapshot_dir?: string
  dataset_snapshot_sha256?: string
  snapshot_binding?: Record<string, unknown>
}
