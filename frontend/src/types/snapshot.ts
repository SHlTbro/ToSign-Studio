export type FamilySplitSide = 'train' | 'val'
export interface SnapshotParentSource {
  source_asset_id: string
  source_asset_sha256: string
  source_admission: { user_id: number; style_id: number; asset_id: string; source_sha256: string; receipt_sha256: string; history_status?: string; actor_kind?: string }
}
export interface SnapshotFamilyComponent {
  component_id: string
  parent_hashes: string[]
  source_asset_ids: string[]
  family_ids: string[]
}
export interface SnapshotPreflight {
  schema: 'personal-snapshot-preflight/1'
  target_user_id: number
  target_style_id: number
  base_intake_sha256: string | null
  eligible_parent_sources: SnapshotParentSource[]
  explicit_family_split_required: boolean
  family_components: SnapshotFamilyComponent[]
  blockers: string[]
  status: 'PREFLIGHT_READY' | 'BLOCKED'
  structural_dataset_ready: boolean
  snapshot_ready: false
  snapshot_created: false
  authorization_created: false
  selection_requires_expected_base_intake_sha256: true
}
