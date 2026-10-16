import type { FamilySplitSide, SnapshotPreflight } from '@/types/snapshot'
const hash = (value: unknown): value is string => typeof value === 'string' && /^[0-9a-f]{64}$/.test(value)
const unique = (values: string[]) => new Set(values).size === values.length
export function assertSnapshotPreflight(value: SnapshotPreflight, userId: string, styleId: number): void {
  if (!value || value.schema !== 'personal-snapshot-preflight/1' || String(value.target_user_id) !== userId || value.target_style_id !== styleId
    || !['PREFLIGHT_READY', 'BLOCKED'].includes(value.status) || !Array.isArray(value.blockers)
    || value.blockers.some((item) => typeof item !== 'string') || typeof value.explicit_family_split_required !== 'boolean'
    || typeof value.structural_dataset_ready !== 'boolean' || value.snapshot_ready !== false || value.snapshot_created !== false
    || value.authorization_created !== false || value.selection_requires_expected_base_intake_sha256 !== true
    || !(value.base_intake_sha256 === null || hash(value.base_intake_sha256))
    || !Array.isArray(value.eligible_parent_sources) || !Array.isArray(value.family_components)) throw new Error('来源分组预检与当前账号或档案不匹配，请刷新')
  const sources = value.eligible_parent_sources
  if (sources.some((source) => !source || !source.source_asset_id || !hash(source.source_asset_sha256)
    || String(source.source_admission?.user_id) !== userId || source.source_admission?.style_id !== styleId
    || source.source_admission?.asset_id !== source.source_asset_id || source.source_admission?.source_sha256 !== source.source_asset_sha256
    || !hash(source.source_admission?.receipt_sha256)) || !unique(sources.map((source) => source.source_asset_id))) throw new Error('来源授权不完整或不属于当前档案')
  const expected = [...new Set(sources.map((source) => source.source_asset_sha256))].sort()
  const assigned: string[] = []
  if (!unique(value.family_components.map((component) => component.component_id))) throw new Error('服务端来源组编号重复')
  for (const component of value.family_components) {
    if (!component.component_id || !Array.isArray(component.parent_hashes) || !component.parent_hashes.length
      || component.parent_hashes.some((parent) => !hash(parent)) || !unique(component.parent_hashes)
      || !Array.isArray(component.source_asset_ids) || !unique(component.source_asset_ids)
      || !Array.isArray(component.family_ids) || component.family_ids.some((id) => typeof id !== 'string')) throw new Error('服务端来源组不完整')
    const assets = sources.filter((source) => component.parent_hashes.includes(source.source_asset_sha256)).map((source) => source.source_asset_id).sort()
    if (JSON.stringify(assets) !== JSON.stringify([...component.source_asset_ids].sort())) throw new Error('来源组与原图身份不一致')
    assigned.push(...component.parent_hashes)
  }
  if (!unique(assigned) || JSON.stringify(assigned.sort()) !== JSON.stringify(expected)) throw new Error('来源组未精确覆盖当前原图，请刷新')
}
export function snapshotPlanIdentity(value: SnapshotPreflight): string {
  return JSON.stringify([value.base_intake_sha256, value.explicit_family_split_required,
    value.eligible_parent_sources.map((source) => [source.source_asset_id, source.source_asset_sha256, source.source_admission.receipt_sha256]).sort(),
    value.family_components.map((component) => [component.component_id, [...component.parent_hashes].sort(), [...component.source_asset_ids].sort(), [...component.family_ids].sort()]).sort()])
}
export function parentAssignments(value: SnapshotPreflight, choices: Record<string, string>): Record<string, FamilySplitSide> | null {
  if (!hash(value.base_intake_sha256) || !value.family_components.length
    || Object.keys(choices).some((id) => !value.family_components.some((component) => component.component_id === id))) return null
  const result: Record<string, FamilySplitSide> = {}
  for (const component of value.family_components) {
    const side = choices[component.component_id]
    if (side !== 'train' && side !== 'val') return null
    for (const parent of component.parent_hashes) result[parent] = side
  }
  return new Set(Object.values(result)).size === 2 ? result : null
}
