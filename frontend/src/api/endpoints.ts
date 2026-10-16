/* frozen-gated: true —— 本文件是端点声明单一来源（非页面消费组件）：
   仅在此定义/登记 frozen:false 端点，从不发起调用；消费侧门规则对页面/组件照常生效。 */
/**
 * 全部 API 路径常量集中维护（白皮书 §1.4.4；契约 v0.2 52 端点 + v0.2.1 五端点
 * + v0.3-patch-A（CC-4/CC-8）+ v0.3-patch-B（CC-2/CC-3 训练扩展））。
 *
 * frozen 标记纪律（T35 硬约束；B5-02 解冻更新）：
 * - frozen:true  = 契约已冻结，前端可正常调用；
 * - T33-B 训练扩展现已由 B-GATE-STYLE-API 与 OpenAPI v4 冻结；
 *   七个扩展端点与基线生命周期端点均可由真实 P7 控制台消费。
 * - frozen:false 不再用于已发布的训练接口。
 * - B5-02（2026-09-24）：CC-8 detect-text / extract 已随批次 B5 后端解冻
 *   （契约 style-font-pipeline-v1 + patch-D §3/§4/§8），由 unfrozen 转 frozen，
 *   P4_Extract 按 PRD-v0.7 §5.4 + patch-D §4 四要素正常接线。
 * - frozen:false 端点仅允许被整页置灰组件消费：ESLint 规则
 *   frozen-gate/no-frozen-endpoint-outside-gated 在构建期拦截（见 eslint.config.js），
 *   且本文件 ENDPOINT_REGISTRY 的 frozen 不变量由 tests/unit/endpoints-frozen.spec.ts 守护。
 *
 * 页面禁止硬编码 /api/v1/... 字符串；路径插值用 fill()。
 */

export interface EndpointDef {
  method: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'
  path: string
  frozen: boolean
}

const frozen = (method: EndpointDef['method'], path: string): EndpointDef => ({
  method,
  path,
  frozen: true,
})
// ---------- v0.2.1 §2 认证与用户（5） ----------

export const ENDPOINT_AUTH_ME = frozen('GET', '/api/v1/auth/me')
export const ENDPOINT_AUTH_LOGIN = frozen('POST', '/api/v1/auth/login')
export const ENDPOINT_AUTH_LOGOUT = frozen('POST', '/api/v1/auth/logout')
export const ENDPOINT_AUTH_CHANGE_PASSWORD = frozen('POST', '/api/v1/auth/change-password')
export const ENDPOINT_AUTH_REGISTER = frozen('POST', '/api/v1/auth/register')
export const ENDPOINT_USER_EXPORT = frozen('POST', '/api/v1/users/me/export')

// ---------- v0.2 §1.1 资产（6） ----------

export const ENDPOINT_ASSETS = frozen('GET', '/api/v1/assets')
export const ENDPOINT_ASSETS_CREATE = frozen('POST', '/api/v1/assets')
export const ENDPOINT_ASSET_DETAIL = frozen('GET', '/api/v1/assets/{id}')
export const ENDPOINT_ASSET_APPROVE = frozen('POST', '/api/v1/assets/{id}/approve')
export const ENDPOINT_ASSET_REJECT = frozen('POST', '/api/v1/assets/{id}/reject')
export const ENDPOINT_ASSET_DELETE = frozen('DELETE', '/api/v1/assets/{id}')

// ---------- v0.2 §1.2 位置模板（6） ----------

export const ENDPOINT_LAYOUT_TEMPLATES = frozen('GET', '/api/v1/layout-templates')
export const ENDPOINT_LAYOUT_TEMPLATE_CREATE = frozen('POST', '/api/v1/layout-templates')
export const ENDPOINT_LAYOUT_TEMPLATE_DETAIL = frozen('GET', '/api/v1/layout-templates/{id}')
export const ENDPOINT_LAYOUT_TEMPLATE_UPDATE = frozen('PUT', '/api/v1/layout-templates/{id}')
export const ENDPOINT_LAYOUT_TEMPLATE_APPROVE = frozen(
  'POST',
  '/api/v1/layout-templates/{id}/approve',
)
export const ENDPOINT_LAYOUT_TEMPLATE_REJECT = frozen(
  'POST',
  '/api/v1/layout-templates/{id}/reject',
)

