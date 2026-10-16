import { request } from '@/api/http'

interface UnpublishedModelIdentity {
  import_id: string
  adapter_sha256: string
  manifest_sha256: string
  input_fingerprint_sha256: string
  created_at: string
  source_summary: string
  publication_status: 'UNPUBLISHED'
  publish_ready: false
  model_quality_approval: false
}
const ACCEPTED_V3_SCOPE = {
  id: 'user-accepted-0178-v3-20261007-r2', text_lines: ['To：shitT', '拉shit永远通畅'],
  latin_font_sha256: '0bdb6b660482d31531b3945849fba5916b3ef8695da7024a9e6b9ee3c4157988', latin_weight: 600,
  punctuation_font_sha256: 'b76b0433203017ca80401b2ee0dd69350349871c4b19d504c34dbdd80541690a', punctuation_face_index: 2,
  color: '#8C3044', layout_seed: 2026100401, model_quality_approval: false, publication_approval: false,
} as const
type AcceptedV3Scope = typeof ACCEPTED_V3_SCOPE
export type UnpublishedModel = UnpublishedModelIdentity & (
  { runtime_status: 'NOT_REGISTERED', inference_ready: false, verification_scope: 'IMPORT_RECORD_ONLY' }
  | { runtime_status: 'SELECTED_BLOCKED', inference_ready: false, inference_selection_id: number,
      verification_scope: 'CURRENT_SELECTION_RECHECK_BLOCKED' }
  | { runtime_status: 'INFERENCE_READY', inference_ready: true, inference_selection_id: number,
      verification_scope: 'CURRENT_OWNED_QUALIFIED_V3_SELECTION', quality_status: 'V3_LIMITED_USER_ACCEPTED',
      inference_only: true, acceptance_scope: AcceptedV3Scope }
)
export interface UnpublishedModelList {
  items: UnpublishedModel[]
  total: number
  page: number
  page_size: number
  registry_status: 'ready' | 'not_installed'
}
const record = (value: unknown): value is Record<string, unknown> => !!value && typeof value === 'object' && !Array.isArray(value)
const hash = (value: unknown) => typeof value === 'string' && /^[0-9a-f]{64}$/.test(value)
const selection = (value: unknown) => Number.isSafeInteger(value) && Number(value) > 0
const noQualifiedScope = (row: Record<string, unknown>) => row.quality_status === undefined
  && row.inference_only === undefined && row.acceptance_scope === undefined
function acceptedV3(value: unknown): boolean {
  if (!record(value) || Object.keys(value).length !== Object.keys(ACCEPTED_V3_SCOPE).length) return false
  return Object.entries(ACCEPTED_V3_SCOPE).every(([key, expected]) => key === 'text_lines'
    ? Array.isArray(value.text_lines) && value.text_lines.length === 2
      && value.text_lines.every((line, index) => line === ACCEPTED_V3_SCOPE.text_lines[index])
    : value[key] === expected)
}
function validRuntime(row: Record<string, unknown>): boolean {
  if (row.runtime_status === 'NOT_REGISTERED') {
    return row.inference_ready === false && row.verification_scope === 'IMPORT_RECORD_ONLY'
      && row.inference_selection_id === undefined && noQualifiedScope(row)
  }
  if (!selection(row.inference_selection_id)) return false
  if (row.runtime_status === 'SELECTED_BLOCKED') {
    return row.inference_ready === false && row.verification_scope === 'CURRENT_SELECTION_RECHECK_BLOCKED'
      && noQualifiedScope(row)
  }
  return row.runtime_status === 'INFERENCE_READY' && row.inference_ready === true
    && row.verification_scope === 'CURRENT_OWNED_QUALIFIED_V3_SELECTION'
    && row.quality_status === 'V3_LIMITED_USER_ACCEPTED' && row.inference_only === true
    && row.adapter_sha256 === '017881442b7f8b909f7d8d8c4c5e7ad1893d0caa3c3267ae3c793a9a96175f81'
    && row.manifest_sha256 === '9bbefed5c2c845720a11423e0be241a5b30702a89818c67c1f1c4ffd474e40f2'
    && acceptedV3(row.acceptance_scope)
}
export async function readUnpublishedModels(page = 1): Promise<UnpublishedModelList> {
  const data = await request<unknown>(`/api/v1/styles/unpublished-models?page=${page}&page_size=20`, { timeoutMs: 600000, retries: 0 })
  if (!record(data) || !Array.isArray(data.items) || !Number.isSafeInteger(data.total) || Number(data.total) < 0
    || data.page !== page || data.page_size !== 20 || !['ready', 'not_installed'].includes(String(data.registry_status))
    || data.items.length > 20 || Number(data.total) < data.items.length
    || data.registry_status === 'not_installed' && (data.items.length > 0 || data.total !== 0)
    || data.items.some((row: unknown) => !record(row) || typeof row.import_id !== 'string' || !/^[0-9a-f]{32}$/.test(row.import_id)
      || !hash(row.adapter_sha256) || !hash(row.manifest_sha256) || !hash(row.input_fingerprint_sha256)
      || typeof row.created_at !== 'string' || !Number.isFinite(Date.parse(row.created_at))
      || typeof row.source_summary !== 'string' || !row.source_summary.trim()
      || row.publication_status !== 'UNPUBLISHED' || row.publish_ready !== false || row.model_quality_approval !== false
      || !validRuntime(row))) {
    throw new Error('未发布模型列表格式无法核验，请刷新或联系维护人员')
  }
  return data as unknown as UnpublishedModelList
}