// ---------- v0.2 §1.3 祝福语模板（6；v0.4 E-6.5：旧别名路径已移除，统一新路径） ----------

export const ENDPOINT_PHRASE_TEMPLATES = frozen('GET', '/api/v1/phrase-templates')
export const ENDPOINT_PHRASE_TEMPLATE_CREATE = frozen('POST', '/api/v1/phrase-templates')
export const ENDPOINT_PHRASE_TEMPLATE_DETAIL = frozen(
  'GET',
  '/api/v1/phrase-templates/{id}',
)
export const ENDPOINT_PHRASE_TEMPLATE_UPDATE = frozen('PUT', '/api/v1/phrase-templates/{id}')
export const ENDPOINT_PHRASE_TEMPLATE_DISABLE = frozen(
  'POST',
  '/api/v1/phrase-templates/{id}/disable',
)
export const ENDPOINT_PHRASE_TEMPLATE_ENABLE = frozen(
  'POST',
  '/api/v1/phrase-templates/{id}/enable',
)

// ---------- v0.2 §1.4 风格档案与样本（7） ----------

export const ENDPOINT_STYLES = frozen('GET', '/api/v1/styles')
export const ENDPOINT_STYLE_CREATE = frozen('POST', '/api/v1/styles')
export const ENDPOINT_STYLE_DETAIL = frozen('GET', '/api/v1/styles/{id}')
export const ENDPOINT_STYLE_DISABLE = frozen('POST', '/api/v1/styles/{id}/disable')
export const ENDPOINT_STYLE_ENABLE = frozen('POST', '/api/v1/styles/{id}/enable')
export const ENDPOINT_STYLE_SAMPLES = frozen('GET', '/api/v1/styles/{id}/samples')
export const ENDPOINT_STYLE_SAMPLE_UPLOAD = frozen('POST', '/api/v1/styles/{id}/samples')
export const ENDPOINT_TRAINING_SETS = frozen('GET', '/api/v1/training-sets')
export const ENDPOINT_TRAINING_IMAGES_UPLOAD = frozen('POST', '/api/v1/training-sets/images')
export const ENDPOINT_TRAINING_IMAGE_DELETE = frozen('DELETE', '/api/v1/training-sets/images/{id}')
export const ENDPOINT_TRAINING_IMAGE_FILE = frozen('GET', '/api/v1/training-sets/images/{id}/file')
export const ENDPOINT_TRAINING_SAMPLES = frozen('GET', '/api/v1/training-samples')
export const ENDPOINT_TRAINING_SAMPLES_BATCH_REVIEW = frozen('POST', '/api/v1/training-samples/batch-review')

// ---------- v0.2 §1.5 覆盖预检（1） ----------

export const ENDPOINT_COVERAGE_CHECK = frozen('POST', '/api/v1/coverage/check')

// ---------- v0.2 §1.6 渲染任务（14） ----------

export const ENDPOINT_RENDER_JOBS = frozen('GET', '/api/v1/render-jobs')
export const ENDPOINT_RENDER_JOB_CREATE = frozen('POST', '/api/v1/render-jobs')
export const ENDPOINT_RENDER_JOB_DETAIL = frozen('GET', '/api/v1/render-jobs/{id}')
export const ENDPOINT_RENDER_JOB_ITEMS = frozen('GET', '/api/v1/render-jobs/{id}/items')
export const ENDPOINT_RENDER_JOB_CANCEL = frozen('POST', '/api/v1/render-jobs/{id}/cancel')
export const ENDPOINT_RENDER_JOB_RETRY = frozen('POST', '/api/v1/render-jobs/{id}/retry')
export const ENDPOINT_RENDER_JOB_RETRY_CANCELLED = frozen(
  'POST',
  '/api/v1/render-jobs/{id}/retry-cancelled',
)
export const ENDPOINT_RENDER_ITEM_PATCH = frozen(
  'PATCH',
  '/api/v1/render-jobs/{id}/items/{itemId}',
)
export const ENDPOINT_RENDER_ITEM_RETRY = frozen(
  'POST',
  '/api/v1/render-jobs/{id}/items/{itemId}/retry',
)
export const ENDPOINT_RENDER_JOB_REVIEW = frozen('POST', '/api/v1/render-jobs/{id}/review')
export const ENDPOINT_RENDER_ITEM_APPROVE = frozen(
  'POST',
  '/api/v1/render-jobs/{id}/items/{itemId}/approve',
)
export const ENDPOINT_RENDER_ITEM_REJECT = frozen(
  'POST',
  '/api/v1/render-jobs/{id}/items/{itemId}/reject',
)
export const ENDPOINT_RENDER_JOB_EXPORT = frozen('POST', '/api/v1/render-jobs/{id}/export')
export const ENDPOINT_RENDER_JOB_OUTPUTS = frozen('GET', '/api/v1/render-jobs/{id}/outputs')

// ---------- v0.2 §1.7 备份与恢复（5；仅 admin，v0.2.1 §2 约束） ----------

export const ENDPOINT_BACKUPS = frozen('GET', '/api/v1/backups')
export const ENDPOINT_BACKUP_CREATE = frozen('POST', '/api/v1/backups')
export const ENDPOINT_BACKUP_DETAIL = frozen('GET', '/api/v1/backups/{id}')
export const ENDPOINT_BACKUP_VERIFY = frozen('POST', '/api/v1/backups/{id}/verify')
export const ENDPOINT_BACKUP_RESTORE = frozen('POST', '/api/v1/backups/{id}/restore')

// ---------- v0.2 §1.8 Worker / 审计 / 风格训练基线（7） ----------

export const ENDPOINT_WORKERS_STATUS = frozen('GET', '/api/v1/workers/status')
export const ENDPOINT_AUDIT_LOGS = frozen('GET', '/api/v1/audit-logs')
export const ENDPOINT_STYLE_RUNS = frozen('POST', '/api/v1/style-runs')
export const ENDPOINT_STYLE_RUN_DETAIL = frozen('GET', '/api/v1/style-runs/{id}')
export const ENDPOINT_STYLE_RUN_START = frozen('POST', '/api/v1/style-runs/{id}/start')
export const ENDPOINT_STYLE_RUN_CANCEL = frozen('POST', '/api/v1/style-runs/{id}/cancel')
export const ENDPOINT_STYLE_RUN_PUBLISH = frozen('POST', '/api/v1/style-runs/{id}/publish')

// ---------- T33-A（v0.3-patch-A，2026-09-15 冻结） ----------

/** CC-4 AI 识图文本预填（已冻结 → frozen:true） */
export const ENDPOINT_OCR_EXTRACT = frozen('POST', '/api/v1/ocr/extract')

/**
 * CC-8 提取辅助双端点（B5-02 已解冻 → frozen:true）——
 * 语义事实源：patch-A §3.1 + patch-D §3.1/§3.4/§8 + 契约 style-font-pipeline-v1。
 * ESLint frozen-gate 规则对这两个符号名仍按历史清单拦截（见 discrepancy 登记簿），
 * 消费组件（P4_Extract）暂保留 frozen-gated 标记注释以满足规则，直至该门退役。
 */
export const ENDPOINT_DETECT_TEXT = frozen('POST', '/api/v1/detect-text')
export const ENDPOINT_EXTRACT = frozen('POST', '/api/v1/extract')

// ---------- T33-B（v0.3-patch-B；B-GATE-STYLE-API/OpenAPI v4 已冻结） ----------

export const ENDPOINT_STYLE_RUN_METRICS = frozen('GET', '/api/v1/style-runs/{id}/metrics')
export const ENDPOINT_STYLE_RUN_SAMPLES = frozen('GET', '/api/v1/style-runs/{id}/samples')
export const ENDPOINT_STYLE_RUN_SAMPLE_DELETE = frozen(
  'DELETE',
  '/api/v1/style-runs/{id}/samples/{sampleId}',
)
export const ENDPOINT_STYLE_RUN_UPDATE = frozen('PUT', '/api/v1/style-runs/{id}')
export const ENDPOINT_STYLE_RUN_PROGRESS = frozen('GET', '/api/v1/style-runs/{id}/progress')
// Owned recovery: only server checkpoint IDs and one exact idempotent attempt.
export const ENDPOINT_STYLE_RUN_RECOVERY = frozen('GET', '/api/v1/style-runs/{id}/recovery')
export const ENDPOINT_STYLE_RUN_RETRY = frozen('POST', '/api/v1/style-runs/{id}/retry')
export const ENDPOINT_STYLE_RUN_RESUME = frozen('POST', '/api/v1/style-runs/{id}/resume')
export const ENDPOINT_STYLE_RUN_EVALUATE = frozen('GET', '/api/v1/style-runs/{id}/evaluate')
export const ENDPOINT_STYLE_RUN_BLIND_REVIEW = frozen(
  'POST',
  '/api/v1/style-runs/{id}/blind-review',
)

// ---------- Native intake R3: explicit admission, trusted evidence and occurrence selection ----------
export const ENDPOINT_NATIVE_SOURCE_ADMIT = frozen('POST', '/api/v1/training-sources/{id}/admit')
export const ENDPOINT_NATIVE_REVIEW_CREATE = frozen('POST', '/api/v1/candidate-review-jobs')
export const ENDPOINT_NATIVE_REVIEW_LIST = frozen('GET', '/api/v1/candidate-review-jobs')
export const ENDPOINT_NATIVE_REVIEW_EXPORT = frozen('GET', '/api/v1/candidate-review-jobs/{id}/export')
export const ENDPOINT_NATIVE_REVIEW_INGRESS = frozen('GET', '/api/v1/candidate-review-jobs/{id}/review-ingress')
export const ENDPOINT_NATIVE_REVIEW_REQUEST = frozen('POST', '/api/v1/candidate-review-jobs/{id}/review-ingress')
export const ENDPOINT_NATIVE_REVIEW_IMPORT = frozen('POST', '/api/v1/candidate-review-jobs/{id}/review-ingress/import')
export const ENDPOINT_NATIVE_REVIEW_REQUEST_CANCEL = frozen('POST', '/api/v1/candidate-review-jobs/{id}/review-ingress/cancel')
export const ENDPOINT_NATIVE_REVIEW_DETAIL = frozen('GET', '/api/v1/candidate-review-jobs/{id}')
export const ENDPOINT_NATIVE_REVIEW_ALPHA = frozen('GET', '/api/v1/candidate-review-jobs/{id}/candidates/{candidateId}/alpha')
export const ENDPOINT_NATIVE_REVIEW_CONTEXT = frozen('GET', '/api/v1/candidate-review-jobs/{id}/candidates/{candidateId}/source-context')
export const ENDPOINT_NATIVE_REVIEW_APPLY = frozen('POST', '/api/v1/candidate-review-jobs/{id}/apply')
export const ENDPOINT_NATIVE_REVIEW_CANCEL = frozen('POST', '/api/v1/candidate-review-jobs/{id}/cancel')
export const ENDPOINT_NATIVE_OCCURRENCES = frozen('GET', '/api/v1/styles/{id}/occurrence-selection')
export const ENDPOINT_NATIVE_OCCURRENCE_SELECT = frozen('POST', '/api/v1/styles/{id}/occurrence-selection')

// ---------- M2 product workflow: verified runtime and distinct AI acceptance ----------
export const ENDPOINT_STYLE_M2_RUNTIME = frozen('GET', '/api/v1/styles/{id}/m2-runtime')
export const ENDPOINT_STYLE_RUN_READINESS = frozen('GET', '/api/v1/style-runs/{id}/readiness')
export const ENDPOINT_STYLE_RUN_EVALUATION_START = frozen('POST', '/api/v1/style-runs/{id}/evaluate')
export const ENDPOINT_STYLE_RUN_AI_REVIEW = frozen('POST', '/api/v1/style-runs/{id}/ai-review')
export const ENDPOINT_TRAINING_SAMPLES_AI_CONFIRM = frozen('POST', '/api/v1/training-samples/ai-confirm')
export const ENDPOINT_RENDER_CAPABILITIES = frozen('GET', '/api/v1/render-jobs/capabilities')
export const ENDPOINT_TRAINING_SNAPSHOT_PREFLIGHT = frozen('GET', '/api/v1/training-samples/snapshot-preflight')
export const ENDPOINT_TRAINING_SNAPSHOT_PREPARE = frozen('POST', '/api/v1/training-samples/prepare-snapshot')

// ---------- 注册表（单源枚举，供测试与 mock adapter 派生） ----------

export const ENDPOINT_REGISTRY: EndpointDef[] = [
  ENDPOINT_STYLE_RUN_RECOVERY,
  ENDPOINT_STYLE_RUN_RETRY,
  ENDPOINT_STYLE_RUN_RESUME,
  ENDPOINT_AUTH_ME,
  ENDPOINT_AUTH_LOGIN,
  ENDPOINT_AUTH_LOGOUT,
  ENDPOINT_AUTH_CHANGE_PASSWORD,
  ENDPOINT_USER_EXPORT,
  ENDPOINT_ASSETS,
  ENDPOINT_ASSETS_CREATE,
  ENDPOINT_ASSET_DETAIL,
  ENDPOINT_ASSET_APPROVE,
  ENDPOINT_ASSET_REJECT,
  ENDPOINT_ASSET_DELETE,
  ENDPOINT_LAYOUT_TEMPLATES,
  ENDPOINT_LAYOUT_TEMPLATE_CREATE,
  ENDPOINT_LAYOUT_TEMPLATE_DETAIL,
  ENDPOINT_LAYOUT_TEMPLATE_UPDATE,
  ENDPOINT_LAYOUT_TEMPLATE_APPROVE,
  ENDPOINT_LAYOUT_TEMPLATE_REJECT,
  ENDPOINT_PHRASE_TEMPLATES,
  ENDPOINT_PHRASE_TEMPLATE_CREATE,
  ENDPOINT_PHRASE_TEMPLATE_DETAIL,
  ENDPOINT_PHRASE_TEMPLATE_UPDATE,
  ENDPOINT_PHRASE_TEMPLATE_DISABLE,
  ENDPOINT_PHRASE_TEMPLATE_ENABLE,
  ENDPOINT_STYLES,
  ENDPOINT_STYLE_CREATE,
  ENDPOINT_STYLE_DETAIL,
  ENDPOINT_STYLE_DISABLE,
  ENDPOINT_STYLE_ENABLE,
  ENDPOINT_STYLE_SAMPLES,
  ENDPOINT_STYLE_SAMPLE_UPLOAD,
  ENDPOINT_TRAINING_SETS,
  ENDPOINT_TRAINING_IMAGES_UPLOAD,
  ENDPOINT_TRAINING_IMAGE_DELETE,
  ENDPOINT_TRAINING_IMAGE_FILE,
  ENDPOINT_TRAINING_SAMPLES,
  ENDPOINT_TRAINING_SAMPLES_BATCH_REVIEW,
  ENDPOINT_COVERAGE_CHECK,
  ENDPOINT_RENDER_JOBS,
  ENDPOINT_RENDER_JOB_CREATE,
  ENDPOINT_RENDER_JOB_DETAIL,
  ENDPOINT_RENDER_JOB_ITEMS,
  ENDPOINT_RENDER_JOB_CANCEL,
  ENDPOINT_RENDER_JOB_RETRY,
  ENDPOINT_RENDER_JOB_RETRY_CANCELLED,
  ENDPOINT_RENDER_ITEM_PATCH,
  ENDPOINT_RENDER_ITEM_RETRY,
  ENDPOINT_RENDER_JOB_REVIEW,
  ENDPOINT_RENDER_ITEM_APPROVE,
  ENDPOINT_RENDER_ITEM_REJECT,
  ENDPOINT_RENDER_JOB_EXPORT,
  ENDPOINT_RENDER_JOB_OUTPUTS,
  ENDPOINT_BACKUPS,
  ENDPOINT_BACKUP_CREATE,
  ENDPOINT_BACKUP_DETAIL,
  ENDPOINT_BACKUP_VERIFY,
  ENDPOINT_BACKUP_RESTORE,
  ENDPOINT_WORKERS_STATUS,
  ENDPOINT_AUDIT_LOGS,
  ENDPOINT_STYLE_RUNS,
  ENDPOINT_STYLE_RUN_DETAIL,
  ENDPOINT_STYLE_RUN_START,
  ENDPOINT_STYLE_RUN_CANCEL,
  ENDPOINT_STYLE_RUN_PUBLISH,
  ENDPOINT_OCR_EXTRACT,
  ENDPOINT_DETECT_TEXT,
  ENDPOINT_EXTRACT,
  ENDPOINT_STYLE_RUN_METRICS,
  ENDPOINT_STYLE_RUN_SAMPLES,
  ENDPOINT_STYLE_RUN_SAMPLE_DELETE,
  ENDPOINT_STYLE_RUN_UPDATE,
  ENDPOINT_STYLE_RUN_PROGRESS,
  ENDPOINT_STYLE_RUN_EVALUATE,
  ENDPOINT_STYLE_RUN_BLIND_REVIEW,
  ENDPOINT_STYLE_M2_RUNTIME,
  ENDPOINT_STYLE_RUN_READINESS,
  ENDPOINT_STYLE_RUN_EVALUATION_START,
  ENDPOINT_STYLE_RUN_AI_REVIEW,
  ENDPOINT_TRAINING_SAMPLES_AI_CONFIRM,
  ENDPOINT_TRAINING_SNAPSHOT_PREPARE,
  ENDPOINT_TRAINING_SNAPSHOT_PREFLIGHT,
  ENDPOINT_RENDER_CAPABILITIES,
  ENDPOINT_NATIVE_SOURCE_ADMIT,
  ENDPOINT_NATIVE_REVIEW_CREATE,
  ENDPOINT_NATIVE_REVIEW_LIST,
  ENDPOINT_NATIVE_REVIEW_DETAIL,
  ENDPOINT_NATIVE_REVIEW_EXPORT,
  ENDPOINT_NATIVE_REVIEW_INGRESS,
  ENDPOINT_NATIVE_REVIEW_REQUEST,
  ENDPOINT_NATIVE_REVIEW_IMPORT,
  ENDPOINT_NATIVE_REVIEW_REQUEST_CANCEL,
  ENDPOINT_NATIVE_REVIEW_ALPHA,
  ENDPOINT_NATIVE_REVIEW_CONTEXT,
  ENDPOINT_NATIVE_REVIEW_APPLY,
  ENDPOINT_NATIVE_REVIEW_CANCEL,
  ENDPOINT_NATIVE_OCCURRENCES,
  ENDPOINT_NATIVE_OCCURRENCE_SELECT,
]

/** 路径插值：把 {id}/{itemId} 占位符替换为实际值 */
export function fill(template: string, params: Record<string, string>): string {
  return template.replace(/\{(\w+)\}/g, (_, k: string) => params[k] ?? `{${k}}`)
}
